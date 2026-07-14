import base64
from email.mime.text import MIMEText

from googleapiclient.discovery import build

from google_auth import get_credentials

_service = None


def _get_service():
    global _service
    if _service is None:
        _service = build("gmail", "v1", credentials=get_credentials())
    return _service


def send_email(to, subject, body):
    """Sends a new email. Returns the Gmail thread id."""
    service = _get_service()
    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return sent["threadId"]


def _header(headers, name):
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def send_reply(thread_id, to, body):
    """Sends body as a reply within thread_id, threaded via In-Reply-To/References."""
    service = _get_service()
    thread = service.users().threads().get(userId="me", id=thread_id, format="metadata",
                                             metadataHeaders=["Subject", "Message-ID"]).execute()
    messages = thread.get("messages", [])
    first_headers = messages[0]["payload"]["headers"]
    latest_headers = messages[-1]["payload"]["headers"]

    subject = _header(first_headers, "Subject") or "Re:"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    message_id = _header(latest_headers, "Message-ID")

    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    if message_id:
        message["In-Reply-To"] = message_id
        message["References"] = message_id
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    sent = service.users().messages().send(
        userId="me", body={"raw": raw, "threadId": thread_id}
    ).execute()
    return sent["threadId"]


def _extract_body(payload):
    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain":
            return _extract_body(part)
    for part in payload.get("parts", []):
        text = _extract_body(part)
        if text:
            return text
    return ""


def get_latest_reply(thread_id):
    """Returns the body text of the newest message in the thread if there's
    more than one message (i.e. someone replied to our outreach email),
    otherwise returns None."""
    service = _get_service()
    thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    messages = thread.get("messages", [])
    if len(messages) < 2:
        return None

    latest = messages[-1]
    return _extract_body(latest["payload"]).strip()
