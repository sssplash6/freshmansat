"""Scheduled background jobs.

Two jobs, both driven by APScheduler (see bot.py):
  * run_reminders       — every few days; nudges students who still owe money.
  * run_payment_check   — daily; thanks students whose status just went Paid.

Both read the bulk tables once (Bot Data / Send Log) to stay well under the
Sheets API rate limits, and offload blocking gspread calls to worker threads.
"""
import asyncio
import logging

import config
import messages
import sheets

log = logging.getLogger(__name__)


def _is_paid(status: str) -> bool:
    return status.strip().lower() in config.PAID_STATUSES


async def run_reminders(bot):
    """Send an escalating payment reminder to every student who still owes."""
    log.info("Reminder job: starting")
    records = await asyncio.to_thread(sheets.iter_group_records)
    bot_data = await asyncio.to_thread(sheets.get_bot_data_map)
    sent_counts = await asyncio.to_thread(sheets.get_sent_counts)

    sent = skipped = no_chat = errors = 0

    for rec in records:
        if _is_paid(rec["status"]):
            skipped += 1
            continue
        if not rec["tg"].strip():
            skipped += 1
            continue

        key = sheets.normalize_username(rec["tg"])
        entry = bot_data.get(key)

        if entry and str(entry.get("proof_link", "")).strip():
            skipped += 1
            continue
        if not entry or not str(entry.get("chat_id", "")).strip():
            await asyncio.to_thread(
                sheets.append_send_log,
                rec["group"], rec["name"], rec["tg"], "no chat_id found", "",
            )
            no_chat += 1
            continue

        reminder_number = sent_counts.get(key, 0) + 1
        caption = messages.reminder_text(rec["name"], rec["amount"], reminder_number)

        try:
            with open(config.PAYME_QR_FILE, "rb") as photo:
                await bot.send_photo(
                    chat_id=int(entry["chat_id"]),
                    photo=photo,
                    caption=caption,
                )
        except Exception as exc:  # network / blocked / bad chat_id
            log.warning("Reminder to %s failed: %s", rec["tg"], exc)
            await asyncio.to_thread(
                sheets.append_send_log,
                rec["group"], rec["name"], rec["tg"], f"error: {exc}", "",
            )
            errors += 1
            continue

        await asyncio.to_thread(
            sheets.append_send_log,
            rec["group"], rec["name"], rec["tg"], "sent", reminder_number,
        )
        await asyncio.to_thread(sheets.mark_pay_shown, rec["tg"])
        sent_counts[key] = reminder_number  # keep count fresh within this run
        sent += 1

    log.info(
        "Reminder job: done (sent=%d, no_chat=%d, errors=%d, skipped=%d)",
        sent, no_chat, errors, skipped,
    )


async def run_payment_check(bot):
    """Thank students whose status just changed to Paid/Scholarship."""
    log.info("Payment-check job: starting")
    records = await asyncio.to_thread(sheets.iter_group_records)
    bot_data = await asyncio.to_thread(sheets.get_bot_data_map)

    notified = updated = 0
    seen = set()  # avoid double-processing duplicate usernames across tabs

    for rec in records:
        if not rec["tg"].strip():
            continue
        key = sheets.normalize_username(rec["tg"])
        if key in seen:
            continue
        entry = bot_data.get(key)
        if not entry:
            continue  # not registered — nowhere to store status, can't message
        seen.add(key)

        current = rec["status"].strip()
        last = entry["last_status"].strip()

        newly_paid = _is_paid(current) and not _is_paid(last)
        if newly_paid and str(entry.get("chat_id", "")).strip():
            try:
                await bot.send_message(
                    chat_id=int(entry["chat_id"]),
                    text=messages.payment_success(rec["name"]),
                )
                notified += 1
            except Exception as exc:
                log.warning("Thank-you to %s failed: %s", rec["tg"], exc)

        if current != last:
            await asyncio.to_thread(
                sheets.update_last_known_status, entry["row_number"], current
            )
            updated += 1

    log.info(
        "Payment-check job: done (notified=%d, statuses_updated=%d)",
        notified, updated,
    )
