"""Telegram bot entrypoint.

Registers the /start, /status, and /penalty command handlers, and wires up the
two APScheduler jobs (reminders + payment confirmation) to run alongside the
bot's async event loop.

Run:  ./venv/bin/python bot.py
"""
import asyncio
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
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler
import config
import jobs
import messages
import sheets
from notify import notify_student

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


async def safe_edit_text(query, text, **kwargs):
    """Wraps query.edit_message_text, swallowing Telegram's harmless
    'Message is not modified' BadRequest — this fires whenever an edit call
    sends the exact same text+buttons the message already has (e.g. a
    double-tap on a button before Telegram registers the first tap). Any
    other BadRequest is re-raised since that could be a real problem.
    """
    try:
        await query.edit_message_text(text, **kwargs)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def safe_edit_caption(query, caption, **kwargs):
    """Same as safe_edit_text, for edit_message_caption."""
    try:
        await query.edit_message_caption(caption=caption, **kwargs)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


# --- Command handlers ------------------------------------------------------
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

    # Match the sender against the group tabs (read-only).
    rec = await asyncio.to_thread(sheets.find_student, username)
    if not rec:
        await message.reply_text(messages.proof_no_record())
        return

    # Largest available photo size is the last entry.
    file_id = message.photo[-1].file_id
    date_str = sheets.today_str()

    try:
        # Forward the actual photo to the admin with context in the caption.
        # (Re-sending by file_id needs no download.)
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

        # Ensure a Bot Data row exists, then record the submission there.
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

PAYMENT_STATUSES = ["Paid", "Pending", "Scholarship", "Cancelled"]


async def set_payment_status(bot, target_username: str, status: str, source: str, amount=None) -> tuple[bool, str]:
    """Single entry point for changing a student's payment status to ANY of
    the real Finance-sheet values (Paid, Pending, Scholarship, Cancelled).

    This is the ONLY place that writes payment status to the Finance sheet
    — Apps Script never touches Finance directly. Every write here also
    logs to Payment_Log in the Admin Panel spreadsheet, with `amount` when
    given (e.g. a partial payment received while status is Pending) so the
    admin panel has a full paper trail without needing Finance access.

    Returns (success, message) for the caller to relay back to the admin.
    """
    rec = await asyncio.to_thread(sheets.find_student, target_username)
    if not rec:
        return False, f"No student record found for @{target_username}."

    bd_entry = await asyncio.to_thread(sheets.find_bot_data_row, target_username)

    await asyncio.to_thread(sheets.set_finance_status, rec["worksheet"], rec["row_number"], status)
    log_amount = amount if amount is not None else rec["amount"]
    await asyncio.to_thread(
        sheets.log_payment_change, rec["name"], target_username, status, source, log_amount
    )

    if bd_entry:
        await asyncio.to_thread(sheets.update_last_known_status, bd_entry["row_number"], status)
        if status.lower() == "paid" and bd_entry.get("chat_id"):
            try:
                await notify_student(
                    bot, int(bd_entry["chat_id"]), text=messages.payment_success(rec["name"])
                )
            except Exception as exc:
                log.warning("Could not notify @%s of payment approval: %s", target_username, exc)

    amount_note = f" (amount: {amount})" if amount is not None else ""
    return True, f"Set @{target_username} ({rec['name']}) to {status}{amount_note}."


async def set_bot_commands(app: Application):
    """Registers Telegram's native '/' autocomplete menu.

    Students see one set of commands with plain-language descriptions (no
    need to remember exact syntax — Telegram shows the description as they
    type). The admin chat sees an additional set on top, scoped so no other
    chat can see or use them.
    """
    student_commands = [
        BotCommand("start", "Register with the bot"),
        BotCommand("status", "Check your payment status"),
        BotCommand("pay", "Get payment details and QR code"),
        BotCommand("penalty", "Check your penalty points"),
    ]
    await app.bot.set_my_commands(student_commands, scope=BotCommandScopeDefault())

    if config.ADMIN_CHAT_ID:
        admin_commands = student_commands + [
            BotCommand("admin", "Open the admin menu"),
            BotCommand("admin_setpayment", "Set a student's payment status directly"),
            BotCommand("reject", "Reject a student's payment proof"),
        ]
        await app.bot.set_my_commands(
            admin_commands,
            scope=BotCommandScopeChat(chat_id=int(config.ADMIN_CHAT_ID)),
        )


# --- Guided admin menu (buttons instead of typed syntax) -------------------
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/admin — entry point to a button-driven menu, so the admin never has
    to remember exact command syntax."""
    if not is_admin_update(update):
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Set payment status", callback_data="admin_menu:setpayment")],
        [InlineKeyboardButton("📋 Browse roster", callback_data="admin_menu:browse")],
        [InlineKeyboardButton("🔍 Search student", callback_data="admin_menu:search")],
        [InlineKeyboardButton("➕ Add student", callback_data="admin_menu:addstudent")],
        [InlineKeyboardButton("➕ Add group", callback_data="admin_menu:addgroup")],
        [InlineKeyboardButton("🗑 Remove group", callback_data="admin_menu:removegroup")],
        [InlineKeyboardButton("❌ Cancel", callback_data="admin_menu:cancel")],
    ])
    await update.message.reply_text(
        "Admin menu — what would you like to do?", reply_markup=keyboard
    )


def _payment_status_keyboard(origin: str) -> InlineKeyboardMarkup:
    """origin is 'search' (top-level menu — student not yet known, will need
    to be found by name/username) or 'direct' (student already selected from
    a profile card — act on them immediately, no search needed).
    """
    rows = [
        [InlineKeyboardButton(status, callback_data=f"admin_setpayment_status:{status}:{origin}")]
        for status in PAYMENT_STATUSES
    ]
    rows.append([InlineKeyboardButton("⬅️ Cancel", callback_data="admin_menu:cancel")])
    return InlineKeyboardMarkup(rows)


async def admin_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles taps on the /admin menu buttons and the payment-status sub-menu."""
    query = update.callback_query
    await query.answer()

    if not is_admin_update(update):
        return

    _, _, action = query.data.partition(":")

    if action == "cancel":
        context.user_data.pop("pending_admin_action", None)
        await safe_edit_text(query, "Cancelled.")
        return

    if action == "setpayment":
        await safe_edit_text(query, "Set status to:", reply_markup=_payment_status_keyboard("search"))
        return

    if action == "browse":
        groups = await asyncio.to_thread(sheets.get_groups_schedule)
        buttons = [[InlineKeyboardButton(g["name"], callback_data=f"admin_group:{g['name']}")] for g in groups]
        buttons.append([InlineKeyboardButton("⬅️ Cancel", callback_data="admin_menu:cancel")])
        await safe_edit_text(query, "Pick a group:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if action == "search":
        context.user_data["pending_admin_action"] = {"type": "search_student"}
        await safe_edit_text(query, "Send part of a student's name or @username to search for.")
        return

    if action == "addstudent":
        context.user_data["pending_admin_action"] = {"type": "addstudent_name"}
        await safe_edit_text(query, "New student — send their full name.")
        return

    if action == "addgroup":
        context.user_data["pending_admin_action"] = {"type": "addgroup_name"}
        await safe_edit_text(query, "New group — send the group name (e.g. \"Padawan Offline 2\").")
        return

    if action == "removegroup":
        groups = await asyncio.to_thread(sheets.get_groups_schedule)
        if not groups:
            await safe_edit_text(query, "No groups found.")
            return
        buttons = [[InlineKeyboardButton(g["name"], callback_data=f"admin_removegroup_pick:{g['name']}")] for g in groups]
        buttons.append([InlineKeyboardButton("⬅️ Cancel", callback_data="admin_menu:cancel")])
        await safe_edit_text(query, 
            "Pick a group to remove.\n\n"
            "This only removes it from scheduling/syncing — students, attendance history, "
            "and logs are kept untouched.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if action.startswith("admin_setpayment_status"):
        return  # handled by the dedicated callback below


async def admin_removegroup_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirmation step before actually deleting a Groups row."""
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return
    _, _, group_name = query.data.partition(":")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, remove it", callback_data=f"admin_removegroup_confirm:{group_name}")],
        [InlineKeyboardButton("⬅️ Cancel", callback_data="admin_menu:cancel")],
    ])
    await safe_edit_text(query, f"Remove group *{group_name}*? This can't be undone from here.",
                                   parse_mode="Markdown", reply_markup=keyboard)


async def admin_removegroup_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return
    _, _, group_name = query.data.partition(":")
    removed = await asyncio.to_thread(sheets.remove_group, group_name)
    await safe_edit_text(query, 
        f"Removed {group_name}." if removed else f"Couldn't find {group_name} — it may have already been removed."
    )


async def admin_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """After picking a group from Browse Roster, show its students as buttons."""
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return

    _, _, group_name = query.data.partition(":")
    students = await asyncio.to_thread(sheets.get_roster_by_group, group_name)
    if not students:
        await safe_edit_text(query, f"No students found in {group_name}.")
        return

    buttons = [[InlineKeyboardButton(s["name"], callback_data=f"admin_pick:{s['name']}")] for s in students]
    buttons.append([InlineKeyboardButton("⬅️ Cancel", callback_data="admin_menu:cancel")])
    await safe_edit_text(query, f"{group_name} — pick a student:", reply_markup=InlineKeyboardMarkup(buttons))


async def admin_pick_student_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A student was picked (from browse or search) — show their action card."""
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
        [InlineKeyboardButton("⬅️ Cancel", callback_data="admin_menu:cancel")],
    ])
    await safe_edit_text(query, f"*{student_name}* — what would you like to do?",
                                   parse_mode="Markdown", reply_markup=keyboard)


def _format_student_profile(profile: dict) -> str:
    payment = profile["payment"]
    payment_line = (
        f"{payment['status']} (amount: {payment['amount']}, as of {payment['timestamp']})"
        if payment else "No payment record yet"
    )
    status_line = "" if profile.get("roster_status", "active").lower() == "active" else "\n🚫 *Status: inactive/removed*"
    return (
        f"👤 *{profile['name']}*{status_line}\n"
        f"Group: {profile['group']}\n"
        f"TG: @{profile['tg']}\n\n"
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
    """Handles the per-student action card buttons (profile/add/remove/payment)."""
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return

    student_name = context.user_data.get("selected_student")
    if not student_name:
        await safe_edit_text(query, "No student selected — start over with /admin.")
        return

    _, _, action = query.data.partition(":")

    if action == "profile":
        profile = await asyncio.to_thread(sheets.get_student_profile, student_name)
        if not profile:
            await safe_edit_text(query, f"No profile data found for {student_name}.")
            return
        await safe_edit_text(query, _format_student_profile(profile), parse_mode="Markdown")
        return

    if action == "addpenalty":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Unexcused absence (-1)", callback_data="admin_penalty_preset:absence")],
            [InlineKeyboardButton("Lateness (-1)", callback_data="admin_penalty_preset:lateness")],
            [InlineKeyboardButton("Late homework (-1)", callback_data="admin_penalty_preset:late_hw")],
            [InlineKeyboardButton("Missing homework (-3)", callback_data="admin_penalty_preset:missing_hw")],
            [InlineKeyboardButton("✏️ Custom (type points + reason)", callback_data="admin_penalty_preset:custom")],
            [InlineKeyboardButton("⬅️ Cancel", callback_data="admin_menu:cancel")],
        ])
        await safe_edit_text(query, f"Add penalty for *{student_name}* — pick a reason:",
                                       parse_mode="Markdown", reply_markup=keyboard)
        return

    if action == "removepenalty":
        penalties = await asyncio.to_thread(sheets.get_active_penalties, student_name)
        if not penalties:
            await safe_edit_text(query, f"{student_name} has no active penalties.")
            return
        buttons = [
            [InlineKeyboardButton(f"{p['reason']} (-{p['points']})", callback_data=f"admin_removepenalty_row:{p['row_number']}")]
            for p in penalties
        ]
        buttons.append([InlineKeyboardButton("⬅️ Cancel", callback_data="admin_menu:cancel")])
        await safe_edit_text(query, f"Remove which penalty for *{student_name}*?",
                                       parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if action == "setpayment_direct":
        await safe_edit_text(query, "Set status to:", reply_markup=_payment_status_keyboard("direct"))
        return

    if action == "removestudent":
        rows = await asyncio.to_thread(sheets.get_roster_rows_for_student, student_name, False)
        active_rows = [r for r in rows if r["status"].lower() == "active"]
        if not active_rows:
            await safe_edit_text(query, f"{student_name} has no active roster entries to remove.")
            return
        if len(active_rows) == 1:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, remove", callback_data=f"admin_removestudent_confirm:{active_rows[0]['row_number']}")],
                [InlineKeyboardButton("⬅️ Cancel", callback_data="admin_menu:cancel")],
            ])
            await safe_edit_text(query, 
                f"Remove *{student_name}* from {active_rows[0]['group']}?\n\n"
                f"This keeps all their history (attendance, homework, penalties, payments) — "
                f"it just marks them inactive so they stop showing up in browse/search and stop "
                f"getting reminders/alerts.",
                parse_mode="Markdown", reply_markup=keyboard,
            )
        else:
            # Multi-group student — let the admin remove them from one group
            # specifically, rather than guessing which enrollment they mean.
            buttons = [
                [InlineKeyboardButton(f"Remove from {r['group']}", callback_data=f"admin_removestudent_confirm:{r['row_number']}")]
                for r in active_rows
            ]
            buttons.append([InlineKeyboardButton("⬅️ Cancel", callback_data="admin_menu:cancel")])
            await safe_edit_text(query, 
                f"*{student_name}* is enrolled in multiple groups — remove from which one?",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons),
            )
        return


async def admin_removestudent_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return
    _, _, row_str = query.data.partition(":")
    await asyncio.to_thread(sheets.set_student_status, int(row_str), "inactive")
    await safe_edit_text(query, "Student removed (marked inactive — history preserved).")


async def admin_penalty_preset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the preset-reason buttons under Add Penalty."""
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return

    student_name = context.user_data.get("selected_student")
    if not student_name:
        await safe_edit_text(query, "No student selected — start over with /admin.")
        return

    _, _, preset_key = query.data.partition(":")

    if preset_key == "custom":
        context.user_data["pending_admin_action"] = {"type": "addpenalty_custom", "student": student_name}
        await safe_edit_text(query, 
            f"Adding a custom penalty for *{student_name}*.\n\n"
            f"Send it as: `<points> <reason text>` — e.g. `2 Disrupting class`",
            parse_mode="Markdown",
        )
        return

    reason, points = _PENALTY_PRESETS[preset_key]
    profile = await asyncio.to_thread(sheets.get_student_profile, student_name)
    group = profile["group"] if profile else ""
    await asyncio.to_thread(sheets.add_manual_penalty, student_name, group, reason, points, "Admin")
    await safe_edit_text(query, f"Added: {reason} (-{points}) for {student_name}.")


async def admin_removepenalty_row_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles tapping a specific penalty to remove."""
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return

    _, _, row_str = query.data.partition(":")
    await asyncio.to_thread(sheets.remove_admin_panel_penalty, int(row_str))
    await safe_edit_text(query, "Penalty removed.")


async def admin_addstudent_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group was picked — now ask for the tuition amount before actually
    writing anything, since creating the student needs both Roster AND a
    Finance row (with an amount) to be usable end-to-end."""
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return

    pending = context.user_data.get("pending_admin_action")
    if not pending or pending.get("type") != "addstudent_pick_group":
        await safe_edit_text(query, "That selection expired — start over with /admin.")
        return

    _, _, group_name = query.data.partition(":")
    context.user_data["pending_admin_action"] = {
        "type": "addstudent_amount", "name": pending["name"], "tg": pending["tg"], "group": group_name,
    }
    await safe_edit_text(query, f"What's {pending['name']}'s tuition for {group_name}?",
                          reply_markup=_amount_picker_keyboard("addstudent"))


PRESET_AMOUNTS = {"89": 89, "119": 119}


def _amount_picker_keyboard(purpose: str) -> InlineKeyboardMarkup:
    """purpose is 'setpayment' (amount received toward a Pending balance) or
    'addstudent' (tuition fee owed for a newly-added student)."""
    rows = [
        [
            InlineKeyboardButton("$89", callback_data=f"admin_amount_preset:{purpose}:89"),
            InlineKeyboardButton("$119", callback_data=f"admin_amount_preset:{purpose}:119"),
        ],
        [InlineKeyboardButton("✏️ Custom amount", callback_data=f"admin_amount_preset:{purpose}:custom")],
        [InlineKeyboardButton("⬅️ Cancel", callback_data="admin_menu:cancel")],
    ]
    return InlineKeyboardMarkup(rows)


async def _finalize_addstudent(name: str, tg: str, group: str, amount) -> str:
    """Creates the student in BOTH Roster (Admin Panel) and the Finance
    group tab (with the given tuition amount, status starting Pending) —
    both are required for the student to actually be usable end-to-end.
    """
    await asyncio.to_thread(sheets.add_roster_student, name, tg, group)
    try:
        await asyncio.to_thread(sheets.add_finance_student, group, name, tg, amount, "Pending")
    except Exception as exc:
        log.warning("add_finance_student failed for %s in %s: %s", name, group, exc)
        return (
            f"Added {name} to Roster and {group}'s attendance tab, but couldn't create their "
            f"Finance row automatically ({exc}) — add them to the Finance sheet's \"{group}\" tab "
            f"manually with amount {amount}."
        )
    return f"Added {name}" + (f" (@{tg})" if tg else " (no TG handle yet)") + f" to {group}, tuition ${amount} (Pending)."


async def admin_setpayment_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resolves which student was meant when a name/username search under
    the payment-status flow returned more than one match."""
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return

    pending = context.user_data.get("pending_admin_action")
    if not pending or pending.get("type") != "setpayment":
        await safe_edit_text(query, "That selection expired — start over with /admin.")
        return

    _, _, student_name = query.data.partition(":")
    profile = await asyncio.to_thread(sheets.get_student_profile, student_name)
    if not profile or not profile.get("tg"):
        await safe_edit_text(query, f"{student_name} has no TG handle on file — can't set payment status.")
        context.user_data.pop("pending_admin_action", None)
        return

    target_username = profile["tg"].lstrip("@")
    status = pending["status"]

    if status.lower() == "pending":
        context.user_data["pending_admin_action"] = {
            "type": "setpayment_amount", "status": status,
            "target_username": target_username, "student_name": student_name,
        }
        await safe_edit_text(query, f"How much {student_name} to pay?",
                              reply_markup=_amount_picker_keyboard("setpayment"))
        return

    success, msg = await set_payment_status(context.bot, target_username, status, source="admin_override")
    await safe_edit_text(query, msg if success else f"⚠️ {msg}")
    context.user_data.pop("pending_admin_action", None)


async def admin_setpayment_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """After a status button is tapped: if the student is already known
    (origin='direct', from a profile card), act immediately — no re-asking
    for a username. If not (origin='search', from the top-level menu), ask
    for a name/username to look up. Either way, choosing Pending branches
    into the amount picker before finalizing.
    """
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return

    parts = query.data.split(":")
    status = parts[1] if len(parts) > 1 else ""
    origin = parts[2] if len(parts) > 2 else "search"

    if origin == "direct":
        student_name = context.user_data.get("selected_student")
        if not student_name:
            await safe_edit_text(query, "No student selected — start over with /admin.")
            return
        profile = await asyncio.to_thread(sheets.get_student_profile, student_name)
        if not profile or not profile.get("tg"):
            await safe_edit_text(query, f"{student_name} has no TG handle on file — can't set payment status.")
            return
        target_username = profile["tg"].lstrip("@")

        if status.lower() == "pending":
            context.user_data["pending_admin_action"] = {
                "type": "setpayment_amount", "status": status,
                "target_username": target_username, "student_name": student_name,
            }
            await safe_edit_text(query, f"How much has {student_name} paid so far?",
                                  reply_markup=_amount_picker_keyboard("setpayment"))
            return

        success, msg = await set_payment_status(context.bot, target_username, status, source="admin_override")
        await safe_edit_text(query, msg if success else f"⚠️ {msg}")
        return

    # origin == "search" — student not yet known, ask for name/username
    context.user_data["pending_admin_action"] = {"type": "setpayment", "status": status}
    await safe_edit_text(query,
        f"Setting status to *{status}*.\n\n"
        f"Now send the student's name or @username (partial is fine — I'll match it).",
        parse_mode="Markdown",
    )


async def admin_amount_preset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the $89/$119/Custom buttons shown for both the Pending-amount
    flow and the new-student tuition flow."""
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return

    _, purpose, code = query.data.split(":")
    pending = context.user_data.get("pending_admin_action")
    if not pending:
        await safe_edit_text(query, "That selection expired — start over with /admin.")
        return

    if code == "custom":
        pending["awaiting_custom_amount"] = True
        context.user_data["pending_admin_action"] = pending
        await safe_edit_text(query, "Send the amount as a number (e.g. \"50\").")
        return

    amount = PRESET_AMOUNTS[code]

    if purpose == "setpayment":
        success, msg = await set_payment_status(
            context.bot, pending["target_username"], pending["status"], source="admin_override", amount=amount
        )
        await safe_edit_text(query, msg if success else f"⚠️ {msg}")
    elif purpose == "addstudent":
        result_msg = await _finalize_addstudent(pending["name"], pending["tg"], pending["group"], amount)
        await safe_edit_text(query, result_msg)

    context.user_data.pop("pending_admin_action", None)


async def admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches the admin's plain-text reply after a guided-menu step that's
    waiting on typed input. Does nothing if there's no pending action, so it
    never interferes with normal admin chatting."""
    if not is_admin_update(update):
        return

    pending = context.user_data.get("pending_admin_action")
    if not pending:
        return  # no guided flow in progress — ignore, let other handlers run

    text = update.message.text.strip()

    if pending.get("awaiting_custom_amount"):
        try:
            amount = int(float(text.replace("$", "").strip()))
        except ValueError:
            await update.message.reply_text("Couldn't parse that as a number — send just the amount, e.g. \"50\".")
            return  # keep pending so they can retry

        if pending["type"] == "setpayment_amount":
            success, msg = await set_payment_status(
                context.bot, pending["target_username"], pending["status"], source="admin_override", amount=amount
            )
            await update.message.reply_text(msg if success else f"⚠️ {msg}")
        elif pending["type"] == "addstudent_amount":
            result_msg = await _finalize_addstudent(pending["name"], pending["tg"], pending["group"], amount)
            await update.message.reply_text(result_msg)

        context.user_data.pop("pending_admin_action", None)
        return

    if pending["type"] == "setpayment":
        matches = await asyncio.to_thread(sheets.search_roster, text)
        if not matches:
            await update.message.reply_text(f"No student found matching '{text}'. Try again or /admin to restart.")
            return  # keep pending so they can retry
        if len(matches) == 1:
            target_username = matches[0]["tg"].lstrip("@")
            if not target_username:
                await update.message.reply_text(f"{matches[0]['name']} has no TG handle on file — can't set payment status.")
                context.user_data.pop("pending_admin_action", None)
                return
            status = pending["status"]
            if status.lower() == "pending":
                context.user_data["pending_admin_action"] = {
                    "type": "setpayment_amount", "status": status,
                    "target_username": target_username, "student_name": matches[0]["name"],
                }
                await update.message.reply_text(
                    f"How much has {matches[0]['name']} paid so far?",
                    reply_markup=_amount_picker_keyboard("setpayment"),
                )
                return  # amount picker will finalize + clear pending
            success, msg = await set_payment_status(context.bot, target_username, status, source="admin_override")
            await update.message.reply_text(msg if success else f"⚠️ {msg}")
        else:
            # Multiple matches — keep the pending status alive and let the
            # admin tap the right one instead of typing a more specific query.
            buttons = [
                [InlineKeyboardButton(f"{m['name']} ({m['group']})", callback_data=f"admin_setpayment_pick:{m['name']}")]
                for m in matches
            ]
            await update.message.reply_text(
                f"Found {len(matches)} matches — which one?", reply_markup=InlineKeyboardMarkup(buttons)
            )
            return  # keep pending_admin_action set; the pick callback will clear it

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
            return  # keep pending_admin_action set so they can retry
        points, reason = int(parts[0]), parts[1]
        profile = await asyncio.to_thread(sheets.get_student_profile, student_name)
        group = profile["group"] if profile else ""
        await asyncio.to_thread(sheets.add_manual_penalty, student_name, group, reason, points, "Admin")
        await update.message.reply_text(f"Added: {reason} ({points:+d}) for {student_name}.")

    elif pending["type"] == "addstudent_name":
        context.user_data["pending_admin_action"] = {"type": "addstudent_tg", "name": text}
        await update.message.reply_text(
            f"Name: {text}\n\nNow send their @username (or type \"none\" if they don't have one yet)."
        )
        return  # keep the flow going — don't clear pending_admin_action

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
        return  # next step (tuition amount) is triggered by the group-pick callback

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


async def admin_setpayment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/admin_setpayment <name or @username> <paid|pending|scholarship|cancelled>
    — manual override, admin-only. Routes through the same set_payment_status()
    the buttons use, so there is only ever one code path that writes to
    the Finance sheet.
    """
    if not is_admin_update(update):
        return  # silently ignore non-admins

    valid = {s.lower() for s in PAYMENT_STATUSES}
    if len(context.args) < 2 or context.args[-1].lower() not in valid:
        await update.message.reply_text(
            f"Usage: /admin_setpayment <name or @username> <{'|'.join(s.lower() for s in PAYMENT_STATUSES)}>"
        )
        return

    new_status = next(s for s in PAYMENT_STATUSES if s.lower() == context.args[-1].lower())
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
        context.user_data["pending_admin_action"] = {"type": "setpayment", "status": new_status}
        await update.message.reply_text(f"Found {len(matches)} matches — which one?",
                                         reply_markup=InlineKeyboardMarkup(buttons))
        return

    target_username = matches[0]["tg"].lstrip("@")
    if not target_username:
        await update.message.reply_text(f"{matches[0]['name']} has no TG handle on file — can't set payment status.")
        return

    if new_status.lower() == "pending":
        context.user_data["pending_admin_action"] = {
            "type": "setpayment_amount", "status": new_status,
            "target_username": target_username, "student_name": matches[0]["name"],
        }
        await update.message.reply_text(
            f"How much has {matches[0]['name']} paid so far?",
            reply_markup=_amount_picker_keyboard("setpayment"),
        )
        return

    success, msg = await set_payment_status(context.bot, target_username, new_status, source="admin_override")
    await update.message.reply_text(msg if success else f"⚠️ {msg}")


async def proof_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin_update(update):
        return

    action, _, target_username = query.data.partition(":")
    bd_entry = await asyncio.to_thread(sheets.find_bot_data_row, target_username)

    if not bd_entry or not bd_entry.get("chat_id"):
        await safe_edit_caption(query, caption=query.message.caption + "\n\n⚠️ No linked chat found.")
        return

    if action == "reject":
        await asyncio.to_thread(sheets.clear_payment_proof, target_username)
        await notify_student(context.bot, int(bd_entry["chat_id"]), text=messages.proof_rejected())
        await safe_edit_caption(query, caption=query.message.caption + "\n\n❌ Rejected")

    elif action == "approve":
        success, _ = await set_payment_status(context.bot, target_username, "Paid", source="proof")
        suffix = "\n\n✅ Approved" if success else "\n\n⚠️ Approval failed — check logs."
        await safe_edit_caption(query, caption=query.message.caption + suffix)

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
        return  # silently ignore non-admins

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
    app.add_handler(MessageHandler(filters.PHOTO, photo))
    app.add_handler(CommandHandler("pay", pay))

    # Order matters: more specific callback_data patterns first, proof_decision
    # (no pattern — catches "approve:"/"reject:") stays last as the fallback.
    app.add_handler(CallbackQueryHandler(admin_menu_callback, pattern=r"^admin_menu:"))
    app.add_handler(CallbackQueryHandler(admin_group_callback, pattern=r"^admin_group:"))
    app.add_handler(CallbackQueryHandler(admin_pick_student_callback, pattern=r"^admin_pick:"))
    app.add_handler(CallbackQueryHandler(admin_action_callback, pattern=r"^admin_action:"))
    app.add_handler(CallbackQueryHandler(admin_penalty_preset_callback, pattern=r"^admin_penalty_preset:"))
    app.add_handler(CallbackQueryHandler(admin_removepenalty_row_callback, pattern=r"^admin_removepenalty_row:"))
    app.add_handler(CallbackQueryHandler(admin_setpayment_status_callback, pattern=r"^admin_setpayment_status:"))
    app.add_handler(CallbackQueryHandler(admin_setpayment_pick_callback, pattern=r"^admin_setpayment_pick:"))
    app.add_handler(CallbackQueryHandler(admin_amount_preset_callback, pattern=r"^admin_amount_preset:"))
    app.add_handler(CallbackQueryHandler(admin_addstudent_group_callback, pattern=r"^admin_addstudent_group:"))
    app.add_handler(CallbackQueryHandler(admin_removestudent_confirm_callback, pattern=r"^admin_removestudent_confirm:"))
    app.add_handler(CallbackQueryHandler(admin_removegroup_pick_callback, pattern=r"^admin_removegroup_pick:"))
    app.add_handler(CallbackQueryHandler(admin_removegroup_confirm_callback, pattern=r"^admin_removegroup_confirm:"))
    app.add_handler(CallbackQueryHandler(proof_decision))

    # Plain-text replies during a guided admin flow (search query, custom
    # penalty, username for payment) — must not swallow normal messages, so
    # it only acts when a pending_admin_action is actually set (checked
    # inside admin_text_input itself). Registered after admin-only command
    # handlers so /commands still route correctly first.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text_input))

    log.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()