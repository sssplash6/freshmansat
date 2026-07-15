"""Single choke point for every message the bot sends to a STUDENT.

Never used for admin-facing messages (those go straight to config.ADMIN_CHAT_ID
via bot.send_message/send_photo as before — this module is student-only).

Import this from both bot.py and jobs.py instead of calling bot.send_message /
bot.send_photo directly for anything student-facing, so there is exactly one
place that can suppress outbound messages during testing.
"""
import logging

import config

log = logging.getLogger(__name__)


async def notify_student(bot, chat_id: int, text: str = None, photo=None, caption: str = None):
    """Send (or, if config.SUPPRESS_STUDENT_MESSAGES is True, just log) a
    message to a student's chat_id.

    Set config.SUPPRESS_STUDENT_MESSAGES = False only when you're ready for
    real students to start receiving messages. While True, every call here
    logs what *would* have been sent instead of actually sending it — safe
    to run reminders, payment-check, approvals, everything, with zero risk
    of pinging a real student.
    """
    if config.SUPPRESS_STUDENT_MESSAGES:
        log.info(
            "[SUPPRESSED — would have messaged chat_id=%s] text=%r caption=%r",
            chat_id, text, caption,
        )
        return
    if photo is not None:
        await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption)
    else:
        await bot.send_message(chat_id=chat_id, text=text)