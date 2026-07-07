"""Standalone sanity check for the Google Sheets connection.

Run:  ./venv/bin/python test_connection.py

Verifies auth, lists group vs. special tabs, reads Bot Data / Send Log headers,
and performs a write round-trip (append + delete) so nothing is left behind.
"""
import config
import sheets


def main():
    print("=== Config ===")
    for p in config.validate():
        print(f"  [warn] {p}")
    print(f"  Sheet ID: {config.SHEET_ID}")
    print(f"  Credentials: {config.CREDENTIALS_FILE}")

    print("\n=== Connecting ===")
    ss = sheets.get_spreadsheet()
    print(f"  Opened spreadsheet: {ss.title!r}")

    print("\n=== All tabs ===")
    all_titles = [ws.title for ws in ss.worksheets()]
    for t in all_titles:
        skipped = t.strip().lower() in config.SPECIAL_TABS
        print(f"  {'SKIP ' if skipped else 'GROUP'}  {t!r}")

    group_ws = sheets.get_group_worksheets()
    group_titles = [ws.title for ws in group_ws]
    print(f"\n  -> {len(group_titles)} group tab(s): {group_titles}")

    # Confirm every special tab is excluded from the group list.
    print("\n=== Special-tab exclusion check ===")
    for special in config.SPECIAL_TABS:
        present = any(t.strip().lower() == special for t in all_titles)
        excluded = all(t.strip().lower() != special for t in group_titles)
        status = "ok" if excluded else "FAIL — still in group list!"
        note = "" if present else " (not present in sheet)"
        print(f"  {special!r}: excluded={excluded} [{status}]{note}")

    print("\n=== Reading group records ===")
    records = sheets.iter_group_records()
    print(f"  Total data rows across group tabs: {len(records)}")
    for rec in records[:5]:
        print(
            f"    [{rec['group']}] row {rec['row_number']}: "
            f"name={rec['name']!r} tg={rec['tg']!r} "
            f"amount={rec['amount']!r} status={rec['status']!r} total={rec['total']!r}"
        )
    if len(records) > 5:
        print(f"    ... ({len(records) - 5} more)")

    print("\n=== Bot Data / Send Log headers ===")
    bd = sheets.get_spreadsheet().worksheet(config.BOT_DATA_TAB)
    sl = sheets.get_spreadsheet().worksheet(config.SEND_LOG_TAB)
    print(f"  {config.BOT_DATA_TAB!r} headers: {bd.row_values(1)}")
    print(f"  {config.SEND_LOG_TAB!r} headers: {sl.row_values(1)}")

    print("\n=== Write round-trip (append + delete) ===")
    _test_write(bd, "Bot Data")
    _test_write(sl, "Send Log")

    print("\nAll checks passed.")


def _test_write(ws, label):
    before = len(ws.get_all_values())
    headers = ws.row_values(1)
    marker = "__CONNECTION_TEST__"
    ws.append_row([marker] + [""] * (len(headers) - 1), value_input_option="RAW")
    after = len(ws.get_all_values())
    assert after == before + 1, f"{label}: append did not add a row"
    # Delete the row we just added (it is the last row).
    ws.delete_rows(after)
    final = len(ws.get_all_values())
    assert final == before, f"{label}: cleanup delete failed"
    print(f"  {label}: append+delete OK (rows {before} -> {after} -> {final})")


if __name__ == "__main__":
    main()
