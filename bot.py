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

async def approve_payment(bot, target_username: str, source: str) -> tuple[bool, str]:
    """Single entry point for marking a student Paid.

    Used by both the admin's inline Approve button (source='proof') and the
    /admin_setpayment command (source='admin_override'). This is the ONLY
    place that writes Paid status to the Finance sheet — Apps Script never
    touches Finance directly. Every write here also logs to Payment_Log in
    the Admin Panel spreadsheet so the admin panel has full visibility
    without needing Finance access itself.

    Returns (success, message) for the caller to relay back to the admin.
    """
    rec = await asyncio.to_thread(sheets.find_student, target_username)
    if not rec:
        return False, f"No student record found for @{target_username}."

    bd_entry = await asyncio.to_thread(sheets.find_bot_data_row, target_username)

    await asyncio.to_thread(sheets.set_status_paid, rec["worksheet"], rec["row_number"])
    await asyncio.to_thread(
        sheets.log_payment_change, rec["name"], target_username, "Paid", source, rec["amount"]
    )

    if bd_entry and bd_entry.get("chat_id"):
        await asyncio.to_thread(sheets.update_last_known_status, bd_entry["row_number"], "Paid")
        try:
            await notify_student(
                bot, int(bd_entry["chat_id"]), text=messages.payment_success(rec["name"])
            )
        except Exception as exc:
            log.warning("Could not notify @%s of payment approval: %s", target_username, exc)

    return True, f"Marked @{target_username} ({rec['name']}) as Paid."


async def set_unpaid(target_username: str, source: str) -> tuple[bool, str]:
    """Reverts a student to Unpaid. Admin-override only — there's no inline
    button for this since the normal flow only ever moves Unpaid -> Paid."""
    rec = await asyncio.to_thread(sheets.find_student, target_username)
    if not rec:
        return False, f"No student record found for @{target_username}."

    await asyncio.to_thread(sheets.set_status_unpaid, rec["worksheet"], rec["row_number"])
    await asyncio.to_thread(
        sheets.log_payment_change, rec["name"], target_username, "Unpaid", source, rec["amount"]
    )

    bd_entry = await asyncio.to_thread(sheets.find_bot_data_row, target_username)
    if bd_entry:
        await asyncio.to_thread(sheets.update_last_known_status, bd_entry["row_number"], "Unpaid")

    return True, f"Marked @{target_username} ({rec['name']}) as Unpaid."


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
    if str(update.message.chat_id) != str(config.ADMIN_CHAT_ID):
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Set payment status", callback_data="admin_menu:setpayment")],
        [InlineKeyboardButton("❌ Cancel", callback_data="admin_menu:cancel")],
    ])
    await update.message.reply_text(
        "Admin menu — what would you like to do?", reply_markup=keyboard
    )


async def admin_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles taps on the /admin menu buttons and the Paid/Unpaid sub-menu."""
    query = update.callback_query
    await query.answer()

    if str(query.message.chat_id) != str(config.ADMIN_CHAT_ID):
        return

    _, _, action = query.data.partition(":")

    if action == "cancel":
        context.user_data.pop("pending_admin_action", None)
        await query.edit_message_text("Cancelled.")
        return

    if action == "setpayment":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Paid", callback_data="admin_setpayment_status:paid"),
                InlineKeyboardButton("❌ Unpaid", callback_data="admin_setpayment_status:unpaid"),
            ],
            [InlineKeyboardButton("⬅️ Cancel", callback_data="admin_menu:cancel")],
        ])
        await query.edit_message_text("Set status to:", reply_markup=keyboard)
        return

    if action.startswith("admin_setpayment_status"):
        return  # handled by the dedicated callback below


async def admin_setpayment_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Second step of the guided flow: after Paid/Unpaid is tapped, ask the
    admin to just send the student's @username as a plain message."""
    query = update.callback_query
    await query.answer()

    if str(query.message.chat_id) != str(config.ADMIN_CHAT_ID):
        return

    _, _, status = query.data.partition(":")
    context.user_data["pending_admin_action"] = {"type": "setpayment", "status": status}
    await query.edit_message_text(
        f"Setting status to *{status.title()}*.\n\n"
        f"Now send the student's @username (just the username, as a normal message).",
        parse_mode="Markdown",
    )


async def admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches the admin's plain-text reply after a guided-menu step that's
    waiting on a username. Does nothing if there's no pending action, so it
    never interferes with normal admin chatting."""
    if str(update.message.chat_id) != str(config.ADMIN_CHAT_ID):
        return

    pending = context.user_data.get("pending_admin_action")
    if not pending:
        return  # no guided flow in progress — ignore, let other handlers run

    target_username = update.message.text.strip().lstrip("@")

    if pending["type"] == "setpayment":
        if pending["status"] == "paid":
            success, msg = await approve_payment(context.bot, target_username, source="admin_override")
        else:
            success, msg = await set_unpaid(target_username, source="admin_override")
        await update.message.reply_text(msg if success else f"⚠️ {msg}")

    context.user_data.pop("pending_admin_action", None)


async def admin_setpayment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/admin_setpayment <username> <paid|unpaid> — manual override, admin-only.

    Routes through the exact same approve_payment()/set_unpaid() functions
    the inline Approve button uses, so there is only ever one code path that
    writes to the Finance sheet.
    """
    if str(update.message.chat_id) != str(config.ADMIN_CHAT_ID):
        return  # silently ignore non-admins

    if len(context.args) != 2 or context.args[1].lower() not in ("paid", "unpaid"):
        await update.message.reply_text("Usage: /admin_setpayment <username> <paid|unpaid>")
        return

    target_username = context.args[0].lstrip("@")
    new_status = context.args[1].lower()

    if new_status == "paid":
        success, msg = await approve_payment(context.bot, target_username, source="admin_override")
    else:
        success, msg = await set_unpaid(target_username, source="admin_override")

    await update.message.reply_text(msg if success else f"⚠️ {msg}")


async def proof_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if str(query.message.chat_id) != str(config.ADMIN_CHAT_ID):
        return

    action, _, target_username = query.data.partition(":")
    bd_entry = await asyncio.to_thread(sheets.find_bot_data_row, target_username)

    if not bd_entry or not bd_entry.get("chat_id"):
        await query.edit_message_caption(caption=query.message.caption + "\n\n⚠️ No linked chat found.")
        return

    if action == "reject":
        await asyncio.to_thread(sheets.clear_payment_proof, target_username)
        await notify_student(context.bot, int(bd_entry["chat_id"]), text=messages.proof_rejected())
        await query.edit_message_caption(caption=query.message.caption + "\n\n❌ Rejected")

    elif action == "approve":
        success, _ = await approve_payment(context.bot, target_username, source="proof")
        suffix = "\n\n✅ Approved" if success else "\n\n⚠️ Approval failed — check logs."
        await query.edit_message_caption(caption=query.message.caption + suffix)

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
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    log.info(
        "Scheduler started: reminders every %d day(s), payment check daily at %02d:%02d",
        config.REMINDER_INTERVAL_DAYS,
        config.PAYMENT_CHECK_HOUR,
        config.PAYMENT_CHECK_MINUTE,
    )

async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.message.chat_id) != str(config.ADMIN_CHAT_ID):
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
    app.add_handler(MessageHandler(filters.PHOTO, photo))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CallbackQueryHandler(proof_decision))

    log.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()