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
            messages.status_found(rec["amount"], rec["status"], rec["total"])
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
            await context.bot.send_photo(
                chat_id=config.ADMIN_CHAT_ID,
                photo=file_id,
                caption=messages.admin_proof_caption(
                    rec["name"], rec["group"], rec["amount"]
                ),
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

    await message.reply_text(messages.proof_received())
    log.info("Proof from @%s recorded (file_id=%s)", username, file_id)


# --- Scheduler lifecycle ---------------------------------------------------
async def on_startup(app: Application):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        jobs.run_reminders,
        trigger=IntervalTrigger(days=config.REMINDER_INTERVAL_DAYS),
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
    app.add_handler(MessageHandler(filters.PHOTO, photo))

    log.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
