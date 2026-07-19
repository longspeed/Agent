import base64
import re
from email.mime.text import MIMEText
from email.utils import parseaddr

from googleapiclient.discovery import build

from google_auth import get_credentials

def _get_service(account):
    # Deliberately not cached: credentials differ per account, and
    # send_outreach.py calls this concurrently from a thread pool where the
    # underlying httplib2.Http connection isn't safe to share across threads.
    return build("gmail", "v1", credentials=get_credentials(account))


def send_email(account, to, subject, body):
    """Sends a new email from the account's connected Gmail. Returns the thread id."""
    service = _get_service(account)
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


def send_reply(account, thread_id, to, body):
    """Sends body as a reply within thread_id, threaded via In-Reply-To/References."""
    service = _get_service(account)
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


_ATTRIBUTION = re.compile(r"^On .{0,200} wrote:\s*$")
_ORIG_MESSAGE = re.compile(r"^-{2,}\s*Original Message\s*-{2,}$")
_HEADERISH = re.compile(r"^(From|Sent|To|Cc|Subject|Date):\s")


def strip_quoted(text):
    """Trims the quoted-history TAIL of an email body so only what this
    message actually added remains. Deliberately not cut-at-first-marker:
    inline repliers weave answers between '>' lines, and those answers must
    survive to the LLM. Rules:
    - "-----Original Message-----" cuts unconditionally (nobody writes below it)
    - "On ... wrote:" / forwarded-header lines cut only when everything after
      them is quoted, blank, or more headers -- content below means an inline
      reply, which is kept whole (quotes included, as context)
    - otherwise only trailing '>'/blank lines are stripped
    Falls back to the full text if trimming would leave nothing."""
    lines = text.splitlines()

    def only_quoted_below(idx):
        return all(
            not l.strip() or l.strip().startswith(">") or _HEADERISH.match(l.strip())
            for l in lines[idx + 1:]
        )

    cut = len(lines)
    for i, line in enumerate(lines):
        s = line.strip()
        if _ORIG_MESSAGE.match(s):
            cut = i
            break
        if (_ATTRIBUTION.match(s) or _HEADERISH.match(s)) and only_quoted_below(i):
            cut = i
            break
    while cut > 0:
        s = lines[cut - 1].strip()
        if not s or s.startswith(">"):
            cut -= 1
        else:
            break
    trimmed = "\n".join(lines[:cut]).strip()
    return trimmed or text.strip()


def get_latest_reply(account, thread_id, contact_email):
    reply, _ = get_latest_reply_with_history(account, thread_id, contact_email)
    return reply


def get_latest_reply_with_history(account, thread_id, contact_email):
    """Returns (reply_text, history) where reply_text is the body of the
    newest message in the thread when it was sent by the contact we emailed --
    whether that's their first reply to our outreach or a follow-up later in
    an ongoing thread -- otherwise (None, []). history covers every earlier
    message on the thread, oldest first, as (from_contact, body) pairs, so a
    reply can be drafted against the whole conversation rather than just the
    original outreach.

    A "Replied" row stays in the reply-check scope so later messages on the
    same thread keep getting picked up. To tell a genuine reply apart from our
    own answer, we match the newest message's From address against the
    contact's known email rather than relying on Gmail's SENT label: when the
    replies are produced from the same mailbox that runs outreach (e.g. a
    Send-As alias, common while testing), a real inbound reply still carries
    the SENT label, so keying on SENT silently drops it. The sender address is
    unambiguous -- our replies come from the connected account, theirs come
    from the address we reached out to."""
    service = _get_service(account)
    thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    messages = thread.get("messages", [])
    if len(messages) < 2:
        return None, []

    def _from_addr(msg):
        return parseaddr(_header(msg["payload"]["headers"], "From"))[1].strip().lower()

    latest = messages[-1]
    contact_addr = _from_addr(latest)
    if contact_email and contact_addr != contact_email.strip().lower():
        # The newest message is our own reply (or someone other than the
        # contact) -- nothing new from them to review until they write back.
        return None, []

    history = [
        (_from_addr(m) == contact_addr, _extract_body(m["payload"]).strip())
        for m in messages[:-1]
    ]
    return _extract_body(latest["payload"]).strip(), history
