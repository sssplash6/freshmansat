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
import logging

log = logging.getLogger(__name__)
from google.oauth2.service_account import Credentials

import config

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_spreadsheet = None
_penalty_spreadsheet = None
_admin_panel_spreadsheet = None
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


def get_penalty_spreadsheet():
    """Return the (cached) Penalty Tracker Spreadsheet handle.

    Separate spreadsheet from the payment sheet — the same service account
    (credentials.json) must be shared as at least Viewer on this sheet too,
    or this will raise a permissions error on first use.
    """
    global _penalty_spreadsheet
    if _penalty_spreadsheet is None:
        creds = Credentials.from_service_account_file(
            str(config.CREDENTIALS_FILE), scopes=SCOPES
        )
        client = gspread.authorize(creds)
        _penalty_spreadsheet = client.open_by_key(config.PENALTY_SHEET_ID)
    return _penalty_spreadsheet


def get_admin_panel_spreadsheet():
    """Return the (cached) Admin Panel Spreadsheet handle.

    This is the spreadsheet Code.gs is bound to. The bot only ever writes to
    its Payment_Log tab (audit trail) — it never touches Finance sheet data
    from this handle, and Apps Script never touches Finance directly either.
    The service account needs at least Editor access to this spreadsheet.
    """
    global _admin_panel_spreadsheet
    if _admin_panel_spreadsheet is None:
        creds = Credentials.from_service_account_file(
            str(config.CREDENTIALS_FILE), scopes=SCOPES
        )
        client = gspread.authorize(creds)
        _admin_panel_spreadsheet = client.open_by_key(config.ADMIN_PANEL_SHEET_ID)
    return _admin_panel_spreadsheet


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


def now_str() -> str:
    return datetime.datetime.now().isoformat(sep=" ", timespec="seconds")


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
    config.BD_PAY_SHOWN
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
            "pay_shown": row[config.BD_PAY_SHOWN],
            "proof_link": row[config.BD_PROOF_LINK],
        }
    return result

def mark_pay_shown(username):
    """Record that this user has been shown payment details (via /pay or a reminder)."""
    ws = _bot_data_ws()
    headers = _cached_headers(ws, _BD_COLS, "bot_data")
    col = _header_index(headers, config.BD_PAY_SHOWN)
    if col is None:
        return
    _, idx, rows = _read(ws, _BD_COLS)
    target = normalize_username(username)
    for row in rows:
        if normalize_username(row[config.BD_USERNAME]) == target:
            ws.update_cell(row["_row"], col + 1, today_str())
            return

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
                "pay_shown": row[config.BD_PAY_SHOWN],
            }
    return None

def clear_payment_proof(username):
    """Clear proof link/date for a user, e.g. after rejecting a submission."""
    ws = _bot_data_ws()
    _, idx, rows = _read(ws, _BD_COLS)
    target = normalize_username(username)
    link_col = idx.get(config.BD_PROOF_LINK)
    date_col = idx.get(config.BD_PROOF_DATE)
    if link_col is None or date_col is None:
        return False
    for row in rows:
        if normalize_username(row[config.BD_USERNAME]) == target:
            ws.update_cell(row["_row"], link_col + 1, "")
            ws.update_cell(row["_row"], date_col + 1, "")
            return True
    return False

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

def set_finance_status(ws, row_number, status):
    """Sets Status for a specific row in a Finance group tab to any value
    (Paid, Pending, Scholarship, Cancelled, etc.) — replaces the old
    Paid-only / Unpaid-only functions with one generic write. Only updates
    Date of Payment when the new status is Paid, so a historical record of
    when they last paid is preserved even if status later changes again.
    """
    values = ws.get_all_values()
    header_i = _detect_header_row(values, (config.COL_STATUS, config.COL_DATE_OF_PAYMENT))
    headers = values[header_i]

    status_col = _header_index(headers, config.COL_STATUS)
    date_col = _header_index(headers, config.COL_DATE_OF_PAYMENT)

    if status_col is not None:
        ws.update_cell(row_number, status_col + 1, status)
    if status.strip().lower() == "paid" and date_col is not None:
        ws.update_cell(row_number, date_col + 1, today_str())


def add_finance_student(group_tab_name, name, tg_handle, amount, status="Pending"):
    """Adds a new student row directly to a Finance group tab — creating
    that tab (with proper headers) first if it doesn't exist yet, so this
    never fails to write just because a group tab hasn't been set up in
    Finance yet.

    This is what makes a newly-added student actually payable/findable —
    Roster (Admin Panel) and Finance (this spreadsheet's group tabs) are
    two separate systems by design, so adding someone to Roster alone does
    NOT make them exist in Finance. This function is the other half of
    student creation.
    """
    ss = get_spreadsheet()
    cols = (config.COL_NAME, config.COL_TG, config.COL_AMOUNT, config.COL_STATUS, config.COL_DATE_OF_PAYMENT)
    try:
        ws = ss.worksheet(group_tab_name)
        values = ws.get_all_values()
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title=group_tab_name, rows=200, cols=len(cols))
        ws.append_row(list(cols))
        values = ws.get_all_values()

    header_i = _detect_header_row(values, cols) if values else 0
    headers = values[header_i] if values else list(cols)
    row = _build_row(headers, {
        config.COL_NAME: name,
        config.COL_TG: normalize_username(tg_handle),
        config.COL_AMOUNT: amount,
        config.COL_STATUS: status,
    })
    ws.append_row(row, value_input_option="USER_ENTERED")

    # Clear the cached worksheet lookup so future find_student()/iter_group_records()
    # calls see the row we just added.
    _ws_cache.pop(group_tab_name, None)





# --- Payment_Log tab (Admin Panel spreadsheet) ------------------------------
_PAYMENT_LOG_TAB = "Payment_Log"
_PAYMENT_LOG_HEADERS = ["Timestamp", "Student", "Username", "Status", "Amount", "Source"]


def log_payment_change(student_name, username, new_status, source, amount=""):
    """Append an audit row to Payment_Log in the Admin Panel spreadsheet.

    This is the ONLY thing the Admin Panel (Apps Script side) ever reads for
    payment history — it never touches the Finance sheet directly. Called
    from bot.py's set_payment_status(), always alongside the
    Finance sheet write, so the two can never drift apart.

    source: 'proof' (student-submitted photo, admin tapped Approve) or
            'admin_override' (/admin_setpayment command)
    """
    admin_panel = get_admin_panel_spreadsheet()
    ws = admin_panel.worksheet(_PAYMENT_LOG_TAB)
    ws.append_row(
        [now_str(), student_name, normalize_username(username), new_status, amount, source],
        value_input_option="USER_ENTERED",
    )


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


# --- Admin Panel: Groups / Roster / Attendance_Log / Penalty_Log (read) ----
# These read the SEPARATE Admin Panel spreadsheet (Code.gs's spreadsheet),
# not the payment SHEET_ID above. Used by the attendance alert job.

_ADMIN_GROUPS_COLS = ("Group Name", "Days", "Start Time", "End Time")
_ADMIN_ROSTER_COLS = ("Student Name", "TG Handle", "Group")
_ADMIN_ATTENDANCE_LOG_COLS = ("Student", "Group", "Session Date", "Status")
_ADMIN_PENALTY_LOG_COLS = ("Student", "Points", "Status")

_ATTENDANCE_ALERT_LOG_TAB = "AttendanceAlertLog"


def get_groups_schedule():
    """Reads the Admin Panel's Groups tab. Returns a list of dicts:
    {name, days: ['Mon','Wed',...], start_time: 'HH:MM'}.
    """
    ws = get_admin_panel_spreadsheet().worksheet("Groups")
    values = ws.get_all_values()
    if not values:
        return []
    headers = values[0]
    idx = {c: _header_index(headers, c) for c in _ADMIN_GROUPS_COLS}
    result = []
    for raw in values[1:]:
        def get(col):
            i = idx.get(col)
            return _norm(raw[i]) if (i is not None and i < len(raw)) else ""
        name = get("Group Name")
        if not name:
            continue
        result.append({
            "name": name,
            "days": [d.strip() for d in get("Days").split(",") if d.strip()],
            "start_time": get("Start Time"),
        })
    return result


def get_roster_map():
    """Reads the Admin Panel's Roster tab into {student_name: {tg, group}}."""
    ws = get_admin_panel_spreadsheet().worksheet("Roster")
    values = ws.get_all_values()
    if not values:
        return {}
    headers = values[0]
    idx = {c: _header_index(headers, c) for c in _ADMIN_ROSTER_COLS}
    result = {}
    for raw in values[1:]:
        def get(col):
            i = idx.get(col)
            return _norm(raw[i]) if (i is not None and i < len(raw)) else ""
        name = get("Student Name")
        if not name:
            continue
        result[name] = {"tg": get("TG Handle"), "group": get("Group")}
    return result


def get_todays_attendance_for_group(group_name, date_str):
    """Reads Attendance_Log rows for `group_name` whose Session Date matches
    `date_str` (YYYY-MM-DD), returning only Late/Absent rows.
    """
    ws = get_admin_panel_spreadsheet().worksheet("Attendance_Log")
    values = ws.get_all_values()
    if not values:
        return []
    headers = values[0]
    idx = {c: _header_index(headers, c) for c in _ADMIN_ATTENDANCE_LOG_COLS}
    result = []
    for raw in values[1:]:
        def get(col):
            i = idx.get(col)
            return _norm(raw[i]) if (i is not None and i < len(raw)) else ""
        if get("Group") != group_name:
            continue
        if get("Status") not in ("Late", "Absent"):
            continue
        session_date_raw = get("Session Date")
        # Session Date may be a full date string (e.g. from a Date-typed
        # header cell) — match on just the YYYY-MM-DD portion.
        if not session_date_raw.startswith(date_str) and date_str not in session_date_raw:
            # Fall back to parsing common formats if the direct match fails.
            try:
                parsed = datetime.datetime.fromisoformat(session_date_raw[:19])
                if parsed.strftime("%Y-%m-%d") != date_str:
                    continue
            except ValueError:
                continue
        result.append({"student": get("Student"), "status": get("Status")})
    return result


def get_penalty_total(student_name):
    """Sums Active points for a student from the Admin Panel's Penalty_Log."""
    ws = get_admin_panel_spreadsheet().worksheet("Penalty_Log")
    values = ws.get_all_values()
    if not values:
        return 0
    headers = values[0]
    idx = {c: _header_index(headers, c) for c in _ADMIN_PENALTY_LOG_COLS}
    total = 0
    for raw in values[1:]:
        def get(col):
            i = idx.get(col)
            return _norm(raw[i]) if (i is not None and i < len(raw)) else ""
        if get("Student") != student_name or get("Status") != "Active":
            continue
        try:
            total += int(float(get("Points") or 0))
        except ValueError:
            pass
    return total


def _attendance_alert_log_ws():
    ss = get_admin_panel_spreadsheet()
    try:
        return ss.worksheet(_ATTENDANCE_ALERT_LOG_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title=_ATTENDANCE_ALERT_LOG_TAB, rows=100, cols=1)
        ws.append_row(["Alert Key"])
        return ws


def get_alerted_keys():
    """Dedup guard so the same student/session never gets alerted twice."""
    ws = _attendance_alert_log_ws()
    values = ws.get_all_values()
    if len(values) < 2:
        return set()
    return {row[0] for row in values[1:] if row}


def log_alert(key):
    _attendance_alert_log_ws().append_row([key])



def search_roster(query):
    """Case-insensitive substring match against student name or TG handle.
    Returns a list of {name, tg, group} dicts — reads raw rows so a
    multi-group student appears once per group they're actually in."""
    query = query.strip().lower().lstrip("@")
    if not query:
        return []
    results = [
        row for row in get_roster_rows(include_inactive=True)
        if query in row["name"].lower() or query in row["tg"].lower().lstrip("@")
    ]
    return results[:10]


def get_roster_rows(include_inactive=False):
    """Every row in Roster, with NO deduplication by name — needed so a
    student enrolled in more than one group (same name, same TG handle, two
    separate rows with different Group values) shows up correctly in each
    group's browse list. Returns [{row_number, name, tg, group, status}].

    By default skips rows where Status is 'inactive' (soft-deleted students)
    — pass include_inactive=True when you specifically need to see them
    (e.g. searching up a departed student's history).
    """
    ws = get_admin_panel_spreadsheet().worksheet("Roster")
    values = ws.get_all_values()
    if len(values) < 2:
        return []
    headers = values[0]
    idx = {c: _header_index(headers, c) for c in ("Student Name", "TG Handle", "Group", "Status")}
    rows = []
    for r, raw in enumerate(values[1:], start=2):
        def get(col):
            i = idx.get(col)
            return _norm(raw[i]) if (i is not None and i < len(raw)) else ""
        name = get("Student Name")
        if not name:
            continue
        status = get("Status") or "active"
        if status.lower() == "inactive" and not include_inactive:
            continue
        rows.append({"row_number": r, "name": name, "tg": get("TG Handle"), "group": get("Group"), "status": status})
    return rows


def get_roster_map():
    """Reads Roster into {student_name: {tg, group}} (active students only).

    NOTE: if a student has multiple Roster rows (enrolled in more than one
    group), this dict can only hold one entry per name — 'group' here ends
    up being whichever row was read last, and is mainly useful for TG handle
    lookup, not group membership. Use get_roster_by_group() or
    get_roster_rows() for anything that needs to respect multi-group
    students correctly.
    """
    result = {}
    for row in get_roster_rows():
        result[row["name"]] = {"tg": row["tg"], "group": row["group"]}
    return result


def get_roster_by_group(group_name):
    """Active roster entries for a specific group, as {name, tg, group} dicts.
    Reads raw rows (not the deduped map) so multi-group students correctly
    appear in every group they're actually enrolled in."""
    return [row for row in get_roster_rows() if row["group"] == group_name]

def get_students_for_group(group_name):
    """Alias used by the attendance-marking flow."""
    return get_roster_by_group(group_name)


def _parse_session_date(raw: str):
    """Parses a Session Date cell that may be a clean 'YYYY-MM-DD' string
    (from Telegram-originated marks) or a messy JS Date.toString() (from the
    original Apps Script wide-sheet sync, e.g. 'Sat Aug 15 2026 00:00:00
    GMT+0500 ...'). Returns a datetime.date or None."""
    raw = raw.strip()
    try:
        return datetime.date.fromisoformat(raw[:10])
    except ValueError:
        pass
    months = {m: i + 1 for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}
    import re
    m = re.search(r"(\w{3}) (\d{1,2}) (\d{4})", raw)
    if m and m.group(1) in months:
        return datetime.date(int(m.group(3)), months[m.group(1)], int(m.group(2)))
    return None


def get_last_session_date(group_name):
    """Most recent Session Date logged for this group in Attendance_Log,
    as 'YYYY-MM-DD', or None if there's no history yet."""
    ws = get_admin_panel_spreadsheet().worksheet("Attendance_Log")
    values = ws.get_all_values()
    if len(values) < 2:
        return None
    headers = values[0]
    idx = {c: _header_index(headers, c) for c in ("Group", "Session Date")}
    g_i, d_i = idx.get("Group"), idx.get("Session Date")
    if g_i is None or d_i is None:
        return None
    best = None
    for raw in values[1:]:
        if g_i >= len(raw) or _norm(raw[g_i]) != group_name:
            continue
        d = _parse_session_date(_norm(raw[d_i])) if d_i < len(raw) else None
        if d and (best is None or d > best):
            best = d
    return best.isoformat() if best else None


def get_attendance_for_date_group(date_str, group_name):
    """{student_name: status} already marked for this exact date+group."""
    ws = get_admin_panel_spreadsheet().worksheet("Attendance_Log")
    values = ws.get_all_values()
    if len(values) < 2:
        return {}
    headers = values[0]
    idx = {c: _header_index(headers, c) for c in ("Student", "Group", "Session Date", "Status")}
    target_date = datetime.date.fromisoformat(date_str)
    result = {}
    for raw in values[1:]:
        def get(col):
            i = idx.get(col)
            return _norm(raw[i]) if (i is not None and i < len(raw)) else ""
        if get("Group") != group_name:
            continue
        d = _parse_session_date(get("Session Date"))
        if d == target_date:
            result[get("Student")] = get("Status")
    return result


def _find_attendance_log_row(student_name, group_name, target_date):
    ws = get_admin_panel_spreadsheet().worksheet("Attendance_Log")
    values = ws.get_all_values()
    if len(values) < 2:
        return None, None
    headers = values[0]
    idx = {c: _header_index(headers, c) for c in ("Student", "Group", "Session Date", "Status")}
    for r, raw in enumerate(values[1:], start=2):
        def get(col):
            i = idx.get(col)
            return _norm(raw[i]) if (i is not None and i < len(raw)) else ""
        if get("Student") == student_name and get("Group") == group_name:
            if _parse_session_date(get("Session Date")) == target_date:
                return r, get("Status")
    return None, None


def _remove_penalty_by_reason(student_name, reason):
    ws = get_admin_panel_spreadsheet().worksheet("Penalty_Log")
    values = ws.get_all_values()
    if len(values) < 2:
        return
    headers = values[0]
    idx = {c: _header_index(headers, c) for c in ("Student", "Reason", "Status")}
    s_i, r_i, st_i = idx.get("Student"), idx.get("Reason"), idx.get("Status")
    status_col = idx.get("Status")
    for r, raw in enumerate(values[1:], start=2):
        if (s_i is not None and s_i < len(raw) and _norm(raw[s_i]) == student_name
                and r_i is not None and r_i < len(raw) and _norm(raw[r_i]) == reason
                and st_i is not None and st_i < len(raw) and _norm(raw[st_i]) == "Active"):
            ws.update_cell(r, status_col + 1, "Removed")


def mark_attendance(date_str, group_name, student_name, status):
    """Writes/updates one attendance mark and keeps Penalty_Log in sync —
    reverses the old status's penalty (if any) before applying the new
    one, so using the Change button to correct a mistake doesn't leave
    stale or duplicate penalties behind."""
    target_date = datetime.date.fromisoformat(date_str)
    row_number, old_status = _find_attendance_log_row(student_name, group_name, target_date)

    if old_status == status:
        return True  # no-op, already this status

    if old_status == "Absent":
        _remove_penalty_by_reason(student_name, f"Unexcused absence ({date_str})")
    elif old_status == "Late":
        _remove_penalty_by_reason(student_name, f"Lateness ({date_str})")

    ws = get_admin_panel_spreadsheet().worksheet("Attendance_Log")
    if row_number:
        headers = ws.row_values(1)
        status_col = _header_index(headers, "Status")
        marked_by_col = _header_index(headers, "Marked By")
        if status_col is not None:
            ws.update_cell(row_number, status_col + 1, status)
        if marked_by_col is not None:
            ws.update_cell(row_number, marked_by_col + 1, "Telegram Admin")
    else:
        ws.append_row([now_str(), student_name, group_name, date_str, status, "Telegram Admin"],
                       value_input_option="USER_ENTERED")

    if status == "Absent":
        add_manual_penalty(student_name, group_name, f"Unexcused absence ({date_str})", 1, "System")
    elif status == "Late":
        # Count this student's total Late marks in this group to apply the
        # every-3rd-lateness rule, same as the Apps Script sync logic.
        ws2 = get_admin_panel_spreadsheet().worksheet("Attendance_Log")
        values = ws2.get_all_values()
        headers = values[0]
        idx = {c: _header_index(headers, c) for c in ("Student", "Group", "Status")}
        late_count = sum(
            1 for raw in values[1:]
            if idx.get("Student") is not None and idx["Student"] < len(raw) and _norm(raw[idx["Student"]]) == student_name
            and idx.get("Group") is not None and idx["Group"] < len(raw) and _norm(raw[idx["Group"]]) == group_name
            and idx.get("Status") is not None and idx["Status"] < len(raw) and _norm(raw[idx["Status"]]) == "Late"
        )
        if late_count % 3 == 0:
            add_manual_penalty(student_name, group_name, f"Lateness ({date_str})", 1, "System")

    return True

def add_roster_student(name, tg_handle, group):
    """Adds a student to Roster for a given group (Status: active).

    If a row for this exact name+group already exists — even if it's
    currently inactive (soft-deleted) — that row is REACTIVATED (status set
    back to active, TG handle refreshed) instead of appending a duplicate.
    This is what makes "remove student" then "add student" again behave
    like undo, rather than leaving two rows for the same enrollment.

    Also appends them to that group's attendance entry tab if they're not
    already listed there, so the teacher can start marking them.
    """
    ss = get_admin_panel_spreadsheet()
    roster_ws = ss.worksheet("Roster")

    existing = [r for r in get_roster_rows(include_inactive=True) if r["name"] == name and r["group"] == group]
    if existing:
        row_number = existing[0]["row_number"]
        headers = roster_ws.row_values(1)
        status_col = _header_index(headers, "Status")
        tg_col = _header_index(headers, "TG Handle")
        if status_col is not None:
            roster_ws.update_cell(row_number, status_col + 1, "active")
        if tg_col is not None and tg_handle:
            roster_ws.update_cell(row_number, tg_col + 1, tg_handle)
    else:
        headers = roster_ws.row_values(1)
        row = [""] * len(headers)
        values_map = {
            "student name": name,
            "tg handle": tg_handle,
            "group": group,
            "status": "active",
        }
        for i, h in enumerate(headers):
            key = h.strip().lower()
            if key in values_map:
                row[i] = values_map[key]
        roster_ws.append_row(row, value_input_option="USER_ENTERED")

    attendance_tab_name = f"Attendance - {group}"
    try:
        attendance_ws = ss.worksheet(attendance_tab_name)
        existing_names = [r[0] for r in attendance_ws.get_all_values()[1:]] if attendance_ws.row_count > 1 else []
        if name not in existing_names:
            attendance_ws.append_row([name, tg_handle], value_input_option="USER_ENTERED")
    except gspread.exceptions.WorksheetNotFound:
        log.warning(
            "add_roster_student: no attendance tab '%s' found — student added to "
            "Roster only. Register the group first if this is unexpected.",
            attendance_tab_name,
        )


def set_student_status(row_number, status):
    """Soft-delete/restore: sets a specific Roster row's Status column.
    Takes row_number (not name) since a student can have multiple rows
    across groups and only one should usually be touched at a time.
    """
    ws = get_admin_panel_spreadsheet().worksheet("Roster")
    headers = ws.row_values(1)
    col = _header_index(headers, "Status")
    if col is not None:
        ws.update_cell(row_number, col + 1, status)


def get_roster_rows_for_student(student_name, include_inactive=True):
    """All Roster rows (across every group) for one student name — used by
    the remove-student flow, which needs to know every group they're in."""
    return [r for r in get_roster_rows(include_inactive=include_inactive) if r["name"] == student_name]


def add_group(name, days_csv, start_time, end_time):
    """Adds a new group: appends a Groups row and creates its
    'Attendance - <name>' entry tab (header only — starts empty, students
    get added to it as they're added via add_roster_student, or manually).
    Safe to call again for an existing group name — skips creating a
    duplicate Groups row or tab if either already exists.
    """
    ss = get_admin_panel_spreadsheet()
    groups_ws = ss.worksheet("Groups")
    existing_names = {g["name"] for g in get_groups_schedule()}

    if name not in existing_names:
        headers = groups_ws.row_values(1)
        row = [""] * len(headers)
        values_map = {"group name": name, "days": days_csv, "start time": start_time, "end time": end_time}
        for i, h in enumerate(headers):
            key = h.strip().lower()
            if key in values_map:
                row[i] = values_map[key]
        groups_ws.append_row(row, value_input_option="USER_ENTERED")

    tab_name = f"Attendance - {name}"
    try:
        ss.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        new_ws = ss.add_worksheet(title=tab_name, rows=100, cols=10)
        new_ws.append_row(["Student Name", "TG Handle"])
        new_ws.freeze(rows=1)


def remove_group(name):
    """Deletes ONLY the Groups row for `name`. Deliberately does not touch
    Roster, the attendance tab, or any log — a discontinued group's history
    stays intact, it just stops being scheduled/synced going forward.
    Returns True if a row was found and deleted, False otherwise.
    """
    ws = get_admin_panel_spreadsheet().worksheet("Groups")
    values = ws.get_all_values()
    headers = values[0]
    name_col = _header_index(headers, "Group Name")
    if name_col is None:
        return False
    for r, raw in enumerate(values[1:], start=2):
        if name_col < len(raw) and _norm(raw[name_col]) == name:
            ws.delete_rows(r)
            return True
    return False





def get_active_penalties(student_name):
    """Active Penalty_Log rows for a student, with sheet row numbers so they
    can be individually removed. Returns [{row_number, reason, points, group}]."""
    ws = get_admin_panel_spreadsheet().worksheet("Penalty_Log")
    values = ws.get_all_values()
    if len(values) < 2:
        return []
    headers = values[0]
    idx = {c: _header_index(headers, c) for c in ("Student", "Group", "Reason", "Points", "Status")}
    result = []
    for r, raw in enumerate(values[1:], start=2):
        def get(col):
            i = idx.get(col)
            return _norm(raw[i]) if (i is not None and i < len(raw)) else ""
        if get("Student") != student_name or get("Status") != "Active":
            continue
        result.append({
            "row_number": r,
            "group": get("Group"),
            "reason": get("Reason"),
            "points": get("Points"),
        })
    return result


def add_manual_penalty(student_name, group, reason, points, assigned_by):
    """Appends a manually-assigned penalty to Penalty_Log (Admin Panel)."""
    ws = get_admin_panel_spreadsheet().worksheet("Penalty_Log")
    ws.append_row(
        [now_str(), student_name, group, reason, points, "Manual", assigned_by, "Active"],
        value_input_option="USER_ENTERED",
    )


def remove_admin_panel_penalty(row_number):
    """Soft-deletes a Penalty_Log row by setting Status to 'Removed'."""
    ws = get_admin_panel_spreadsheet().worksheet("Penalty_Log")
    values = ws.get_all_values()
    headers = values[0]
    status_col = _header_index(headers, "Status")
    if status_col is not None:
        ws.update_cell(row_number, status_col + 1, "Removed")


def get_latest_payment_status(username):
    """Most recent Payment_Log entry for a username, or None."""
    ws = get_admin_panel_spreadsheet().worksheet("Payment_Log")
    values = ws.get_all_values()
    if len(values) < 2:
        return None
    headers = values[0]
    idx = {c: _header_index(headers, c) for c in ("Username", "Status", "Amount", "Timestamp")}
    target = normalize_username(username)
    latest = None
    for raw in values[1:]:
        def get(col):
            i = idx.get(col)
            return _norm(raw[i]) if (i is not None and i < len(raw)) else ""
        if normalize_username(get("Username")) == target:
            latest = {"status": get("Status"), "amount": get("Amount"), "timestamp": get("Timestamp")}
    return latest


def get_student_profile(student_name):
    """Aggregates everything the admin panel needs for a full student card:
    roster info, attendance breakdown, homework breakdown, penalty total,
    and latest payment status. Reads Admin Panel tabs only.

    Attendance/homework/penalty totals are matched by student name alone
    (no group filter), so a student enrolled in multiple groups correctly
    gets one combined profile across all their groups — as long as their
    name is spelled identically everywhere (Roster + each group's
    attendance tab + Homework_Log/Penalty_Log).
    """
    matching_rows = [r for r in get_roster_rows(include_inactive=True) if r["name"] == student_name]
    if not matching_rows:
        return None

    active_rows = [r for r in matching_rows if r["status"].lower() == "active"]
    # Prefer active rows for display — an old inactive duplicate shouldn't
    # override a genuinely active enrollment (e.g. remove-then-re-add).
    display_rows = active_rows if active_rows else matching_rows
    tg = next((r["tg"] for r in display_rows if r["tg"]), "")
    # Dedupe group names while preserving order (a student can legitimately
    # be in the same group only once, but defensive against stale duplicates).
    seen_groups = []
    for r in display_rows:
        if r["group"] and r["group"] not in seen_groups:
            seen_groups.append(r["group"])
    groups = seen_groups
    roster_status = "active" if active_rows else (matching_rows[-1]["status"] if matching_rows else "unknown")

    ws = get_admin_panel_spreadsheet().worksheet("Attendance_Log")
    values = ws.get_all_values()
    attendance_counts = {"Present": 0, "Late": 0, "Absent": 0}
    if len(values) >= 2:
        headers = values[0]
        idx = {c: _header_index(headers, c) for c in ("Student", "Status")}
        i_s, i_st = idx.get("Student"), idx.get("Status")
        if i_s is not None and i_st is not None:
            for raw in values[1:]:
                if i_s < len(raw) and _norm(raw[i_s]) == student_name:
                    status = _norm(raw[i_st]) if i_st < len(raw) else ""
                    if status in attendance_counts:
                        attendance_counts[status] += 1

    ws = get_admin_panel_spreadsheet().worksheet("Homework_Log")
    values = ws.get_all_values()
    homework_counts = {"On Time": 0, "Late": 0, "Missing": 0}
    if len(values) >= 2:
        headers = values[0]
        idx = {c: _header_index(headers, c) for c in ("Student", "Status")}
        i_s, i_st = idx.get("Student"), idx.get("Status")
        if i_s is not None and i_st is not None:
            for raw in values[1:]:
                if i_s < len(raw) and _norm(raw[i_s]) == student_name:
                    status = _norm(raw[i_st]) if i_st < len(raw) else ""
                    if status in homework_counts:
                        homework_counts[status] += 1

    total_points = get_penalty_total(student_name)
    payment = get_latest_payment_status(tg) if tg else None

    return {
        "name": student_name,
        "tg": tg,
        "group": ", ".join(groups) if groups else "",
        "roster_status": roster_status,
        "attendance": attendance_counts,
        "homework": homework_counts,
        "total_points": total_points,
        "payment": payment,
    }


_PL_COLS = (
    config.PL_TG_HANDLE,
    config.PL_STUDENT_NAME,
    config.PL_CLASS,
    config.PL_TOTAL_POINTS,
)


def find_penalty_record(username):
    """Look up a student's total penalty points by TG handle, or None.

    Reads the 'PenaltyLookupForBot' tab in the separate Penalty Tracker
    spreadsheet, which is refreshed automatically whenever penalties change
    on that sheet's side. This function is read-only.
    """
    target = normalize_username(username)
    if not target:
        return None

    ws = get_penalty_spreadsheet().worksheet(config.PENALTY_LOOKUP_TAB)
    values = ws.get_all_values()
    if not values:
        return None

    headers = values[0]
    idx = {c: _header_index(headers, c) for c in _PL_COLS}
    tg_i = idx.get(config.PL_TG_HANDLE)
    if tg_i is None:
        return None

    for raw in values[1:]:
        if tg_i >= len(raw):
            continue
        if normalize_username(raw[tg_i]) == target:
            def get(col):
                i = idx.get(col)
                return _norm(raw[i]) if (i is not None and i < len(raw)) else ""
            return {
                "name": get(config.PL_STUDENT_NAME),
                "class": get(config.PL_CLASS),
                "points": get(config.PL_TOTAL_POINTS),
            }
    return None