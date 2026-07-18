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


def find_student(query):
    """Find a student row in the payment sheet, matching flexibly so minor
    TG-handle drift between the payment sheet and the Admin Panel Roster
    doesn't cause a false 'not found'. Tries, in order:
      1. Exact TG handle match (original behavior)
      2. Exact student name match
      3. Substring name match (either direction)
      4. Substring TG handle match (either direction) — catches typos,
         truncation, or a changed handle where one is a prefix of the other
    Returns the first/best match, or None if nothing matches at all.
    """
    records = iter_group_records()
    target_tg = normalize_username(query)
    target_name = _norm(query).lower()

    if target_tg:
        for rec in records:
            if normalize_username(rec["tg"]) == target_tg:
                return rec

    if target_name:
        for rec in records:
            if rec["name"].strip().lower() == target_name:
                return rec

        contains = [
            r for r in records
            if target_name in r["name"].lower() or r["name"].lower() in target_name
        ]
        if contains:
            return contains[0]

    if target_tg:
        for rec in records:
            rec_tg = normalize_username(rec["tg"])
            if rec_tg and (rec_tg in target_tg or target_tg in rec_tg):
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

def set_student_status(ws, row_number, status_label):
    """Set Status for a specific row to one of: 'Paid', 'Scholarship',
    'Pending', 'Cancel'. Date of Payment is only touched when moving TO
    Paid or Scholarship — it's deliberately never cleared on Pending/Cancel,
    so a historical record of when they last paid survives even if their
    status is later reverted.
    """
    values = ws.get_all_values()
    header_i = _detect_header_row(values, (config.COL_STATUS, config.COL_DATE_OF_PAYMENT))
    headers = values[header_i]

    status_col = _header_index(headers, config.COL_STATUS)
    date_col = _header_index(headers, config.COL_DATE_OF_PAYMENT)

    if status_col is not None:
        ws.update_cell(row_number, status_col + 1, status_label)
    if status_label.strip().lower() in ("paid", "scholarship") and date_col is not None:
        ws.update_cell(row_number, date_col + 1, today_str())


# --- Payment_Log tab (Admin Panel spreadsheet) ------------------------------
_PAYMENT_LOG_TAB = "Payment_Log"
_PAYMENT_LOG_HEADERS = ["Timestamp", "Student", "Username", "Status", "Amount", "Source"]


def log_payment_change(student_name, username, new_status, source, amount=""):
    """Append an audit row to Payment_Log in the Admin Panel spreadsheet.

    This is the ONLY thing the Admin Panel (Apps Script side) ever reads for
    payment history — it never touches the Finance sheet directly. Called
    from bot.py's approve_payment() and set_unpaid(), always alongside the
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
        if row[config.SL_RESULT].strip().lower().startswith("sent"):
            u = normalize_username(row[config.SL_TG_CONTACT])
            if u:
                counts[u] = counts.get(u, 0) + 1
    return counts


def count_sent(username):
    """How many successful ('sent' or 'sent (manual)') reminders this user
    already received."""
    target = normalize_username(username)
    _, _, rows = _read(_send_log_ws(), _SL_COLS)
    return sum(
        1
        for row in rows
        if normalize_username(row[config.SL_TG_CONTACT]) == target
        and row[config.SL_RESULT].strip().lower().startswith("sent")
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
        row for row in get_roster_rows()
        if query in row["name"].lower() or query in row["tg"].lower().lstrip("@")
    ]
    return results[:10]


def get_roster_rows():
    """Every row in Roster as-is, with NO deduplication by name — needed so
    a student enrolled in more than one group (same name, same TG handle,
    two separate rows with different Group values) shows up correctly in
    each group's browse list. Returns [{name, tg, group}]."""
    ws = get_admin_panel_spreadsheet().worksheet("Roster")
    values = ws.get_all_values()
    if len(values) < 2:
        return []
    headers = values[0]
    idx = {c: _header_index(headers, c) for c in ("Student Name", "TG Handle", "Group")}
    rows = []
    for raw in values[1:]:
        def get(col):
            i = idx.get(col)
            return _norm(raw[i]) if (i is not None and i < len(raw)) else ""
        name = get("Student Name")
        if not name:
            continue
        rows.append({"name": name, "tg": get("TG Handle"), "group": get("Group")})
    return rows


def get_roster_map():
    """Reads Roster into {student_name: {tg, group}}.

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
    """All roster entries for a specific group, as {name, tg, group} dicts.
    Reads raw rows (not the deduped map) so multi-group students correctly
    appear in every group they're actually enrolled in."""
    return [row for row in get_roster_rows() if row["group"] == group_name]


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
    matching_rows = [r for r in get_roster_rows() if r["name"] == student_name]
    if not matching_rows:
        return None

    tg = next((r["tg"] for r in matching_rows if r["tg"]), "")
    groups = [r["group"] for r in matching_rows if r["group"]]

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


# --- Attendance Marking (Teacher UI) ----------------------------------------

def get_students_for_group(group_name: str) -> list:
    """Get all students in a group from Roster, returning [{name, tg}]."""
    ws = get_admin_panel_spreadsheet().worksheet("Roster")
    values = ws.get_all_values()
    if not values:
        return []
    headers = values[0]
    idx = {c: _header_index(headers, c) for c in ("Student Name", "TG Handle", "Group")}
    students = []
    for raw in values[1:]:
        def get(col):
            i = idx.get(col)
            return _norm(raw[i]) if (i is not None and i < len(raw)) else ""
        if get("Group") == group_name:
            name = get("Student Name")
            if name:
                students.append({"name": name, "tg": get("TG Handle")})
    return students


def get_attendance_for_date_group(date_str: str, group_name: str) -> dict:
    """Get attendance status for all students in a group on a specific date.
    Returns {student_name: status} where status is 'Present', 'Late', 'Absent', 'Excused', or ''.
    """
    try:
        ss = get_admin_panel_spreadsheet()
        tab_name = f"Attendance - {group_name}"
        ws = ss.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        return {}

    try:
        values = ws.get_all_values()
        if not values or len(values) < 2:
            return {}

        headers = values[0]
        
        # Find the column for this date
        date_col_idx = None
        for i, header in enumerate(headers):
            if date_str in str(header):
                date_col_idx = i
                break
        
        if date_col_idx is None:
            return {}
        
        # Build result: {student_name: status}
        result = {}
        for row in values[1:]:
            student_name = _norm(row[0]) if row else ""
            if not student_name:
                continue
            status = _norm(row[date_col_idx]) if date_col_idx < len(row) else ""
            result[student_name] = status
        
        return result
    except Exception as e:
        print(f"Error reading attendance: {e}")
        return {}


def get_existing_attendance_dates(group_name: str) -> list:
    """Get all existing attendance dates for a group (from column headers).
    Returns a sorted list of YYYY-MM-DD strings, most recent first.
    """
    try:
        ss = get_admin_panel_spreadsheet()
        tab_name = f"Attendance - {group_name}"
        ws = ss.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        return []

    try:
        values = ws.get_all_values()
        if not values:
            return []

        headers = values[0]
        dates = []
        
        # Extract dates from headers (skip first 2: Student Name, TG Handle)
        for i, header in enumerate(headers[2:], start=2):
            header_str = str(header).strip()
            if not header_str:
                continue
            # Try to extract YYYY-MM-DD format
            # Date objects often come as "Sat Aug 15 2026 00:00:00 GMT+0500 ..."
            # Try to find YYYY-MM-DD in the string
            import re
            match = re.search(r'(\d{4})-(\d{2})-(\d{2})', header_str)
            if match:
                date_str = match.group(0)
                dates.append(date_str)
            # Also check if it looks like a date in other formats
            elif len(header_str) >= 10 and any(c.isdigit() for c in header_str):
                # Try parsing as a date object representation
                try:
                    # This handles "Sat Aug 15 2026 00:00:00 GMT+0500" format
                    if "2026" in header_str or "2025" in header_str or "2024" in header_str:
                        import re
                        # Extract year-month-day
                        year_match = re.search(r'(202\d)', header_str)
                        month_day = re.search(r'([A-Za-z]+)\s+(\d+)', header_str)
                        if year_match and month_day:
                            month_map = {"Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
                                       "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12"}
                            month = month_map.get(month_day.group(1), "")
                            day = month_day.group(2).zfill(2)
                            if month:
                                date_str = f"{year_match.group(1)}-{month}-{day}"
                                dates.append(date_str)
                except Exception:
                    pass
        
        # Sort descending (most recent first)
        dates = sorted(set(dates), reverse=True)
        return dates
    except Exception as e:
        print(f"Error getting dates: {e}")
        return []

def get_last_session_date(group_name: str) -> str | None:
    """Most recent past attendance date for a group, or None if there isn't one.

    Just the first entry of get_existing_attendance_dates() filtered to
    strictly-before-today, since that function already returns dates sorted
    most-recent-first.
    """
    today = today_str()
    dates = [d for d in get_existing_attendance_dates(group_name) if d < today]
    return dates[0] if dates else None

def mark_attendance(date_str: str, group_name: str, student_name: str, status: str) -> bool:
    """Mark attendance directly in 'Attendance - <Group>' tab.
    
    Creates the date column if it doesn't exist, finds the student row, and
    sets the status. Returns True if successful.
    
    Args:
        date_str: YYYY-MM-DD format
        group_name: e.g. "Padawan Offline"
        student_name: exact student name from Roster
        status: one of "Present", "Late", "Absent", "Excused"
    """
    try:
        ss = get_admin_panel_spreadsheet()
        tab_name = f"Attendance - {group_name}"
        ws = ss.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        return False

    try:
        # Get all data
        values = ws.get_all_values()
        if not values:
            return False

        # Row 1 is headers: "Student Name", "TG Handle", then dates...
        headers = values[0]
        
        # Find or create the date column
        date_col_idx = None
        for i, header in enumerate(headers):
            # Match YYYY-MM-DD anywhere in the header (handles Date object strings)
            if date_str in str(header):
                date_col_idx = i
                break
        
        if date_col_idx is None:
            # Column doesn't exist — add it
            # First, find which column to add (after TG Handle)
            new_col_idx = len(headers)
            # Append the date as a new column header using gspread's date formatting
            ws.update_cell(1, new_col_idx + 1, date_str)
            date_col_idx = new_col_idx
        
        # Find the student row
        student_row_idx = None
        for i, row in enumerate(values[1:], start=2):  # Rows are 1-indexed in gspread
            if _norm(row[0]) == _norm(student_name):
                student_row_idx = i
                break
        
        if student_row_idx is None:
            return False
        
        # Mark the status in the sheet (1-indexed)
        ws.update_cell(student_row_idx, date_col_idx + 1, status)
        return True
    except Exception as e:
        print(f"Error marking attendance: {e}")
        return False
    return None