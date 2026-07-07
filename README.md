# Payment Reminder Bot

A Telegram bot that reads student payment data from a Google Sheet and:

- registers users (`/start`) and reports their balance (`/status`);
- sends **escalating payment reminders** every few days to students who still owe;
- sends a **thank-you** the day a student's status flips to Paid/Scholarship.

## Project layout

| File | Purpose |
|------|---------|
| `config.py` | Sheet ID, tab names, column names, file paths, secrets loader, `validate()` |
| `sheets.py` | All Google Sheets access (auth, reads, writes, header detection) |
| `messages.py` | User-facing message text (reminder tone escalates by count) |
| `jobs.py` | The two scheduled jobs: `run_reminders`, `run_payment_check` |
| `bot.py` | Entrypoint: command + photo handlers + APScheduler wiring |
| `test_connection.py` | Standalone Sheets connectivity/read/write check |
| `credentials.json` | Google service-account key (gitignored) |
| `.env` | `TELEGRAM_BOT_TOKEN`, `STRIPE_LINK` (gitignored) |
| `payme_qr.png` | Payme QR image attached to every reminder |

## Setup

```bash
# 1. Create the virtualenv and install dependencies
python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt

# 2. Configure secrets
cp .env.example .env
#   then edit .env:
#     TELEGRAM_BOT_TOKEN=<token from @BotFather>
#     STRIPE_LINK=<your real Stripe payment link>
#     ADMIN_CHAT_ID=<your personal Telegram chat_id>

# 3. Make sure the service account (see credentials.json -> client_email)
#    has been shared as an Editor on the Google Sheet.

# 4. Verify the Sheets connection
./venv/bin/python test_connection.py

# 5. Run the bot (starts polling + both scheduled jobs)
./venv/bin/python bot.py
```

### Getting your ADMIN_CHAT_ID

Payment-proof photos are forwarded to you on Telegram. To find your chat_id,
message `@userinfobot` (or `@RawDataBot`) on Telegram — it replies with your
numeric id. Put that in `ADMIN_CHAT_ID`. You must also have sent the bot at
least one message (e.g. `/start`) so it's allowed to message you.

## How it works

**Groups.** Every tab in the sheet is a student group *except* `Bot Apps`,
`Bot Data`, and `Send Log`. Group tabs carry a title banner in row 1 and the
real column headers in row 2; the reader detects the header row automatically.
Headers are matched case-insensitively and whitespace-trimmed.

**`/start`** — registers `@username` + chat_id into `Bot Data` (idempotent). If
the user has no Telegram username set, it asks them to add one, since all
matching is by username.

**`/status`** — searches every group tab for the user's TG Contact and replies
with Amount, Status, and Total.

**Photo / payment proof** — when any user sends a photo, the bot matches them
against the group tabs by username. If there's no match it replies that no
record was found. Otherwise it forwards the largest photo size to `ADMIN_CHAT_ID`
with a caption showing the student's name, group, and amount; records the photo's
Telegram `file_id` + today's date in the user's **Bot Data** row
(`Payment Proof Link`, `Proof Submitted Date` — the group tabs are confidential
and never written to); and confirms receipt to the student.

The image lives in your admin Telegram chat, where you review it. No Google
Drive is involved (service accounts have no Drive storage quota, which made a
plain Drive folder unworkable). Storing the `file_id` lets the bot re-send the
photo later if needed.

**Reminder job** (every `REMINDER_INTERVAL_DAYS`, default 3) — for each group
row that is not Paid/Scholarship and has a TG Contact: looks up chat_id in
`Bot Data`; if missing, logs `no chat_id found` to `Send Log`; otherwise sends a
photo (Payme QR) captioned with the reminder text (which includes the Stripe
link), then logs `sent` with the escalating reminder number.

- Reminder 1 → friendly
- Reminders 2–3 → more direct
- Reminders 4+ → firm

**Payment-confirmation job** (daily at `PAYMENT_CHECK_HOUR:MINUTE`, default
09:00) — for each group row with a TG Contact, compares the sheet Status to
`Last Known Status` in `Bot Data`. If it just became Paid/Scholarship, sends a
thank-you. Updates `Last Known Status` to the current value regardless.

Job cadence and the daily run time are configurable at the bottom of `config.py`.

## Notes for this machine

The Homebrew Python 3.14 on this Mac shipped with a broken `pyexpat` (linked
against the system `libexpat`, which lacks a newer symbol). This was fixed by
installing Homebrew's `expat` and repointing the extension module at it:

```bash
brew install expat
install_name_tool -change /usr/lib/libexpat.1.dylib \
  /opt/homebrew/opt/expat/lib/libexpat.1.dylib \
  /opt/homebrew/Cellar/python@3.14/3.14.6/Frameworks/Python.framework/Versions/3.14/lib/python3.14/lib-dynload/pyexpat.cpython-314-darwin.so
codesign -f -s - <that same .so path>
```

If you recreate the environment with a fresh Python that has a working
`pyexpat`, this step is unnecessary.
