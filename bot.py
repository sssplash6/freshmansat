"""Telegram bot entrypoint.

Registers the /start, /status, and /penalty command handlers, and wires up the
two APScheduler jobs (reminders + payment confirmation) to run alongside the
bot's async event loop.

Run:  ./venv/bin/python bot.py
"""
import asyncio
import datetime
import logging


from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from telegram.ext import CallbackQueryHandler
import config
import jobs
import messages
import sheets
from notify import notify_student
from telegram.helpers import escape_markdown

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")


def is_admin_update(update: Update) -> bool:
    """True if this update is from the admin group OR from a whitelisted
    admin user's private DM. Use this instead of comparing chat_id directly,
    so admins can use the bot outside the group too."""
    chat_id = update.effective_chat.id if update.effective_chat else None
    user_id = update.effective_user.id if update.effective_user else None
    if chat_id is not None and str(chat_id) == str(config.ADMIN_CHAT_ID):
        return True
    if user_id is not None and user_id in config.ADMIN_USER_IDS:
        return True
    return False


# --- Command handlers ------------------------------------------------------

def _md(text: str) -> str:
    """Escape a piece of dynamic text (student name, TG handle, group name,
    etc.) so it's safe to interpolate into a parse_mode='Markdown' message.
    """
    return escape_markdown(text or "", version=1)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    username = user.username
    chat_id = update.message.chat_id

    if not username:
        await update.message.reply_text(messages.welcome_no_username())
        return

    outcome = await asyncio.to_thread(sheets.register_user, username, chat_id)
    log.info("/start from @%s (chat_id=%s) -> %s", username, chat_id, outcome)
    await update.message.reply_text(messages.welcome_registered())


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    username = user.username

    if not username:
        await update.message.reply_text(messages.welcome_no_username())
        return

    rec = await asyncio.to_thread(sheets.find_student, username)
    if rec:
        await update.message.reply_text(
            messages.status_found(rec["name"], rec["group"], rec["amount"], rec["status"])
        )
    else:
        await update.message.reply_text(messages.status_not_found())


async def penalty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Report the sender's total active penalty points from Penalty Tracker."""
    user = update.message.from_user
    username = user.username

    if not username:
        await update.message.reply_text(messages.welcome_no_username())
        return

    rec = await asyncio.to_thread(sheets.find_penalty_record, username)
    if rec:
        await update.message.reply_text(
            messages.penalty_found(rec["name"], rec["class"], rec["points"])
        )
    else:
        await update.message.reply_text(messages.penalty_not_found())


async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a payment-proof photo: match student, forward to admin, record."""
    message = update.message
    user = message.from_user
    username = user.username
    chat_id = message.chat_id

    if not username:
        await message.reply_text(messages.welcome_no_username())
        return

    rec = await asyncio.to_thread(sheets.find_student, username)
    if not rec:
        await message.reply_text(messages.proof_no_record())
        return

    file_id = message.photo[-1].file_id
    date_str = sheets.today_str()

    try:
        if config.ADMIN_CHAT_ID:
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve:{username}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"reject:{username}"),
                ]
            ])
            await context.bot.send_photo(
                chat_id=config.ADMIN_CHAT_ID,
                photo=file_id,
                caption=messages.admin_proof_caption(
                    rec["name"], rec["group"], rec["amount"]
                ),
                reply_markup=keyboard,
            )
        else:
            log.warning("ADMIN_CHAT_ID not set — proof not forwarded to admin")

        await asyncio.to_thread(sheets.register_user, username, chat_id)
        await asyncio.to_thread(sheets.set_payment_proof, username, file_id, date_str)
    except Exception as exc:
        log.exception("Proof handling failed for @%s: %s", username, exc)
        await message.reply_text(
            "⚠️ Something went wrong while submitting your payment proof. "
            "Please try again shortly or contact the admin."
        )
        return

    bd_entry = await asyncio.to_thread(sheets.find_bot_data_row, username)
    if not bd_entry or not bd_entry.get("pay_shown", "").strip():
        await message.reply_text(messages.proof_before_pay_info())
        return
    await message.reply_text(messages.proof_received())
    log.info("Proof from @%s recorded (file_id=%s)", username, file_id)


async def set_payment_status(bot, target_username: str, status_label: str, source: str) -> tuple[bool, str]:
    """Single entry point for changing a student's payment status to any of
    Paid / Scholarship / Pending / Cancel. This is the ONLY place that
    writes payment status to the Finance sheet.
    """
    rec = await asyncio.to_thread(sheets.find_student, target_username)
    if not rec:
        return False, f"No student record found for @{target_username}."

    bd_entry = await asyncio.to_thread(sheets.find_bot_data_row, target_username)

    await asyncio.to_thread(sheets.set_student_status, rec["worksheet"], rec["row_number"], status_label)
    await asyncio.to_thread(
        sheets.log_payment_change, rec["name"], target_username, status_label, source, rec["amount"]
    )

    if bd_entry and bd_entry.get("chat_id"):
        await asyncio.to_thread(sheets.update_last_known_status, bd_entry["row_number"], status_label)
        if status_label.strip().lower() in config.PAID_STATUSES:
            try:
                await notify_student(
                    bot, int(bd_entry["chat_id"]), text=messages.payment_success(rec["name"])
                )
            except Exception as exc:
                log.warning("Could not notify @%s of payment approval: %s", target_username, exc)

    return True, f"Marked @{target_username} ({rec['name']}) as {status_label}."


async def set_bot_commands(app: Application):
    """Registers Telegram's native '/' autocomplete menu. Every admin
    identity — the group AND each whitelisted admin user's private DM —
    gets the admin command set, not just the group.
    """
    student_commands = [
        BotCommand("start", "Register with the bot"),
        BotCommand("status", "Check your payment status"),
        BotCommand("pay", "Get payment details and QR code"),
        BotCommand("penalty", "Check your penalty points"),
    ]
    await app.bot.set_my_commands(student_commands, scope=BotCommandScopeDefault())

    admin_commands = student_commands + [
        BotCommand("admin", "Open the admin menu"),
        BotCommand("attendance", "Mark attendance"),
        BotCommand("admin_setpayment", "Set a student's payment status directly"),
        BotCommand("reject", "Reject a student's payment proof"),
        BotCommand("admin_report", "Weekly/monthly stats per group"),
    ]
    admin_chat_ids = set()
    if config.ADMIN_CHAT_ID:
        admin_chat_ids.add(int(config.ADMIN_CHAT_ID))
    admin_chat_ids.update(config.ADMIN_USER_IDS)

    for chat_id in admin_chat_ids:
        try:
            await app.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=chat_id))
        except Exception as exc:
            log.warning("Could not set admin commands for chat_id=%s: %s", chat_id, exc)


# --- Guided admin menu (buttons instead of typed syntax) -------------------
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Set payment status", callback_data="admin_menu:setpayment")],
        [InlineKeyboardButton("📋 Browse roster", callback_data="admin_menu:browse")],
        [InlineKeyboardButton("🔍 Search student", callback_data="admin_menu:search")],
        [InlineKeyboardButton("✅ Mark attendance", callback_data="admin_menu:attendance")],
        [InlineKeyboardButton("➕ Add student", callback_data="admin_menu:addstudent")],
        [InlineKeyboardButton("➕ Add group", callback_data="admin_menu:addgroup")],
        [InlineKeyboardButton("🗑 Remove group", callback_data="admin_menu:removegroup")],
        [InlineKeyboardButton("❌ Cancel", callback_data="admin_menu:cancel")],
    ])
    await update.message.reply_text(
        "Admin menu — what would you like to do?", reply_markup=keyboard
    )


async def admin_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin_update(update):
        return

    _, _, action = query.data.partition(":")

    if action == "cancel":
        context.user_data.pop("pending_admin_action", None)
        await query.edit_message_text("Cancelled.")
        return

    if action == "setpayment":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Paid", callback_data="admin_setpayment_status:Paid"),
                InlineKeyboardButton("🎓 Scholarship", callback_data="admin_setpayment_status:Scholarship"),
            ],
            [
                InlineKeyboardButton("⏳ Pending", callback_data="admin_setpayment_status:Pending"),
                InlineKeyboardButton("🚫 Cancel", callback_data="admin_setpayment_status:Cancel"),
            ],
            [InlineKeyboardButton("⬅️ Cancel menu", callback_data="admin_menu:cancel")],
        ])
        await query.edit_message_text("Set status to:", reply_markup=keyboard)
        return

    if action == "browse":
        groups = await asyncio.to_thread(sheets.get_groups_schedule)
        buttons = [[InlineKeyboardButton(g["name"], callback_data=f"admin_group:{g['name']}")] for g in groups]
        buttons.append([InlineKeyboardButton("⬅️ Cancel", callback_data="admin_menu:cancel")])
        await query.edit_message_text("Pick a group:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if action == "search":
        context.user_data["pending_admin_action"] = {"type": "search_student"}
        await query.edit_message_text("Send part of a student's name or @username to search for.")
        return

    if action == "attendance":
        await _show_attendance_group_picker(query.edit_message_text)
        return
    
    if action == "addstudent":
        context.user_data["pending_admin_action"] = {"type": "addstudent_name"}
        await query.edit_message_text("New student — send their full name.")
        return

    if action == "addgroup":
        context.user_data["pending_admin_action"] = {"type": "addgroup_name"}
        await query.edit_message_text("New group — send the group name (e.g. \"Padawan Offline 2\").")
        return

    if action == "removegroup":
        groups = await asyncio.to_thread(sheets.get_groups_schedule)
        if not groups:
            await query.edit_message_text("No groups found.")
            return
        buttons = [[InlineKeyboardButton(g["name"], callback_data=f"admin_removegroup_pick:{g['name']}")] for g in groups]
        buttons.append([InlineKeyboardButton("⬅️ Cancel", callback_data="admin_menu:cancel")])
        await query.edit_message_text(
            "Pick a group to remove.\n\nThis only removes it from scheduling/syncing — "
            "students, attendance history, and logs are kept untouched.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return




    if action.startswith("admin_setpayment_status"):
        return


async def admin_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return

    _, _, group_name = query.data.partition(":")
    students = await asyncio.to_thread(sheets.get_roster_by_group, group_name)
    if not students:
        await query.edit_message_text(f"No students found in {group_name}.")
        return

    buttons = [[InlineKeyboardButton(s["name"], callback_data=f"admin_pick:{s['name']}")] for s in students]
    buttons.append([InlineKeyboardButton("⬅️ Cancel", callback_data="admin_menu:cancel")])
    await query.edit_message_text(f"{group_name} — pick a student:", reply_markup=InlineKeyboardMarkup(buttons))


async def admin_pick_student_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return

    _, _, student_name = query.data.partition(":")
    context.user_data["selected_student"] = student_name

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 View profile", callback_data="admin_action:profile")],
        [InlineKeyboardButton("➕ Add penalty", callback_data="admin_action:addpenalty")],
        [InlineKeyboardButton("➖ Remove penalty", callback_data="admin_action:removepenalty")],
        [InlineKeyboardButton("💰 Set payment status", callback_data="admin_action:setpayment_direct")],
        [InlineKeyboardButton("🚫 Remove student", callback_data="admin_action:removestudent")],
        [InlineKeyboardButton("✏️ Edit tuition", callback_data="admin_action:edittuition")],
        [InlineKeyboardButton("⬅️ Cancel", callback_data="admin_menu:cancel")],
    ])
    await query.edit_message_text(f"*{_md(student_name)}* — what would you like to do?",
                                   parse_mode="Markdown", reply_markup=keyboard)


def _format_student_profile(profile: dict) -> str:
    payment = profile["payment"]
    name = _md(profile['name'])
    tg = _md(profile['tg'])
    group = _md(profile['group'])
    payment_line = (
        f"{payment['status']} (amount: {payment['amount']}, as of {payment['timestamp']})"
        if payment else "No payment record yet"
    )
    return (
        f"👤 *{name}*\n"
        f"Group: {group}\n"
        f"TG: @{tg}\n\n"
        f"📅 Attendance — Present: {profile['attendance']['Present']}, "
        f"Late: {profile['attendance']['Late']}, Absent: {profile['attendance']['Absent']}\n"
        f"📚 Homework — On time: {profile['homework']['On Time']}, "
        f"Late: {profile['homework']['Late']}, Missing: {profile['homework']['Missing']}\n"
        f"⚠️ Total penalty points: {profile['total_points']}\n"
        f"💰 Payment: {payment_line}"
    )


_PENALTY_PRESETS = {
    "absence": ("Unexcused absence (manual)", 1),
    "lateness": ("Lateness (manual)", 1),
    "late_hw": ("Late homework (manual)", 1),
    "missing_hw": ("Missing homework (manual)", 3),
}


async def admin_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return

    student_name = context.user_data.get("selected_student")
    if not student_name:
        await query.edit_message_text("No student selected — start over with /admin.")
        return

    _, _, action = query.data.partition(":")

    if action == "profile":
        profile = await asyncio.to_thread(sheets.get_student_profile, student_name)
        if not profile:
            await query.edit_message_text(f"No profile data found for {student_name}.")
            return
        buttons = []
        payment = profile.get("payment")
        is_unpaid = (not payment) or (payment["status"].strip().lower() not in config.PAID_STATUSES)
        if is_unpaid and profile.get("tg"):
            buttons.append([InlineKeyboardButton("📨 Send reminder", callback_data=f"admin_sendreminder:{student_name}")])
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"admin_pick:{student_name}")])
        await query.edit_message_text(_format_student_profile(profile), parse_mode="Markdown",
                                       reply_markup=InlineKeyboardMarkup(buttons))
        return

    if action == "addpenalty":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Unexcused absence (-1)", callback_data="admin_penalty_preset:absence")],
            [InlineKeyboardButton("Lateness (-1)", callback_data="admin_penalty_preset:lateness")],
            [InlineKeyboardButton("Late homework (-1)", callback_data="admin_penalty_preset:late_hw")],
            [InlineKeyboardButton("Missing homework (-3)", callback_data="admin_penalty_preset:missing_hw")],
            [InlineKeyboardButton("✏️ Custom (type points + reason)", callback_data="admin_penalty_preset:custom")],
            [InlineKeyboardButton("⬅️ Back", callback_data=f"admin_pick:{student_name}")],
        ])
        await query.edit_message_text(f"Add penalty for *{_md(student_name)}* — pick a reason:",
                                       parse_mode="Markdown", reply_markup=keyboard)
        return

    if action == "removepenalty":
        penalties = await asyncio.to_thread(sheets.get_active_penalties, student_name)
        if not penalties:
            buttons = [[InlineKeyboardButton("⬅️ Back", callback_data=f"admin_pick:{student_name}")]]
            await query.edit_message_text(f"{student_name} has no active penalties.",
                                           reply_markup=InlineKeyboardMarkup(buttons))
            return
        buttons = [
            [InlineKeyboardButton(f"{p['reason']} (-{p['points']})", callback_data=f"admin_removepenalty_row:{p['row_number']}")]
            for p in penalties
        ]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"admin_pick:{student_name}")])
        await query.edit_message_text(f"Remove which penalty for *{_md(student_name)}*?",
                                       parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if action == "setpayment_direct":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Paid", callback_data="admin_setpayment_execute:Paid"),
                InlineKeyboardButton("🎓 Scholarship", callback_data="admin_setpayment_execute:Scholarship"),
            ],
            [
                InlineKeyboardButton("⏳ Pending", callback_data="admin_setpayment_execute:Pending"),
                InlineKeyboardButton("🚫 Cancel", callback_data="admin_setpayment_execute:Cancel"),
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data=f"admin_pick:{student_name}")],
        ])
        await query.edit_message_text("Set status to:", reply_markup=keyboard)
        return
    
    if action == "removestudent":
        rows = await asyncio.to_thread(sheets.get_roster_rows_for_student, student_name, False)
        active_rows = [r for r in rows if r["status"].lower() == "active"]
        if not active_rows:
            await query.edit_message_text(f"{student_name} has no active roster entries to remove.")
            return
        if len(active_rows) == 1:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, remove", callback_data=f"admin_removestudent_confirm:{active_rows[0]['row_number']}")],
                [InlineKeyboardButton("⬅️ Cancel", callback_data="admin_menu:cancel")],
            ])
            await query.edit_message_text(
                f"Remove *{_md(student_name)}* from {active_rows[0]['group']}?\n\n"
                f"This keeps all their history — it just marks them inactive.",
                parse_mode="Markdown", reply_markup=keyboard,
            )
        else:
            buttons = [
                [InlineKeyboardButton(f"Remove from {r['group']}", callback_data=f"admin_removestudent_confirm:{r['row_number']}")]
                for r in active_rows
            ]
            buttons.append([InlineKeyboardButton("⬅️ Cancel", callback_data="admin_menu:cancel")])
            await query.edit_message_text(
                f"*{_md(student_name)}* is enrolled in multiple groups — remove from which one?",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons),
            )
        return

    if action == "edittuition":
        context.user_data["pending_admin_action"] = {"type": "edittuition_amount", "student": student_name}
        await query.edit_message_text(f"New tuition for *{_md(student_name)}*:", parse_mode="Markdown",
                                       reply_markup=_tuition_amount_keyboard())
        return
    
async def admin_removestudent_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return
    _, _, row_str = query.data.partition(":")
    await asyncio.to_thread(sheets.set_student_status, int(row_str), "inactive")
    await query.edit_message_text("Student removed (marked inactive — history preserved).")


async def admin_removegroup_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return
    _, _, group_name = query.data.partition(":")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, remove it", callback_data=f"admin_removegroup_confirm:{group_name}")],
        [InlineKeyboardButton("⬅️ Cancel", callback_data="admin_menu:cancel")],
    ])
    await query.edit_message_text(f"Remove group *{_md(group_name)}*? This can't be undone from here.",
                                   parse_mode="Markdown", reply_markup=keyboard)


async def admin_removegroup_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return
    _, _, group_name = query.data.partition(":")
    removed = await asyncio.to_thread(sheets.remove_group, group_name)
    await query.edit_message_text(
        f"Removed {group_name}." if removed else f"Couldn't find {group_name} — it may have already been removed."
    )


def _tuition_amount_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("$89", callback_data="admin_tuition_preset:89"),
            InlineKeyboardButton("$119", callback_data="admin_tuition_preset:119"),
        ],
        [InlineKeyboardButton("✏️ Custom amount", callback_data="admin_tuition_preset:custom")],
        [InlineKeyboardButton("⬅️ Cancel", callback_data="admin_menu:cancel")],
    ])


async def _finalize_addstudent(name: str, tg: str, group: str, amount) -> str:
    await asyncio.to_thread(sheets.add_roster_student, name, tg, group)
    try:
        await asyncio.to_thread(sheets.add_finance_student, group, name, tg, amount, "Pending")
    except Exception as exc:
        log.warning("add_finance_student failed for %s in %s: %s", name, group, exc)
        return (
            f"Added {name} to Roster and {group}'s attendance tab, but couldn't create their "
            f"Finance row automatically ({exc})."
        )
    return f"Added {name}" + (f" (@{tg})" if tg else " (no TG handle yet)") + f" to {group}, tuition ${amount} (Pending)."

async def _finalize_edittuition(student_name: str, amount) -> str:
    rec = await asyncio.to_thread(sheets.find_student, student_name)
    if not rec:
        return f"⚠️ No payment record found for {student_name}."
    await asyncio.to_thread(sheets.set_finance_amount, rec["worksheet"], rec["row_number"], amount)
    await asyncio.to_thread(sheets.log_payment_change, rec["name"], rec["tg"], rec["status"], "admin_override_tuition", amount)
    return f"Updated {student_name}'s tuition to ${amount}."

async def admin_tuition_preset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return
    _, _, code = query.data.partition(":")
    pending = context.user_data.get("pending_admin_action")
    if not pending or pending.get("type") not in ("addstudent_amount", "edittuition_amount"):
        await query.edit_message_text("That selection expired — start over with /admin.")
        return
    if code == "custom":
        pending["awaiting_custom_amount"] = True
        context.user_data["pending_admin_action"] = pending
        await query.edit_message_text("Send the amount as a number (e.g. \"75\").")
        return
    amount = int(code)
    if pending["type"] == "addstudent_amount":
        result_msg = await _finalize_addstudent(pending["name"], pending["tg"], pending["group"], amount)
    else:
        result_msg = await _finalize_edittuition(pending["student"], amount)
    context.user_data.pop("pending_admin_action", None)
    await query.edit_message_text(result_msg)

async def admin_addstudent_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return
    pending = context.user_data.get("pending_admin_action")
    if not pending or pending.get("type") != "addstudent_pick_group":
        await query.edit_message_text("That selection expired — start over with /admin.")
        return
    _, _, group_name = query.data.partition(":")
    context.user_data["pending_admin_action"] = {
        "type": "addstudent_amount", "name": pending["name"], "tg": pending["tg"], "group": group_name,
    }
    await query.edit_message_text(f"What's {pending['name']}'s tuition for {group_name}?",
                                   reply_markup=_tuition_amount_keyboard())

async def admin_setpayment_execute_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return

    student_name = context.user_data.get("selected_student")
    if not student_name:
        await query.edit_message_text("No student selected — start over with /admin.")
        return

    _, _, status = query.data.partition(":")

    rec = await asyncio.to_thread(sheets.find_student, student_name)
    if not rec:
        buttons = [[InlineKeyboardButton("⬅️ Back", callback_data=f"admin_pick:{student_name}")]]
        await query.edit_message_text(
            f"⚠️ No payment record found for {student_name} in the payment sheet at all — "
            f"they may be missing a row there entirely.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    target_username = rec["tg"].lstrip("@")
    success, msg = await set_payment_status(context.bot, target_username, status, source="admin_override")
    result_msg = (f"✅ {msg}" if success else f"⚠️ {msg}")
    buttons = [[InlineKeyboardButton("⬅️ Back", callback_data=f"admin_pick:{student_name}")]]
    await query.edit_message_text(result_msg, reply_markup=InlineKeyboardMarkup(buttons))


async def admin_sendreminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return

    _, _, student_name = query.data.partition(":")
    profile = await asyncio.to_thread(sheets.get_student_profile, student_name)
    if not profile or not profile.get("tg"):
        await query.edit_message_text(f"{student_name} has no TG handle on file — can't send a reminder.")
        return

    success, msg = await jobs.send_manual_reminder(context.bot, profile["tg"].lstrip("@"))
    result_msg = (f"✅ {msg}" if success else f"⚠️ {msg}")
    buttons = [[InlineKeyboardButton("⬅️ Back", callback_data=f"admin_pick:{student_name}")]]
    await query.edit_message_text(result_msg, reply_markup=InlineKeyboardMarkup(buttons))


async def admin_penalty_preset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return

    student_name = context.user_data.get("selected_student")
    if not student_name:
        await query.edit_message_text("No student selected — start over with /admin.")
        return

    _, _, preset_key = query.data.partition(":")

    if preset_key == "custom":
        context.user_data["pending_admin_action"] = {"type": "addpenalty_custom", "student": student_name}
        await query.edit_message_text(
            f"Adding a custom penalty for *{_md(student_name)}*.\n\n"
            f"Send it as: `<points> <reason text>` — e.g. `2 Disrupting class`",
            parse_mode="Markdown",
        )
        return

    reason, points = _PENALTY_PRESETS[preset_key]
    profile = await asyncio.to_thread(sheets.get_student_profile, student_name)
    group = profile["group"] if profile else ""
    try:
        await asyncio.to_thread(sheets.add_manual_penalty, student_name, group, reason, points, "Admin")
        msg = f"✅ Added: {reason} (-{points}) for {student_name}."
    except Exception as e:
        msg = f"⚠️ Error adding penalty: {str(e)}"

    buttons = [[InlineKeyboardButton("⬅️ Back", callback_data=f"admin_pick:{student_name}")]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(buttons))


async def admin_removepenalty_row_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return

    student_name = context.user_data.get("selected_student")
    _, _, row_str = query.data.partition(":")

    try:
        await asyncio.to_thread(sheets.remove_admin_panel_penalty, int(row_str))
        msg = "✅ Penalty removed."
    except Exception as e:
        msg = f"⚠️ Error removing penalty: {str(e)}"

    buttons = [[InlineKeyboardButton("⬅️ Back", callback_data=f"admin_pick:{student_name}")]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(buttons))


async def admin_setpayment_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return

    pending = context.user_data.get("pending_admin_action")
    if not pending or pending.get("type") != "setpayment":
        await query.edit_message_text("That selection expired — start over with /admin.")
        return

    _, _, student_name = query.data.partition(":")
    profile = await asyncio.to_thread(sheets.get_student_profile, student_name)
    if not profile or not profile.get("tg"):
        await query.edit_message_text(f"{student_name} has no TG handle on file — can't set payment status.")
        context.user_data.pop("pending_admin_action", None)
        return

    target_username = profile["tg"].lstrip("@")
    success, msg = await set_payment_status(context.bot, target_username, pending["status"], source="admin_override")

    result_msg = (f"✅ {msg}" if success else f"⚠️ {msg}")
    buttons = [[InlineKeyboardButton("⬅️ Back", callback_data=f"admin_pick:{student_name}")]]
    await query.edit_message_text(result_msg, reply_markup=InlineKeyboardMarkup(buttons))
    context.user_data.pop("pending_admin_action", None)


async def admin_setpayment_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin_update(update):
        return

    _, _, status_label = query.data.partition(":")
    context.user_data["pending_admin_action"] = {"type": "setpayment", "status": status_label}
    await query.edit_message_text(
        f"Setting status to *{status_label}*.\n\n"
        f"Now send the student's name or @username (partial is fine — I'll match it).",
        parse_mode="Markdown",
    )


async def admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return

    pending = context.user_data.get("pending_admin_action")
    if not pending:
        return

    text = update.message.text.strip()

    if pending.get("awaiting_custom_amount") and pending.get("type") in ("addstudent_amount", "edittuition_amount"):
        try:
            amount = int(float(text.replace("$", "").strip()))
        except ValueError:
            await update.message.reply_text("Couldn't parse that as a number — send just the amount, e.g. \"75\".")
            return
        if pending["type"] == "addstudent_amount":
            result_msg = await _finalize_addstudent(pending["name"], pending["tg"], pending["group"], amount)
        else:
            result_msg = await _finalize_edittuition(pending["student"], amount)
        await update.message.reply_text(result_msg)
        context.user_data.pop("pending_admin_action", None)
        return
    
    if pending["type"] == "setpayment":
        matches = await asyncio.to_thread(sheets.search_roster, text)
        if not matches:
            await update.message.reply_text(f"No student found matching '{text}'. Try again or /admin to restart.")
            return
        if len(matches) == 1:
            target_username = matches[0]["tg"].lstrip("@")
            if not target_username:
                await update.message.reply_text(f"{matches[0]['name']} has no TG handle on file — can't set payment status.")
                context.user_data.pop("pending_admin_action", None)
                return
            success, msg = await set_payment_status(context.bot, target_username, pending["status"], source="admin_override")
            await update.message.reply_text((f"✅ {msg}" if success else f"⚠️ {msg}"))
        else:
            buttons = [
                [InlineKeyboardButton(f"{m['name']} ({m['group']})", callback_data=f"admin_setpayment_pick:{m['name']}")]
                for m in matches
            ]
            await update.message.reply_text(
                f"Found {len(matches)} matches — which one?", reply_markup=InlineKeyboardMarkup(buttons)
            )
            return
    elif pending["type"] == "search_student":
        matches = await asyncio.to_thread(sheets.search_roster, text)
        if not matches:
            await update.message.reply_text(f"No matches for '{text}'.")
        else:
            buttons = [[InlineKeyboardButton(f"{m['name']} ({m['group']})", callback_data=f"admin_pick:{m['name']}")]
                       for m in matches]
            await update.message.reply_text(
                f"Found {len(matches)} match(es):", reply_markup=InlineKeyboardMarkup(buttons)
            )

    elif pending["type"] == "addpenalty_custom":
        student_name = pending["student"]
        parts = text.split(maxsplit=1)
        if len(parts) != 2 or not parts[0].lstrip("-").isdigit():
            await update.message.reply_text("Couldn't parse that — send it as: `<points> <reason text>`", parse_mode="Markdown")
            return
        points, reason = int(parts[0]), parts[1]
        profile = await asyncio.to_thread(sheets.get_student_profile, student_name)
        group = profile["group"] if profile else ""
        try:
            await asyncio.to_thread(sheets.add_manual_penalty, student_name, group, reason, points, "Admin")
            await update.message.reply_text(f"✅ Added: {reason} ({points:+d}) for {student_name}.")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Error adding penalty: {str(e)}")
    
    elif pending["type"] == "addstudent_name":
        context.user_data["pending_admin_action"] = {"type": "addstudent_tg", "name": text}
        await update.message.reply_text(
            f"Name: {text}\n\nNow send their @username (or type \"none\" if they don't have one yet)."
        )
        return

    elif pending["type"] == "addstudent_tg":
        tg = "" if text.lower() == "none" else text.lstrip("@")
        groups = await asyncio.to_thread(sheets.get_groups_schedule)
        if not groups:
            await update.message.reply_text("No groups exist yet — add a group first.")
            return
        buttons = [
            [InlineKeyboardButton(g["name"], callback_data=f"admin_addstudent_group:{g['name']}")]
            for g in groups
        ]
        context.user_data["pending_admin_action"] = {"type": "addstudent_pick_group", "name": pending["name"], "tg": tg}
        await update.message.reply_text("Which group?", reply_markup=InlineKeyboardMarkup(buttons))
        return

    elif pending["type"] == "addgroup_name":
        context.user_data["pending_admin_action"] = {"type": "addgroup_days", "name": text}
        await update.message.reply_text(
            f"Group name: {text}\n\nNow send the days it meets, comma-separated "
            f"(use Mon/Tue/Wed/Thu/Fri/Sat/Sun) — e.g. \"Mon,Wed,Fri\"."
        )
        return

    elif pending["type"] == "addgroup_days":
        context.user_data["pending_admin_action"] = {"type": "addgroup_start", "name": pending["name"], "days": text}
        await update.message.reply_text("Now send the start time (24h HH:MM, e.g. \"16:00\").")
        return

    elif pending["type"] == "addgroup_start":
        context.user_data["pending_admin_action"] = {
            "type": "addgroup_end", "name": pending["name"], "days": pending["days"], "start": text
        }
        await update.message.reply_text("Now send the end time (24h HH:MM, e.g. \"18:00\").")
        return

    elif pending["type"] == "addgroup_end":
        await asyncio.to_thread(sheets.add_group, pending["name"], pending["days"], pending["start"], text)
        await update.message.reply_text(
            f"Group \"{pending['name']}\" created ({pending['days']}, {pending['start']}–{text}). "
            f"Its attendance tab is ready — add students to it with ➕ Add student."
        )

    context.user_data.pop("pending_admin_action", None)




def _report_timeframe(code: str):
    today = datetime.date.today()
    if code == "week":
        start = today - datetime.timedelta(days=today.weekday())  # Monday
        return start, today, "This week"
    if code == "month":
        return today.replace(day=1), today, "This month"
    return datetime.date(2020, 1, 1), today, "All time"


def _format_group_report(group_name: str, timeframe_label: str, data: dict) -> str:
    p, a, h = data["payment"], data["attendance"], data["homework"]
    lines = [
        f"📊 *{_md(group_name)}* — {timeframe_label}\n",
        f"💰 Payment: Paid {p['Paid']} | Pending {p['Pending']} | Scholarship {p['Scholarship']} | Cancel {p['Cancel']}",
        f"📅 Attendance: Present {a['Present']} | Late {a['Late']} | Absent {a['Absent']}",
        f"📚 Homework: On time {h['On Time']} | Late {h['Late']} | Missing {h['Missing']}",
        f"⚠️ Penalty points assigned: {data['penalty_points_period']}",
    ]
    return "\n".join(lines)


async def admin_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("This week", callback_data="admin_report_tf:week")],
        [InlineKeyboardButton("This month", callback_data="admin_report_tf:month")],
        [InlineKeyboardButton("All time", callback_data="admin_report_tf:all")],
        [InlineKeyboardButton("❌ Cancel", callback_data="admin_menu:cancel")],
    ])
    await update.message.reply_text("📊 Report — pick a timeframe:", reply_markup=keyboard)


async def admin_report_timeframe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return
    _, _, code = query.data.partition(":")
    context.user_data["report_timeframe"] = code

    groups = await asyncio.to_thread(sheets.get_groups_schedule)
    buttons = [[InlineKeyboardButton("📊 All groups", callback_data="admin_report_group:__all__")]]
    buttons += [[InlineKeyboardButton(g["name"], callback_data=f"admin_report_group:{g['name']}")] for g in groups]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="admin_menu:cancel")])
    await query.edit_message_text("Which group?", reply_markup=InlineKeyboardMarkup(buttons))

async def admin_report_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return
    _, _, group_name = query.data.partition(":")
    code = context.user_data.get("report_timeframe", "week")
    start_date, end_date, label = _report_timeframe(code)

    groups = await asyncio.to_thread(sheets.get_groups_schedule)
    all_names = [g["name"] for g in groups]
    reports = await asyncio.to_thread(sheets.get_all_groups_report, all_names, start_date, end_date)

    if group_name == "__all__":
        text = "\n\n".join(_format_group_report(g, label, reports[g]) for g in all_names) if all_names else "No groups found."
    else:
        text = _format_group_report(group_name, label, reports.get(group_name, {
            "payment": {"Paid": 0, "Pending": 0, "Scholarship": 0, "Cancel": 0},
            "attendance": {"Present": 0, "Late": 0, "Absent": 0},
            "homework": {"On Time": 0, "Late": 0, "Missing": 0},
            "penalty_points_period": 0,
        }))

    await safe_edit_text(query, text, parse_mode="Markdown")

async def admin_setpayment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return

    valid_statuses = {"paid": "Paid", "scholarship": "Scholarship", "pending": "Pending", "cancel": "Cancel"}
    if len(context.args) < 2 or context.args[-1].lower() not in valid_statuses:
        await update.message.reply_text("Usage: /admin_setpayment <name or @username> <paid|scholarship|pending|cancel>")
        return

    status_label = valid_statuses[context.args[-1].lower()]
    query_text = " ".join(context.args[:-1])

    matches = await asyncio.to_thread(sheets.search_roster, query_text)
    if not matches:
        await update.message.reply_text(f"No student found matching '{query_text}'.")
        return
    if len(matches) > 1:
        buttons = [
            [InlineKeyboardButton(f"{m['name']} ({m['group']})", callback_data=f"admin_setpayment_pick:{m['name']}")]
            for m in matches
        ]
        context.user_data["pending_admin_action"] = {"type": "setpayment", "status": status_label}
        await update.message.reply_text(f"Found {len(matches)} matches — which one?",
                                         reply_markup=InlineKeyboardMarkup(buttons))
        return

    target_username = matches[0]["tg"].lstrip("@")
    if not target_username:
        await update.message.reply_text(f"{matches[0]['name']} has no TG handle on file — can't set payment status.")
        return

    success, msg = await set_payment_status(context.bot, target_username, status_label, source="admin_override")
    await update.message.reply_text(msg if success else f"⚠️ {msg}")


async def proof_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin_update(update):
        return

    action, _, target_username = query.data.partition(":")
    bd_entry = await asyncio.to_thread(sheets.find_bot_data_row, target_username)

    base_caption = query.message.caption or ""

    if not bd_entry or not bd_entry.get("chat_id"):
        await query.edit_message_caption(caption=base_caption + "\n\n⚠️ No linked chat found.")
        return

    if action == "reject":
        await asyncio.to_thread(sheets.clear_payment_proof, target_username)
        await notify_student(context.bot, int(bd_entry["chat_id"]), text=messages.proof_rejected())
        await query.edit_message_caption(caption=base_caption + "\n\n❌ Rejected")

    elif action == "approve":
        success, _ = await approve_payment(context.bot, target_username, source="proof")
        suffix = "\n\n✅ Approved" if success else "\n\n⚠️ Approval failed — check logs."
        await query.edit_message_caption(caption=base_caption + suffix)


async def approve_payment(bot, target_username: str, source: str) -> tuple[bool, str]:
    return await set_payment_status(bot, target_username, "Paid", source=source)


async def admin_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚠️ Yes, wipe attendance/homework/penalties", callback_data="admin_reset_confirm")],
        [InlineKeyboardButton("❌ Cancel", callback_data="admin_menu:cancel")],
    ])
    await update.message.reply_text(
        "This will permanently clear ALL attendance, homework, and penalty history "
        "(Roster and payment history are kept). Are you sure?",
        reply_markup=keyboard,
    )


async def admin_reset_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return
    await asyncio.to_thread(sheets.reset_all_logs)
    await query.edit_message_text("✅ Attendance, homework, and penalty logs have been cleared. Starting fresh from zero.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled exception while processing update: %s", update, exc_info=context.error)
    error_text = f"⚠️ Bot error: {type(context.error).__name__}: {context.error}"

    # Always tell the admin group.
    if config.ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=config.ADMIN_CHAT_ID, text=error_text)
        except Exception:
            pass

    # Also tell whoever actually triggered it, if that's a different chat
    # (e.g. an admin testing from their own DM), so it's not silently
    # invisible to them.
    if isinstance(update, Update):
        origin_chat_id = update.effective_chat.id if update.effective_chat else None
        if origin_chat_id and str(origin_chat_id) != str(config.ADMIN_CHAT_ID):
            try:
                await context.bot.send_message(chat_id=origin_chat_id, text=error_text)
            except Exception:
                pass


async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    username = user.username

    if not username:
        await update.message.reply_text(messages.welcome_no_username())
        return

    rec = await asyncio.to_thread(sheets.find_student, username)
    if not rec:
        await update.message.reply_text(messages.status_not_found())
        return

    if rec["status"].strip().lower() in config.PAID_STATUSES:
        await update.message.reply_text("✅ You're already marked as paid. No action needed.")
        return

    caption = messages.pay_info(rec["name"], rec["amount"])
    with open(config.PAYME_QR_FILE, "rb") as photo:
        await update.message.reply_photo(photo=photo, caption=caption)
    await asyncio.to_thread(sheets.mark_pay_shown, username)


# --- Scheduler lifecycle ---------------------------------------------------
async def on_startup(app: Application):
    await set_bot_commands(app)
    log.info("DEBUG: ADMIN_CHAT_ID=%r ADMIN_USER_IDS=%r", config.ADMIN_CHAT_ID, config.ADMIN_USER_IDS)

    scheduler = AsyncIOScheduler()
    if config.REMINDER_INTERVAL_MINUTES > 0:
        reminder_trigger = IntervalTrigger(minutes=config.REMINDER_INTERVAL_MINUTES)
    else:
        reminder_trigger = IntervalTrigger(days=config.REMINDER_INTERVAL_DAYS)

    scheduler.add_job(
        jobs.run_reminders,
        trigger=reminder_trigger,
        args=[app.bot],
        id="reminders",
        name="Payment reminders",
        replace_existing=True,
    )
    scheduler.add_job(
        jobs.run_payment_check,
        trigger=CronTrigger(
            hour=config.PAYMENT_CHECK_HOUR, minute=config.PAYMENT_CHECK_MINUTE
        ),
        args=[app.bot],
        id="payment_check",
        name="Payment confirmation check",
        replace_existing=True,
    )
    scheduler.add_job(
        jobs.run_attendance_alerts,
        trigger=IntervalTrigger(minutes=jobs.JOB_INTERVAL_MINUTES),
        args=[app.bot],
        id="attendance_alerts",
        name="Attendance alerts (10 min after session start)",
        replace_existing=True,
    )
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    log.info(
        "Scheduler started: reminders every %d day(s), payment check daily at %02d:%02d",
        config.REMINDER_INTERVAL_DAYS,
        config.PAYMENT_CHECK_HOUR,
        config.PAYMENT_CHECK_MINUTE,
    )


async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /reject <username>")
        return

    target_username = context.args[0].lstrip("@")
    rec = await asyncio.to_thread(sheets.find_student, target_username)
    bd_entry = await asyncio.to_thread(sheets.find_bot_data_row, target_username)

    if not bd_entry or not bd_entry.get("chat_id"):
        await update.message.reply_text(f"No linked chat found for @{target_username}.")
        return

    await asyncio.to_thread(sheets.clear_payment_proof, target_username)

    await notify_student(context.bot, int(bd_entry["chat_id"]), text=messages.proof_rejected())
    await update.message.reply_text(f"Rejected proof for @{target_username}, they've been notified.")


async def on_shutdown(app: Application):
    scheduler = app.bot_data.get("scheduler")
    if scheduler:
        scheduler.shutdown(wait=False)
        log.info("Scheduler stopped")


# --- Attendance Marking (Teacher UI) ----------------------------------------

_STATUS_EMOJI = {"Present": "✅", "Late": "⏰", "Excused": "✋", "Absent": "❌"}
_STATUS_ORDER = ["Present", "Late", "Excused", "Absent"]


async def attendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    await _show_attendance_group_picker(update.message.reply_text)


async def _show_attendance_group_picker(send):
    groups = await asyncio.to_thread(sheets.get_groups_schedule)
    buttons = [[InlineKeyboardButton(g["name"], callback_data=f"attendance_group:{g['name']}")] for g in groups]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="attendance:cancel")])
    await send("👥 Mark attendance — pick a group:", reply_markup=InlineKeyboardMarkup(buttons))


async def attendance_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return

    context.user_data.pop("attendance_group", None)
    context.user_data.pop("attendance_date", None)
    context.user_data.pop("attendance_students", None)
    context.user_data.pop("attendance_marks", None)

    await query.edit_message_text("Cancelled.")


async def attendance_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return

    _, _, group_name = query.data.partition(":")
    context.user_data["attendance_group"] = group_name

    import datetime
    today = datetime.date.today()

    last_session = await asyncio.to_thread(sheets.get_last_session_date, group_name)

    buttons = []
    if last_session:
        label_date = datetime.datetime.strptime(last_session, "%Y-%m-%d").date()
        buttons.append([InlineKeyboardButton(
            f"🕐 Last session ({label_date.strftime('%a %b %d')})",
            callback_data=f"attendance_date:{last_session}"
        )])

    for offset in range(4):
        date = today + datetime.timedelta(days=offset)
        date_str = date.isoformat()
        label = "Today" if offset == 0 else date.strftime("%a %b %d")
        buttons.append([InlineKeyboardButton(label, callback_data=f"attendance_date:{date_str}")])

    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="attendance:back_to_groups")])

    await query.edit_message_text(
        f"👥 {group_name}\n\n📅 Pick a date:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def attendance_back_to_groups_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return
    context.user_data.pop("attendance_group", None)
    await _show_attendance_group_picker(query.edit_message_text)


def _build_attendance_keyboard(students: list, marks: dict) -> list:
    buttons = []
    for idx, student in enumerate(students):
        buttons.append([InlineKeyboardButton(f"👤 {student['name']}", callback_data="noop")])

        status = marks.get(idx)
        if status:
            emoji = _STATUS_EMOJI.get(status, "❓")
            buttons.append([
                InlineKeyboardButton(f"{emoji} {status}", callback_data="noop"),
                InlineKeyboardButton("↩️ Change", callback_data=f"attendance_change:{idx}"),
            ])
        else:
            row = []
            for s in _STATUS_ORDER:
                row.append(InlineKeyboardButton(f"{_STATUS_EMOJI[s]} {s}", callback_data=f"attendance_mark:{idx}:{s}"))
            buttons.append(row)

    marked_count = len(marks)
    buttons.append([InlineKeyboardButton(f"✅ Submit ({marked_count}/{len(students)})", callback_data="attendance_submit")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="attendance:back_to_groups")])
    return buttons


async def attendance_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return

    _, _, date_str = query.data.partition(":")
    context.user_data["attendance_date"] = date_str
    group_name = context.user_data.get("attendance_group")

    students = await asyncio.to_thread(sheets.get_students_for_group, group_name)
    if not students:
        await query.edit_message_text(f"No students found in {group_name}.")
        return

    current_attendance = await asyncio.to_thread(sheets.get_attendance_for_date_group, date_str, group_name)

    context.user_data["attendance_students"] = students
    marks = {}
    for idx, student in enumerate(students):
        existing = current_attendance.get(student["name"], "")
        if existing in _STATUS_EMOJI:
            marks[idx] = existing
    context.user_data["attendance_marks"] = marks

    await query.edit_message_text(
        f"👥 {group_name} | 📅 {date_str}\n\nTap a status for each student:",
        reply_markup=InlineKeyboardMarkup(_build_attendance_keyboard(students, marks))
    )


async def attendance_mark_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin_update(update):
        await query.answer()
        return

    _, idx_str, status = query.data.split(":", 2)
    idx = int(idx_str)

    students = context.user_data.get("attendance_students")
    marks = context.user_data.get("attendance_marks")
    if students is None or marks is None:
        await query.answer("⚠️ Session lost — start over with /attendance", show_alert=True)
        return

    marks[idx] = status
    emoji = _STATUS_EMOJI.get(status, "❓")
    student_name = students[idx]["name"] if idx < len(students) else "Student"
    await query.answer(f"{emoji} {student_name} marked {status}")

    group_name = context.user_data.get("attendance_group")
    date_str = context.user_data.get("attendance_date")
    await query.edit_message_text(
        f"👥 {group_name} | 📅 {date_str}\n\nTap a status for each student:",
        reply_markup=InlineKeyboardMarkup(_build_attendance_keyboard(students, marks))
    )


async def attendance_change_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin_update(update):
        await query.answer()
        return

    _, _, idx_str = query.data.partition(":")
    idx = int(idx_str)

    students = context.user_data.get("attendance_students")
    marks = context.user_data.get("attendance_marks")
    if students is None or marks is None:
        await query.answer("⚠️ Session lost — start over with /attendance", show_alert=True)
        return

    marks.pop(idx, None)
    await query.answer()

    group_name = context.user_data.get("attendance_group")
    date_str = context.user_data.get("attendance_date")
    await query.edit_message_text(
        f"👥 {group_name} | 📅 {date_str}\n\nTap a status for each student:",
        reply_markup=InlineKeyboardMarkup(_build_attendance_keyboard(students, marks))
    )


async def attendance_submit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return

    students = context.user_data.get("attendance_students")
    marks = context.user_data.get("attendance_marks")
    group_name = context.user_data.get("attendance_group")
    date_str = context.user_data.get("attendance_date")

    if not students or not marks:
        await query.answer("No attendance marked", show_alert=True)
        return

    failed = []
    for idx, status in marks.items():
        student_name = students[idx]["name"]
        success = await asyncio.to_thread(
            sheets.mark_attendance, date_str, group_name, student_name, status
        )
        if not success:
            failed.append(student_name)
        else:
            log.info(f"Attendance submitted: {student_name} ({group_name}) on {date_str} → {status}")

    summary = {s: sum(1 for v in marks.values() if v == s) for s in _STATUS_ORDER}
    unmarked = len(students) - len(marks)

    report_lines = [f"✅ Submitted {len(marks) - len(failed)} of {len(students)} student(s)\n"]
    report_lines.append("📊 Summary:")
    for s in _STATUS_ORDER:
        report_lines.append(f"  {_STATUS_EMOJI[s]} {s}: {summary[s]}")
    if unmarked:
        report_lines.append(f"  ⬜ Not marked: {unmarked}")
    if failed:
        report_lines.append(f"\n⚠️ Failed to save: {', '.join(failed)}")

    buttons = [
        [InlineKeyboardButton("📅 Mark another date/group", callback_data="attendance:back_to_groups")],
    ]
    await query.edit_message_text("\n".join(report_lines), reply_markup=InlineKeyboardMarkup(buttons))

    context.user_data.pop("attendance_group", None)
    context.user_data.pop("attendance_date", None)
    context.user_data.pop("attendance_students", None)
    context.user_data.pop("attendance_marks", None)


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()


def main():
    problems = config.validate()
    fatal = [p for p in problems if "TELEGRAM_BOT_TOKEN" in p or "credentials.json" in p]
    for p in problems:
        log.warning("Config: %s", p)
    if fatal:
        log.error("Cannot start — fix the above and try again.")
        return

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("penalty", penalty))
    app.add_handler(CommandHandler("reject", reject))
    app.add_handler(CommandHandler("admin_setpayment", admin_setpayment))
    app.add_handler(CommandHandler("admin", admin_menu))
    app.add_handler(CommandHandler("attendance", attendance))
    app.add_handler(MessageHandler(filters.PHOTO, photo))
    app.add_handler(CommandHandler("pay", pay))

    app.add_handler(CallbackQueryHandler(attendance_group_callback, pattern=r"^attendance_group:"))
    app.add_handler(CallbackQueryHandler(attendance_date_callback, pattern=r"^attendance_date:"))
    app.add_handler(CallbackQueryHandler(attendance_mark_callback, pattern=r"^attendance_mark:"))
    app.add_handler(CallbackQueryHandler(attendance_change_callback, pattern=r"^attendance_change:"))
    app.add_handler(CallbackQueryHandler(attendance_submit_callback, pattern=r"^attendance_submit$"))
    app.add_handler(CallbackQueryHandler(attendance_back_to_groups_callback, pattern=r"^attendance:back_to_groups$"))
    app.add_handler(CallbackQueryHandler(attendance_cancel_callback, pattern=r"^attendance:cancel$"))
    app.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))
    app.add_error_handler(error_handler)
    app.add_handler(CallbackQueryHandler(admin_menu_callback, pattern=r"^admin_menu:"))
    app.add_handler(CallbackQueryHandler(admin_group_callback, pattern=r"^admin_group:"))
    app.add_handler(CallbackQueryHandler(admin_pick_student_callback, pattern=r"^admin_pick:"))
    app.add_handler(CallbackQueryHandler(admin_action_callback, pattern=r"^admin_action:"))
    app.add_handler(CommandHandler("admin_report", admin_report))
    app.add_handler(CallbackQueryHandler(admin_report_timeframe_callback, pattern=r"^admin_report_tf:"))
    app.add_handler(CommandHandler("admin_reset", admin_reset))
    app.add_handler(CallbackQueryHandler(admin_reset_confirm_callback, pattern=r"^admin_reset_confirm$"))
    app.add_handler(CallbackQueryHandler(admin_report_group_callback, pattern=r"^admin_report_group:"))
    app.add_handler(CallbackQueryHandler(admin_removestudent_confirm_callback, pattern=r"^admin_removestudent_confirm:"))
    app.add_handler(CallbackQueryHandler(admin_removegroup_pick_callback, pattern=r"^admin_removegroup_pick:"))
    app.add_handler(CallbackQueryHandler(admin_removegroup_confirm_callback, pattern=r"^admin_removegroup_confirm:"))
    app.add_handler(CallbackQueryHandler(admin_addstudent_group_callback, pattern=r"^admin_addstudent_group:"))
    app.add_handler(CallbackQueryHandler(admin_tuition_preset_callback, pattern=r"^admin_tuition_preset:"))
    app.add_handler(CallbackQueryHandler(admin_sendreminder_callback, pattern=r"^admin_sendreminder:"))
    app.add_handler(CallbackQueryHandler(admin_penalty_preset_callback, pattern=r"^admin_penalty_preset:"))
    app.add_handler(CallbackQueryHandler(admin_removepenalty_row_callback, pattern=r"^admin_removepenalty_row:"))
    app.add_handler(CallbackQueryHandler(admin_setpayment_status_callback, pattern=r"^admin_setpayment_status:"))
    app.add_handler(CallbackQueryHandler(admin_setpayment_pick_callback, pattern=r"^admin_setpayment_pick:"))
    app.add_handler(CallbackQueryHandler(proof_decision, pattern=r"^(approve|reject):"))
    app.add_handler(CallbackQueryHandler(admin_setpayment_execute_callback, pattern=r"^admin_setpayment_execute:"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text_input))

    log.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()