"""Scheduled background jobs.

Two jobs, both driven by APScheduler (see bot.py):
  * run_reminders       — every few days; nudges students who still owe money.
  * run_payment_check   — daily; thanks students whose status just went Paid.

Both read the bulk tables once (Bot Data / Send Log) to stay well under the
Sheets API rate limits, and offload blocking gspread calls to worker threads.

All outbound messages to students go through notify_student(), which honors
config.SUPPRESS_STUDENT_MESSAGES — while that's True, both jobs run their
full logic (reading sheets, deciding who to message, logging to Send Log)
but no actual Telegram message is sent. Safe to test against real data.
"""
import asyncio
import datetime
import logging

import config
import messages
import sheets
from notify import notify_student

log = logging.getLogger(__name__)

# How long after a session's official start time a student should be
# alerted if they're marked Late/Absent for it.
ALERT_DELAY_MINUTES = 10
# This job should run at roughly this interval — the alert window below is
# sized to match, so each session gets caught exactly once regardless of
# exact trigger timing jitter.
JOB_INTERVAL_MINUTES = 5

_DAY_ABBREVIATIONS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


async def run_attendance_alerts(bot):
    """Every JOB_INTERVAL_MINUTES, checks each group's schedule for a session
    that started ALERT_DELAY_MINUTES ago, and — if so — alerts any student
    marked Late/Absent for it with their reason and current penalty total.

    Reads live from the Admin Panel spreadsheet (Groups, Roster,
    Attendance_Log, Penalty_Log) via Code.gs's data, and is dedup-guarded by
    AttendanceAlertLog so a student is never alerted twice for the same
    session even if the job runs multiple times inside the alert window.
    """
    log.info("Attendance alert job: starting")
    now = datetime.datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    today_abbrev = _DAY_ABBREVIATIONS[now.weekday()]

    groups = await asyncio.to_thread(sheets.get_groups_schedule)
    alerted = 0

    for group in groups:
        if today_abbrev not in group["days"]:
            continue
        if not group["start_time"]:
            continue

        try:
            hour, minute = (int(x) for x in group["start_time"].split(":"))
        except ValueError:
            log.warning("Bad start_time for group %s: %r", group["name"], group["start_time"])
            continue

        session_start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        alert_window_start = session_start + datetime.timedelta(minutes=ALERT_DELAY_MINUTES)
        alert_window_end = alert_window_start + datetime.timedelta(minutes=JOB_INTERVAL_MINUTES)

        if not (alert_window_start <= now < alert_window_end):
            continue  # not this group's alert window right now

        rows = await asyncio.to_thread(sheets.get_todays_attendance_for_group, group["name"], today_str)
        if not rows:
            continue

        roster = await asyncio.to_thread(sheets.get_roster_map)
        already_alerted = await asyncio.to_thread(sheets.get_alerted_keys)

        for row in rows:
            student_name = row["student"]
            key = f"{student_name}|{group['name']}|{today_str}"
            if key in already_alerted:
                continue

            student = roster.get(student_name)
            if not student or not student["tg"]:
                log.warning("No roster/TG match for %s in %s — can't alert.", student_name, group["name"])
                continue

            bd_entry = await asyncio.to_thread(sheets.find_bot_data_row, student["tg"])
            if not bd_entry or not bd_entry.get("chat_id"):
                log.info("No linked chat for %s (@%s) — can't alert yet.", student_name, student["tg"])
                continue

            total_points = await asyncio.to_thread(sheets.get_penalty_total, student_name)

            await notify_student(
                bot, int(bd_entry["chat_id"]),
                text=messages.attendance_alert(student_name, row["status"], total_points),
            )
            await asyncio.to_thread(sheets.log_alert, key)
            alerted += 1

    log.info("Attendance alert job: done (alerted=%d)", alerted)





def _is_paid(status: str) -> bool:
    return status.strip().lower() in config.PAID_STATUSES


async def send_manual_reminder(bot, username: str) -> tuple[bool, str]:
    """Send a single reminder to one student on demand (e.g. from the admin
    panel's 'Send reminder' button), sharing the exact same reminder-number
    escalation, Send Log entry, and pay_shown marking as the scheduled
    run_reminders() job — so a manual send counts toward the same escalation
    sequence instead of skipping it.
    """
    rec = await asyncio.to_thread(sheets.find_student, username)
    if not rec:
        return False, f"No student record found for @{username}."

    if _is_paid(rec["status"]):
        return False, f"{rec['name']} is already marked {rec['status']} — no reminder needed."

    key = sheets.normalize_username(username)
    bd_entry = await asyncio.to_thread(sheets.find_bot_data_row, username)

    if not bd_entry or not str(bd_entry.get("chat_id", "")).strip():
        await asyncio.to_thread(
            sheets.append_send_log,
            rec["group"], rec["name"], rec["tg"], "no chat_id found (manual)", "",
        )
        return False, f"{rec['name']} has no linked Telegram chat — can't send a reminder."

    reminder_number = await asyncio.to_thread(sheets.count_sent, username) + 1
    caption = messages.reminder_text(rec["name"], rec["amount"], reminder_number)

    try:
        with open(config.PAYME_QR_FILE, "rb") as photo:
            await notify_student(bot, int(bd_entry["chat_id"]), photo=photo, caption=caption)
    except Exception as exc:
        log.warning("Manual reminder to %s failed: %s", username, exc)
        await asyncio.to_thread(
            sheets.append_send_log,
            rec["group"], rec["name"], rec["tg"], f"error: {exc} (manual)", "",
        )
        return False, f"Failed to send reminder to {rec['name']}: {exc}"

    log_result = "sent (manual)" if not config.SUPPRESS_STUDENT_MESSAGES else "suppressed (test mode, manual)"
    await asyncio.to_thread(
        sheets.append_send_log,
        rec["group"], rec["name"], rec["tg"], log_result, reminder_number,
    )
    await asyncio.to_thread(sheets.mark_pay_shown, username)

    return True, f"Reminder #{reminder_number} sent to {rec['name']}."

async def run_reminders(bot):
    """Send an escalating payment reminder to every student who still owes."""
    log.info("Reminder job: starting")
    records = await asyncio.to_thread(sheets.iter_group_records)
    bot_data = await asyncio.to_thread(sheets.get_bot_data_map)
    sent_counts = await asyncio.to_thread(sheets.get_sent_counts)

    sent = skipped = no_chat = errors = 0

    for rec in records:
        if _skip_reminder(rec["status"]):
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
                await notify_student(
                    bot, int(entry["chat_id"]), photo=photo, caption=caption
                )
        except Exception as exc:  # network / blocked / bad chat_id
            log.warning("Reminder to %s failed: %s", rec["tg"], exc)
            await asyncio.to_thread(
                sheets.append_send_log,
                rec["group"], rec["name"], rec["tg"], f"error: {exc}", "",
            )
            errors += 1
            continue

        log_result = "sent" if not config.SUPPRESS_STUDENT_MESSAGES else "suppressed (test mode)"
        await asyncio.to_thread(
            sheets.append_send_log,
            rec["group"], rec["name"], rec["tg"], log_result, reminder_number,
        )
        await asyncio.to_thread(sheets.mark_pay_shown, rec["tg"])
        sent_counts[key] = reminder_number  # keep count fresh within this run
        sent += 1

    log.info(
        "Reminder job: done (sent=%d, no_chat=%d, errors=%d, skipped=%d, suppressed=%s)",
        sent, no_chat, errors, skipped, config.SUPPRESS_STUDENT_MESSAGES,
    )

def _is_paid(status: str) -> bool:
    return status.strip().lower() in config.PAID_STATUSES


def _skip_reminder(status: str) -> bool:
    return status.strip().lower() in config.NO_REMINDER_STATUSES

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
                await notify_student(
                    bot, int(entry["chat_id"]), text=messages.payment_success(rec["name"])
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
        "Payment-check job: done (notified=%d, statuses_updated=%d, suppressed=%s)",
        notified, updated, config.SUPPRESS_STUDENT_MESSAGES,
    )