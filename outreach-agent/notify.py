import gmail


def notify(account, subject, message):
    """Emails the account's own notify address from its own Gmail. Skips
    quietly if the account hasn't set a notify email."""
    notify_email = (account.get("notify_email") or "").strip()
    if not notify_email:
        return
    try:
        gmail.send_email(account, notify_email, subject, message)
    except Exception as e:
        print(f"Email notification failed: {e}")
