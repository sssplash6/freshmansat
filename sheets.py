"""Google Sheets access layer.

All reads/writes to the spreadsheet go through here. Everything is synchronous
(gspread is blocking); callers in async code should wrap these in
``asyncio.to_thread`` so the event loop is never blocked.

Header matching is case-insensitive and whitespace-trimmed, and writes are
aligned to the actual header positions found in each tab, so the code keeps
working even if columns are renamed in casing or reordered.
"""
from __future__ import annotations

import datetime

import gspread
from google.oauth2.service_account import Credentials

import config

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_spreadsheet = None
_ws_cache: dict = {}
_header_cache: dict = {}


# --- Connection ------------------------------------------------------------
def get_spreadsheet():
    """Return the (cached) Spreadsheet handle, authorizing on first use."""
    global _spreadsheet
    if _spreadsheet is None:
        creds = Credentials.from_service_account_file(
            str(config.CREDENTIALS_FILE), scopes=SCOPES
        )
        client = gspread.authorize(creds)
        _spreadsheet = client.open_by_key(config.SHEET_ID)
    return _spreadsheet


def _ws(title):
    """Return a cached Worksheet handle by title."""
    if title not in _ws_cache:
        _ws_cache[title] = get_spreadsheet().worksheet(title)
    return _ws_cache[title]


# --- Small helpers ---------------------------------------------------------
def _norm(value) -> str:
    return str(value).strip() if value is not None else ""


def normalize_username(username) -> str:
    """Strip a leading @, trim, lowercase — the canonical form for matching."""
    u = _norm(username)
    if u.startswith("@"):
        u = u[1:]
    return u.lower()


def today_str() -> str:
    return datetime.date.today().isoformat()


def _header_index(headers, name):
    """Index of a header (case-insensitive, trimmed), or None if absent."""
    target = name.strip().lower()
    for i, h in enumerate(headers):
        if _norm(h).lower() == target:
            return i
    return None


def _detect_header_row(values, cols, scan=10):
    """0-based index of the row that best matches the expected column names.

    Group tabs carry a title banner in row 1 and the real headers in row 2, so
    we can't assume a fixed position — we pick the row (within the first ``scan``)
    containing the most expected headers. Defaults to 0 if nothing matches.
    """
    targets = {c.strip().lower() for c in cols}
    best_i, best_score = 0, 0
    for i, row in enumerate(values[:scan]):
        present = {_norm(c).lower() for c in row}
        score = len(targets & present)
        if score > best_score:
            best_i, best_score = i, score
    return best_i


def _read(ws, cols):
    """Read a worksheet into normalized records.

    Returns (headers, idx, rows) where idx maps each requested column to its
    0-based position, and each row dict carries ``_row`` (1-based sheet row) plus
    the requested columns' trimmed string values.
    """
    values = ws.get_all_values()
    if not values:
        return [], {c: None for c in cols}, []
    header_i = _detect_header_row(values, cols)
    headers = values[header_i]
    idx = {c: _header_index(headers, c) for c in cols}
    rows = []
    for r, raw in enumerate(values[header_i + 1:], start=header_i + 2):
        rec = {"_row": r}
        for c in cols:
            i = idx[c]
            rec[c] = _norm(raw[i]) if (i is not None and i < len(raw)) else ""
        rows.append(rec)
    return headers, idx, rows


def _build_row(headers, mapping):
    """Build a row list aligned to ``headers`` from a {column_name: value} map."""
    row = [""] * len(headers)
    lowered = {k.strip().lower(): v for k, v in mapping.items()}
    for i, h in enumerate(headers):
        key = _norm(h).lower()
        if key in lowered:
            row[i] = lowered[key]
    return row


# --- Group tabs ------------------------------------------------------------
def get_group_worksheets():
    """Every worksheet that represents a student group (special tabs skipped)."""
    return [
        ws
        for ws in get_spreadsheet().worksheets()
        if ws.title.strip().lower() not in config.SPECIAL_TABS
    ]


def iter_group_records():
    """Yield a dict for every data row across all group tabs."""
    cols = (
        config.COL_NAME,
        config.COL_TG,
        config.COL_AMOUNT,
        config.COL_STATUS,
        config.COL_TOTAL,
    )
    records = []
    for ws in get_group_worksheets():
        _, _, rows = _read(ws, cols)
        for row in rows:
            records.append(
                {
                    "worksheet": ws,
                    "group": ws.title,
                    "row_number": row["_row"],
                    "name": row[config.COL_NAME],
                    "tg": row[config.COL_TG],
                    "amount": row[config.COL_AMOUNT],
                    "status": row[config.COL_STATUS],
                    "total": row[config.COL_TOTAL],
                }
            )
    return records


def find_student(username):
    """First group row whose TG Contact matches ``username``, or None."""
    target = normalize_username(username)
    if not target:
        return None
    for rec in iter_group_records():
        if normalize_username(rec["tg"]) == target:
            return rec
    return None


# --- Bot Data tab ----------------------------------------------------------
_BD_COLS = (
    config.BD_USERNAME,
    config.BD_CHAT_ID,
    config.BD_FIRST_LINKED,
    config.BD_LAST_STATUS,
    config.BD_PROOF_LINK,
    config.BD_PROOF_DATE,
)


def _bot_data_ws():
    return _ws(config.BOT_DATA_TAB)


def get_bot_data_map():
    """Read Bot Data once into {normalized_username: record}.

    Use this in loops instead of calling find_bot_data_row per row, to avoid
    hammering the Sheets API.
    """
    _, _, rows = _read(_bot_data_ws(), _BD_COLS)
    result = {}
    for row in rows:
        u = normalize_username(row[config.BD_USERNAME])
        if not u:
            continue
        result[u] = {
            "row_number": row["_row"],
            "username": row[config.BD_USERNAME],
            "chat_id": row[config.BD_CHAT_ID],
            "first_linked": row[config.BD_FIRST_LINKED],
            "last_status": row[config.BD_LAST_STATUS],
        }
    return result


def find_bot_data_row(username):
    """Return the Bot Data record for ``username`` (or None)."""
    target = normalize_username(username)
    if not target:
        return None
    _, _, rows = _read(_bot_data_ws(), _BD_COLS)
    for row in rows:
        if normalize_username(row[config.BD_USERNAME]) == target:
            return {
                "row_number": row["_row"],
                "username": row[config.BD_USERNAME],
                "chat_id": row[config.BD_CHAT_ID],
                "first_linked": row[config.BD_FIRST_LINKED],
                "last_status": row[config.BD_LAST_STATUS],
            }
    return None


def register_user(username, chat_id):
    """Ensure the user exists in Bot Data.

    Appends a new row if absent; refreshes chat_id if it changed. Returns
    "created" or "exists".
    """
    ws = _bot_data_ws()
    headers, idx, rows = _read(ws, _BD_COLS)
    target = normalize_username(username)
    for row in rows:
        if normalize_username(row[config.BD_USERNAME]) == target:
            if row[config.BD_CHAT_ID] != str(chat_id):
                col = idx[config.BD_CHAT_ID]
                if col is not None:
                    ws.update_cell(row["_row"], col + 1, str(chat_id))
            return "exists"

    new_row = _build_row(
        headers,
        {
            config.BD_USERNAME: normalize_username(username),
            config.BD_CHAT_ID: str(chat_id),
            config.BD_FIRST_LINKED: today_str(),
            config.BD_LAST_STATUS: "",
        },
    )
    ws.append_row(new_row, value_input_option="USER_ENTERED")
    return "created"


def set_payment_proof(username, link, date_str):
    """Write a payment-proof link + submission date into the user's Bot Data row.

    Assumes the user already has a Bot Data row (call register_user first).
    Returns True on success, False if the user/columns weren't found.
    """
    ws = _bot_data_ws()
    _, idx, rows = _read(ws, _BD_COLS)
    target = normalize_username(username)
    link_col = idx.get(config.BD_PROOF_LINK)
    date_col = idx.get(config.BD_PROOF_DATE)
    if link_col is None or date_col is None:
        return False  # proof columns missing from Bot Data
    for row in rows:
        if normalize_username(row[config.BD_USERNAME]) == target:
            ws.update_cell(row["_row"], link_col + 1, link)
            ws.update_cell(row["_row"], date_col + 1, date_str)
            return True
    return False


def _cached_headers(ws, cols, cache_key):
    """Detect and cache a worksheet's header row (headers are stable)."""
    if cache_key not in _header_cache:
        vals = ws.get_all_values()
        _header_cache[cache_key] = vals[_detect_header_row(vals, cols)] if vals else []
    return _header_cache[cache_key]


def update_last_known_status(row_number, status):
    ws = _bot_data_ws()
    headers = _cached_headers(ws, _BD_COLS, "bot_data")
    col = _header_index(headers, config.BD_LAST_STATUS)
    if col is not None:
        ws.update_cell(row_number, col + 1, status)


# --- Send Log tab ----------------------------------------------------------
_SL_COLS = (
    config.SL_DATE_SENT,
    config.SL_GROUP_TAB,
    config.SL_STUDENT_NAME,
    config.SL_TG_CONTACT,
    config.SL_RESULT,
    config.SL_REMINDER_NUM,
)


def _send_log_ws():
    return _ws(config.SEND_LOG_TAB)


def get_sent_counts():
    """Read Send Log once into {normalized_username: count of 'sent' rows}."""
    _, _, rows = _read(_send_log_ws(), _SL_COLS)
    counts: dict = {}
    for row in rows:
        if row[config.SL_RESULT].strip().lower() == "sent":
            u = normalize_username(row[config.SL_TG_CONTACT])
            if u:
                counts[u] = counts.get(u, 0) + 1
    return counts


def count_sent(username):
    """How many successful ('sent') reminders this user already received."""
    target = normalize_username(username)
    _, _, rows = _read(_send_log_ws(), _SL_COLS)
    return sum(
        1
        for row in rows
        if normalize_username(row[config.SL_TG_CONTACT]) == target
        and row[config.SL_RESULT].strip().lower() == "sent"
    )


def append_send_log(group_tab, student_name, tg_contact, result, reminder_number):
    ws = _send_log_ws()
    headers = _cached_headers(ws, _SL_COLS, "send_log")
    row = _build_row(
        headers,
        {
            config.SL_DATE_SENT: today_str(),
            config.SL_GROUP_TAB: group_tab,
            config.SL_STUDENT_NAME: student_name,
            config.SL_TG_CONTACT: tg_contact,
            config.SL_RESULT: result,
            config.SL_REMINDER_NUM: str(reminder_number) if reminder_number else "",
        },
    )
    ws.append_row(row, value_input_option="USER_ENTERED")
