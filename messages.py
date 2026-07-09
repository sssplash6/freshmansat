"""User-facing message text.

Kept separate from logic so wording can be tweaked without touching handlers or
jobs. Reminder tone escalates with the reminder number.
"""
import config


def welcome_registered() -> str:
    return (
        "👋 Welcome! You're now registered to receive payment reminders here.\n\n"
        "Use /status any time to check your current balance and payment status."
    )


def welcome_no_username() -> str:
    return (
        "⚠️ You don't have a Telegram username set.\n\n"
        "Matching your payments relies on your username, so please set one in "
        "Telegram → Settings → Username, then send /start again."
    )


def status_found(amount: str, status: str, total: str) -> str:
    amount = amount or "—"
    status = status or "—"
    total = total or "—"
    return (
        "📋 Here's your record:\n\n"
        f"• Amount: {amount}\n"
        f"• Status: {status}\n"
        f"• Total: {total}"
    )


def status_not_found() -> str:
    return (
        "🔍 I couldn't find a payment record linked to your username.\n\n"
        "Please contact the admin so they can check your details."
    )


def payment_success(name: str) -> str:
    who = f" {name}" if name else ""
    return f"✅ Payment successful — thank you{who}! Your status is now up to date."


def proof_no_record() -> str:
    return (
        "🔍 I received your photo, but I couldn't find a payment record linked to "
        "your username.\n\n"
        "Please contact the admin so they can check your details."
    )


def proof_received() -> str:
    return (
        "✅ Thanks! Your payment proof has been received and is now pending review. "
        "We'll update your status once it's confirmed."
    )


def admin_proof_caption(name: str, group: str, amount: str) -> str:
    """Caption for the proof photo forwarded to the admin."""
    return (
        "🧾 New payment proof submitted — please review:\n"
        f"• Student: {name or '—'}\n"
        f"• Group: {group or '—'}\n"
        f"• Amount: {amount or '—'}"
    )


def _extract_amount_key(amount: str) -> str | None:
    """Pull a bare number like '89' out of strings like '$89', '89 usd', '89.00'."""
    import re
    match = re.search(r"\d+", amount or "")
    return match.group(0) if match else None


def _stripe_line(amount: str) -> str:
    key = _extract_amount_key(amount)
    link = config.STRIPE_LINKS.get(key) if key else None
    if link:
        return f"💳 Card (Stripe): {link}"
    return (
        "💳 Card (Stripe): please contact the admin for your payment link, "
        "since your amount doesn't match a preset option."
    )


def _payment_options(amount: str) -> str:
    return (
        "You can pay either way:\n"
        f"{_stripe_line(amount)}\n"
        "📱 Payme: scan the QR code below."
    )


def reminder_text(name: str, amount: str, reminder_number: int) -> str:
    """Build the reminder caption; tone escalates with ``reminder_number``."""
    greeting_name = name or "there"
    amount = amount or "your balance"

    if reminder_number <= 1:
        body = (
            f"Hi {greeting_name}! 👋 Just a friendly reminder that you have "
            f"{amount} outstanding for your course.\n\n"
            "Whenever you get a chance, here are your payment options:"
        )
    elif reminder_number <= 3:
        body = (
            f"Hi {greeting_name}, a quick follow-up: your payment of {amount} "
            "is still outstanding.\n\n"
            "Please settle it when you can — here are your options:"
        )
    else:
        body = (
            f"Hi {greeting_name}. This is a repeated reminder that your payment "
            f"of {amount} remains outstanding and now needs your attention.\n\n"
            "Please complete payment using one of the options below:"
        )

    return f"{body}\n\n{_payment_options(amount)}"
