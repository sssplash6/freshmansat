"""Telegram bot entrypoint.

Registers the /start and /status command handlers, and wires up the two
APScheduler jobs (reminders + payment confirmation) to run alongside the bot's
async event loop.

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
from telegram.ext import CallbackQueryHandler
import config
import jobs
import messages
import sheets

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
        await context.bot.send_message(
            chat_id=int(bd_entry["chat_id"]),
            text=messages.proof_rejected(),
        )
        await query.edit_message_caption(caption=query.message.caption + "\n\n❌ Rejected")

    elif action == "approve":
        rec = await asyncio.to_thread(sheets.find_student, target_username)
        if rec:
            await asyncio.to_thread(sheets.set_status_paid, rec["worksheet"], rec["row_number"])
        await query.edit_message_caption(caption=query.message.caption + "\n\n✅ Approved")
        # The daily payment-check job will pick this up and send the thank-you,
        # or trigger it immediately:
        await asyncio.to_thread(sheets.update_last_known_status, bd_entry["row_number"], "Paid")
        await context.bot.send_message(
            chat_id=int(bd_entry["chat_id"]),
            text=messages.payment_success(rec["name"] if rec else ""),
        )

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

    await context.bot.send_message(
        chat_id=int(bd_entry["chat_id"]),
        text=messages.proof_rejected(),
    )
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
    app.add_handler(CommandHandler("reject", reject))
    app.add_handler(MessageHandler(filters.PHOTO, photo))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CallbackQueryHandler(proof_decision))

    log.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
