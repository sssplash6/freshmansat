"""Central configuration: paths, sheet identifiers, tab names, secrets.

Secrets are loaded from a .env file (see .env.example). Everything else is a
constant that rarely changes but is kept here so there are no magic strings
scattered across the codebase.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# --- Paths -----------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

CREDENTIALS_FILE = BASE_DIR / "credentials.json"
PAYME_QR_FILE = BASE_DIR / "payme_qr.png"

# --- Secrets / env ---------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
STRIPE_LINKS = {
    "89": os.getenv("STRIPE_LINK_89", "").strip(),
    "119": os.getenv("STRIPE_LINK_119", "").strip(),
}

# Admin's personal Telegram chat_id — payment-proof photos are forwarded here.
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()

ADMIN_PANEL_SHEET_ID = "1BdPzPXXF15LswLlyQOotlWyqZyPRaQcUodKndoTqXxE"
# --- Google Sheet ----------------------------------------------------------
SHEET_ID = "1c9OF_Fwsyh9qTYwgLn1BzS42Y1fyvXEhnn2lhTOetzo"
SUPPRESS_STUDENT_MESSAGES = os.getenv("SUPPRESS_STUDENT_MESSAGES", "true").lower() == "true"


# Tabs that are NOT student groups and must always be skipped.
SPECIAL_TABS = {"bot apps", "bot data", "send log"}  # compared case-insensitively

BOT_DATA_TAB = "Bot Data"
SEND_LOG_TAB = "Send Log"

# --- Column headers (matched case-insensitively, whitespace-trimmed) --------
# Group tabs
COL_NUM = "№"
COL_NAME = "Name"
COL_TG = "TG Contact"
COL_EMAIL = "Email"
COL_AMOUNT = "Amount"
COL_METHOD = "Method"
COL_STATUS = "Status"
COL_CONTACT = "Contact"
COL_DATE_OF_PAYMENT = "Date of Payment"
COL_TOTAL = "Total"

# Bot Data tab
BD_USERNAME = "Telegram Username"
BD_CHAT_ID = "Telegram Chat ID"
BD_FIRST_LINKED = "First Linked Date"
BD_LAST_STATUS = "Last Known Status"
# Payment proofs are recorded here (group tabs are read-only / confidential).
# Proofs are forwarded to the admin via Telegram; BD_PROOF_LINK stores the
# Telegram file_id of the submitted photo (lets the bot re-send it later).
BD_PROOF_LINK = "Payment Proof Link"
BD_PROOF_DATE = "Proof Submitted Date"

# Send Log tab
SL_DATE_SENT = "Date Sent"
SL_GROUP_TAB = "Group Tab"
SL_STUDENT_NAME = "Student Name"
SL_TG_CONTACT = "TG Contact"
SL_RESULT = "Result"
SL_REMINDER_NUM = "Reminder Number"
BD_PAY_SHOWN = "Payment Info Shown"

# Statuses that mean "no reminder needed".
PAID_STATUSES = {"paid", "scholarship"}
NO_REMINDER_STATUSES = {"paid", "scholarship", "cancel"}
# --- Penalty Tracker Sheet (separate spreadsheet) ---------------------------
# NOTE: double-check this ID against the real Penalty Tracker sheet's URL
# before relying on it — it was picked up from a Google error message rather
# than confirmed directly.
PENALTY_SHEET_ID = "1j6ZanQEa4FOT6Cr9W2FlwItvPs6if_GRvoy1vQpmTfw"
PENALTY_LOOKUP_TAB = "PenaltyLookupForBot"

PL_TG_HANDLE = "TG Handle"
PL_STUDENT_NAME = "Student Name"
PL_CLASS = "Class"
PL_TOTAL_POINTS = "Total Active Penalties"

# --- Scheduler -------------------------------------------------------------
REMINDER_INTERVAL_DAYS = 3          # reminder job cadence
REMINDER_INTERVAL_MINUTES = int(os.getenv("REMINDER_INTERVAL_MINUTES", "0"))
PAYMENT_CHECK_HOUR = 9              # daily payment-confirmation job runs at 09:00 local
PAYMENT_CHECK_MINUTE = 0


def validate() -> list[str]:
    """Return a list of human-readable configuration problems (empty if OK)."""
    problems = []
    if not TELEGRAM_BOT_TOKEN:
        problems.append("TELEGRAM_BOT_TOKEN is not set in .env")
    if not all(STRIPE_LINKS.values()):
        problems.append("One or both STRIPE_LINK_89 / STRIPE_LINK_119 are not set in .env")
    if not CREDENTIALS_FILE.exists():
        problems.append(f"credentials.json not found at {CREDENTIALS_FILE}")
    if not PAYME_QR_FILE.exists():
        problems.append(f"payme_qr.png not found at {PAYME_QR_FILE}")
    if not ADMIN_CHAT_ID:
        problems.append("ADMIN_CHAT_ID is not set in .env (proofs are forwarded there)")
    return problems