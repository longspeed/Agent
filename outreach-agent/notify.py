from config import NOTIFY_EMAIL
import gmail


def notify(subject, message):
    try:
        gmail.send_email(NOTIFY_EMAIL, subject, message)
    except Exception as e:
        print(f"Email notification failed: {e}")
