import base64
import hashlib
import re
from datetime import datetime, timezone
from email import message_from_string
from email.mime.text import MIMEText
from email.utils import getaddresses, parseaddr

from googleapiclient.discovery import build

from google_auth import get_credentials

def _get_service(account, capability="monitor"):
    # Deliberately not cached: credentials differ per account, and
    # send_outreach.py calls this concurrently from a thread pool where the
    # underlying httplib2.Http connection isn't safe to share across threads.
    return build("gmail", "v1", credentials=get_credentials(account, capability))


def deterministic_message_id(account_id, operation_key):
    """Stable RFC Message-ID used only to prove an ambiguous send outcome."""
    digest = hashlib.sha256(
        f"{account_id}:{operation_key}".encode("utf-8")
    ).hexdigest()
    return f"sendkeep.{digest}@sendkeep.local"


def send_email(account, to, subject, body, unsubscribe_url="", message_id=""):
    """Sends a new email from the account's connected Gmail. Returns the thread id.

    When unsubscribe_url is given, adds the RFC 2369 / RFC 8058 headers so Gmail
    and other clients render their own native "Unsubscribe" control and honour a
    one-click POST -- both of which materially help deliverability for cold mail.
    """
    service = _get_service(account, "send")
    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    if unsubscribe_url:
        message["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return sent["threadId"]


def get_sending_address(account, capability="monitor"):
    """The Gmail address this account actually sends from. The connected
    mailbox is the sending identity, and it is not necessarily the address the
    customer logged into Sendkeep with -- the deliverability check has to
    inspect the domain that mail will really leave from.

    capability defaults to "monitor" for read paths; the send gate passes
    "send" because checking the domain mail will leave from must not demand a
    permission wider than sending itself (a send-only grant has no
    gmail.readonly, and demanding monitor here 409-blocked every first-touch
    send from such accounts)."""
    service = _get_service(account, capability)
    profile = service.users().getProfile(userId="me").execute()
    return (profile.get("emailAddress") or "").strip()


def _header(headers, name):
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def send_reply(account, thread_id, to, body, unsubscribe_url=""):
    """Sends body as a reply within thread_id, threaded via In-Reply-To/References."""
    service = _get_service(account, "send")
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
        message["Message-ID"] = f"<{message_id.strip('<>')}>"
    if unsubscribe_url:
        message["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    if message_id:
        message["In-Reply-To"] = message_id
        message["References"] = message_id
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    sent = service.users().messages().send(
        userId="me", body={"raw": raw, "threadId": thread_id}
    ).execute()
    return sent["threadId"]


def find_sent_email(account, to, subject, body, message_id):
    """Return exact Gmail proof for an ambiguous first-touch send."""
    service = _get_service(account)
    target_id = (message_id or "").strip("<>")
    if not target_id:
        return None
    listed = service.users().messages().list(
        userId="me", q=f"in:sent rfc822msgid:{target_id}", maxResults=5
    ).execute()
    target = (to or "").strip().lower()
    expected_subject = (subject or "").strip().lower()
    expected_body = " ".join((body or "").split())
    for item in listed.get("messages", []):
        message = service.users().messages().get(
            userId="me", id=item["id"], format="full"
        ).execute()
        payload = message.get("payload", {})
        headers = payload.get("headers", [])
        recipients = {
            address.strip().lower()
            for _, address in getaddresses([_header(headers, "To"), _header(headers, "Cc")])
            if address.strip()
        }
        if target not in recipients:
            continue
        if (_header(headers, "Subject") or "").strip().lower() != expected_subject:
            continue
        if " ".join(_extract_body(payload).split()) != expected_body:
            continue
        return message
    return None


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
    """Only caller: server.py's check_single_reply, a per-row, on-demand
    "has this replied yet" check triggered by a single user action -- never
    a loop over a campaign's rows. get_own_addresses costs one or two extra
    Gmail API calls (profile, Send-As list) on every call here; deliberately
    not cached, since this is a one-shot interactive action, not the
    per-account, per-cycle polling loop in watch_replies.py where that cost
    would actually add up. If a caller ever iterates rows through this
    function instead, the same own_addresses lookup should move out to be
    fetched once, the way check_for_replies already does it."""
    own_addresses, _ = get_own_addresses(account)
    result = get_latest_reply_with_history(account, thread_id, contact_email, own_addresses)
    return result["text"] if result else None


def get_thread(account, thread_id):
    """One full thread read. Split out so a caller that needs both a reply check
    and a bounce check on the same thread pays Gmail's quota once."""
    service = _get_service(account)
    return service.users().threads().get(userId="me", id=thread_id, format="full").execute()


def _reply_subject(thread):
    """Return the subject Gmail uses for a reply in ``thread``."""
    messages = thread.get("messages", [])
    if not messages:
        return "Re:"
    headers = messages[0].get("payload", {}).get("headers", [])
    subject = _header(headers, "Subject") or "Re:"
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def get_reply_subject(thread):
    """Public, presentation-safe wrapper for the subject of a Gmail reply."""
    return _reply_subject(thread)


def find_sent_reply(account, thread_id, to, body, thread=None):
    """Find an exact reply that Gmail accepted in ``thread_id``.

    Gmail and the database cannot commit atomically. If ``messages.send`` times
    out after Gmail accepts the request, retrying would risk a duplicate. This
    read is the safe recovery primitive: only a message labelled ``SENT`` in
    the same thread, addressed to the intended recipient, with the exact
    normalized body and reply subject counts as proof. A matching message may
    have been sent in the current attempt or immediately before an operator
    re-opened the page; in both cases recording it as sent is safer than
    dispatching a second identical follow-up.

    ``thread`` can be supplied by a caller that already fetched the thread so
    the worker and the send endpoint do not pay for a second full read.
    """
    thread = thread if thread is not None else get_thread(account, thread_id)
    target = (to or "").strip().lower()
    expected_body = " ".join((body or "").split())
    expected_subject = _reply_subject(thread).strip().lower()
    if not target or not expected_body:
        return None

    for message in reversed(thread.get("messages", [])):
        if "SENT" not in (message.get("labelIds") or []):
            continue
        payload = message.get("payload", {})
        headers = payload.get("headers", [])
        recipients = {
            address.strip().lower()
            for _, address in getaddresses([
                _header(headers, "To"),
                _header(headers, "Cc"),
            ])
            if address.strip()
        }
        if target not in recipients:
            continue
        subject = (_header(headers, "Subject") or "").strip().lower()
        if subject != expected_subject:
            continue
        if " ".join(_extract_body(payload).split()) != expected_body:
            continue
        return message
    return None


def list_recent_sent_threads(account, own_addresses=None, *, days=30, max_threads=50):
    """Discover recent Gmail conversations that were sent outside Sendkeep.

    Sheets are an optional mirror, not the definition of what the reply desk
    watches. Gmail returns message ids for ``in:sent``; we read bounded
    metadata for the first message seen in each thread, extract the first
    external recipient, and let the durable tracker deduplicate the result.
    Bodies are never fetched here. A failed discovery should be visible to the
    worker as an account error rather than silently making the desk look empty.
    """
    if not days or not max_threads:
        return []
    service = _get_service(account)
    own = {value.strip().lower() for value in (own_addresses or set()) if value}
    listed = service.users().messages().list(
        userId="me",
        q=f"in:sent newer_than:{int(days)}d",
        maxResults=int(max_threads),
    ).execute()

    discovered = []
    seen_threads = set()
    for message in listed.get("messages", []):
        thread_id = (message.get("threadId") or "").strip()
        message_id = (message.get("id") or "").strip()
        if not thread_id or not message_id or thread_id in seen_threads:
            continue
        seen_threads.add(thread_id)
        metadata = service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["To", "Cc", "Subject"],
        ).execute()
        headers = metadata.get("payload", {}).get("headers", [])
        recipients = getaddresses([
            _header(headers, "To"),
            _header(headers, "Cc"),
        ])
        external = next(
            ((name.strip(), address.strip().lower()) for name, address in recipients
             if address.strip() and address.strip().lower() not in own),
            None,
        )
        if not external:
            continue
        name, email = external
        discovered.append({
            "thread_id": thread_id,
            "message_id": message_id,
            "email": email,
            "name": name,
            "subject": _header(headers, "Subject"),
            "source": "gmail_discovery",
        })
    return discovered


_warned_degraded_own_addresses = False


def get_own_addresses(account):
    """Returns (addresses, degraded) -- every address this account may
    legitimately send from: the primary Gmail address plus every verified
    Send-As alias. Needed to tell "our own message" apart from "off-sheet
    stranger" when scanning a whole thread instead of just its newest message.

    Not just the primary address: replies sent via a Send-As alias are common
    while testing (see get_latest_reply_with_history), and matching against a
    single address would flag the operator's own alias replies as an
    unconfirmed stranger, asking them to confirm whether their own email is
    really them.

    degraded is True when the Send-As listing call failed and this fell back
    to just the primary address -- callers must not treat that silently the
    same as a healthy lookup, since it under-recognizes "ours" and can
    reintroduce the exact alias-misclassification this function exists to
    prevent."""
    global _warned_degraded_own_addresses
    service = _get_service(account)
    profile = service.users().getProfile(userId="me").execute()
    primary = (profile.get("emailAddress") or "").strip().lower()
    addresses = {primary} if primary else set()
    try:
        send_as = service.users().settings().sendAs().list(userId="me").execute()
    except Exception as e:
        if not _warned_degraded_own_addresses:
            _warned_degraded_own_addresses = True
            print(
                f"WARNING: could not list Send-As aliases ({e}); reply detection "
                "will treat any alias other than the primary address as a "
                "stranger until this succeeds again."
            )
        return addresses, True
    for entry in send_as.get("sendAs", []):
        email = (entry.get("sendAsEmail") or "").strip().lower()
        if email and entry.get("verificationStatus", "accepted") == "accepted":
            addresses.add(email)
    return addresses, False


_BOUNCE_CONTENT_TYPE_HINT = "report-type=delivery-status"


def _classify_message(headers, own_addresses, target_email):
    """(kind) for one message, in precedence order: "ours" (From is one of
    our own addresses), "daemon" (bounce/delivery-status sender), "auto_responder"
    (RFC 3834 Auto-Submitted), "match" (From is target_email), or "other".

    Shared by get_latest_reply_with_history and get_history_before so both
    agree on what counts as "from the contact" by construction rather than by
    each re-deriving a similar-looking rule -- an auto-responder whose From
    equals target_email is exactly the case two independently-written
    versions of this check could silently disagree on."""
    from_addr = parseaddr(_header(headers, "From"))[1].strip().lower()
    if own_addresses and from_addr in own_addresses:
        return "ours"
    content_type = _header(headers, "Content-Type").lower().replace(" ", "")
    if _BOUNCE_CONTENT_TYPE_HINT in content_type or any(
        hint in from_addr for hint in _BOUNCE_SENDER_HINTS
    ):
        return "daemon"
    auto_submitted = _header(headers, "Auto-Submitted").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return "auto_responder"
    if target_email and from_addr == target_email.strip().lower():
        return "match"
    return "other"


def get_reply_candidates_with_history(account, thread_id, contact_email, own_addresses, thread=None):
    """Return every inbound candidate in chronological order.

    Processing only the newest message loses an earlier reply when a contact
    sends twice between worker cycles. Each result keeps its own message ID,
    history, timestamp, and direct-answer marker for safe queueing.
    """
    thread = thread if thread is not None else get_thread(account, thread_id)
    messages = thread.get("messages", [])
    if len(messages) < 2:
        return []

    kinds = [
        _classify_message(m["payload"]["headers"], own_addresses, contact_email)
        for m in messages
    ]

    candidates = []
    for candidate_idx, kind in enumerate(kinds):
        if kind not in ("match", "other"):
            continue
        candidate = messages[candidate_idx]
        from_addr = parseaddr(
            _header(candidate["payload"]["headers"], "From")
        )[1].strip().lower()
        history = [
            (
                parseaddr(_header(messages[i]["payload"]["headers"], "From"))[1].strip().lower() == from_addr,
                _extract_body(messages[i]["payload"]).strip(),
            )
            for i in range(candidate_idx)
        ]
        timestamp = None
        if candidate.get("internalDate"):
            try:
                timestamp = datetime.fromtimestamp(
                    int(candidate["internalDate"]) / 1000, tz=timezone.utc
                ).isoformat()
            except (TypeError, ValueError, OSError):
                pass
        candidates.append({
            "text": _extract_body(candidate["payload"]).strip(),
            "history": history,
            "message_id": candidate.get("id"),
            "kind": "on_sheet" if kind == "match" else "off_sheet",
            "answered_elsewhere": any(k == "ours" for k in kinds[candidate_idx + 1:]),
            "from_addr": from_addr,
            "timestamp": timestamp,
        })
    return candidates


def get_latest_reply_with_history(account, thread_id, contact_email, own_addresses, thread=None):
    """Return the newest candidate, retaining the legacy single-result API."""
    candidates = get_reply_candidates_with_history(
        account, thread_id, contact_email, own_addresses, thread=thread
    )
    return candidates[-1] if candidates else None


def get_operator_messages(thread, own_addresses):
    """Return operator-authored messages for promise extraction."""
    result = []
    for message in thread.get("messages", []):
        headers = message.get("payload", {}).get("headers", [])
        if _classify_message(headers, own_addresses, "") != "ours":
            continue
        timestamp = None
        if message.get("internalDate"):
            try:
                timestamp = datetime.fromtimestamp(
                    int(message["internalDate"]) / 1000, tz=timezone.utc
                ).isoformat()
            except (TypeError, ValueError, OSError):
                pass
        result.append({
            "message_id": message.get("id"),
            "text": _extract_body(message.get("payload", {})).strip(),
            "timestamp": timestamp,
            "kind": "operator",
        })
    return result


def get_history_before(account, thread_id, message_id, contact_email, own_addresses, thread=None):
    """(from_contact, body) pairs for every message strictly before message_id
    on the thread, oldest first -- the same shape get_latest_reply_with_history
    builds inline, but rebuildable on its own for a message that was queued as
    a review without its history ever being stored (an off-sheet "flagged"
    review defers drafting to confirm time, see watch_replies.py).

    contact_email must be the OBSERVED sender of the message at message_id --
    the review row's `email` column, which for a flagged review is the
    off-sheet address, not the sheet's original contact -- because
    get_latest_reply_with_history now labels its own inline history against
    that candidate's own From address, not the sheet's contact_email. Passing
    the sheet's contact_email here for a flagged review would silently
    rebuild a different history than the one detection would have built:
    an off-sheet sender's own earlier messages would be mislabelled as "not
    from them."

    Uses the same _classify_message as get_latest_reply_with_history, with
    that target, so "from_contact" means exactly the same thing in both
    places -- computing it independently here would risk the two disagreeing
    on an edge case like an auto-responder whose From equals the target."""
    thread = thread if thread is not None else get_thread(account, thread_id)
    messages = thread.get("messages", [])
    history = []
    for m in messages:
        if m.get("id") == message_id:
            break
        kind = _classify_message(m["payload"]["headers"], own_addresses, contact_email)
        history.append((kind == "match", _extract_body(m["payload"]).strip()))
    return history


_BOUNCE_SENDER_HINTS = ("mailer-daemon", "postmaster")

# The verbatim copy of our own message that a bounce quotes back. Its Received:
# chain is a list of IP addresses, and an address like 5.10.20.30 *contains*
# "5.10.20" -- which reads as a perfectly well-formed RFC 3463 permanent-failure
# code. Never pattern-match a status code inside these parts: doing so
# suppresses a live prospect forever because a packet took the scenic route.
# They are still searched for the failed *address*, which is safe -- that is a
# literal match against a string we already know.
_QUOTED_ORIGINAL_TYPES = ("message/rfc822", "text/rfc822-headers")

# Where a status code is allowed to be read from, in descending order of trust.
# The rule behind all three: proximity to something only a mail server writes.
# A field name at the start of a line constrains *which* line is read but says
# nothing about where in it the code may sit -- and "somewhere on this line" is
# how "host mx.x.com[5.10.20.30] said: 452 4.2.2" hands back 5.10.20 instead of
# 4.2.2. Position within the value is the load-bearing part.

# An enhanced code immediately following the 3-digit SMTP reply it qualifies:
# "550 5.1.1", "550-5.1.1", "452 4.2.2". An IP cannot satisfy this, because
# [45]\d{2} needs three consecutive digits and a dotted quad has a separator
# after the first.
_SMTP_ANCHORED_CODE = re.compile(r"\b[45]\d{2}[\s-]+([45]\.\d{1,3}\.\d{1,3})\b")

# A code at the very start of a field value, past an optional RFC 3464 type
# label ("smtp;", "x-unix;"). This is what Status: looks like -- the field *is*
# the code, though some MTAs append commentary after it, which is why this is a
# leading match rather than a whole-value one.
_LEADING_CODE = re.compile(r"^\s*(?:[a-z][a-z0-9-]*;\s*)?([45]\.\d{1,3}\.\d{1,3})\b", re.IGNORECASE)

# Fallback for daemons that send no usable message/delivery-status part. Splits
# out the field value so the two patterns above can be applied to it by
# position, instead of scanning the line for the first thing that looks like a
# code.
_STATUS_FIELD_LINE = re.compile(
    r"^[ \t]*(?:Status|Diagnostic-Code)[ \t]*:(.*)$", re.IGNORECASE | re.MULTILINE
)


def _code_in_field_value(value):
    """A status code from a Status:/Diagnostic-Code: value, or None.

    Tries the SMTP-reply anchor first, because a Diagnostic-Code quoting a
    server's response often names the relaying host and its IP address before
    getting to the code -- and the code that matters is the one attached to the
    reply, not the first dotted triple on the line."""
    match = _SMTP_ANCHORED_CODE.search(value) or _LEADING_CODE.match(value)
    return match.group(1) if match else None


def _decode_part(payload):
    data = payload.get("body", {}).get("data")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def _all_text(payload):
    """Every decodable text part of a message, concatenated.

    For ADDRESS MATCHING ONLY. The address that actually failed is often only
    present inside the attached copy of the original message, so finding out
    *who* bounced needs the whole tree. Deciding *what* the status was must not
    use this -- see _QUOTED_ORIGINAL_TYPES for what happens when it does."""
    chunks = [_decode_part(payload)]
    for part in payload.get("parts", []):
        chunks.append(_all_text(part))
    return "\n".join(c for c in chunks if c)


def _parts_of_type(payload, mime_type):
    """Every part with this MIME type, depth-first."""
    found = []
    if (payload.get("mimeType") or "").lower() == mime_type:
        found.append(payload)
    for part in payload.get("parts", []):
        found.extend(_parts_of_type(part, mime_type))
    return found


def _report_text(payload):
    """The bounce's own account of what happened, and nothing quoted.

    Stops at message/rfc822 and text/rfc822-headers, so the message we sent --
    and every IP address in its headers -- is excluded from anything that gets
    pattern-matched for a status code. The delivery-status part is included: it
    is machine-written, holds no quoted original, and keeps the anchored fallback
    working on a DSN too malformed for _dsn_reports to split into blocks."""
    mime = (payload.get("mimeType") or "").lower()
    if mime in _QUOTED_ORIGINAL_TYPES:
        return ""
    chunks = []
    if mime.startswith("text/") or mime == "message/delivery-status":
        chunks.append(_decode_part(payload))
    for part in payload.get("parts", []):
        chunks.append(_report_text(part))
    return "\n".join(c for c in chunks if c)


def _dsn_reports(payload):
    """Per-recipient report blocks from every message/delivery-status part.

    RFC 3464 shapes that part as RFC 822-style field groups separated by blank
    lines: one per-message group (Reporting-MTA and friends) followed by one
    group per recipient. Groups are selected by whether they carry recipient
    fields rather than by position, because not every MTA emits the per-message
    group. Parsed with the email package so folded continuation lines -- which
    Diagnostic-Code routinely uses -- come back intact."""
    reports = []
    for part in _parts_of_type(payload, "message/delivery-status"):
        for block in re.split(r"\n\s*\n", _decode_part(part).strip()):
            if not block.strip():
                continue
            fields = message_from_string(block)
            if fields.get("Status") or fields.get("Final-Recipient"):
                reports.append({k.lower(): (v or "").strip() for k, v in fields.items()})
    return reports


def _report_address(report):
    """The address a per-recipient block is about. The field is typed --
    "rfc822; user@host" -- so the type label comes off first."""
    raw = report.get("final-recipient") or report.get("original-recipient") or ""
    _, _, addressed = raw.rpartition(";")
    return parseaddr(addressed or raw)[1].strip().lower()


def _mentions_address(text, address):
    """Whether text names exactly this address.

    A substring test cannot do this job, and this decides whose address gets
    suppressed. "john@x.com" is inside "bjohn@x.com" and inside
    "john@x.com.au" -- both real, different people -- so a bounce about a
    similarly-named recipient reads as a bounce about ours. The lookarounds
    exclude every character that is legal on either side of an address, so a
    match has to be the whole thing: "<john@x.com>" and "john@x.com, other@z.com"
    still match, "bjohn@x.com" does not."""
    if not address:
        return False
    pattern = r"(?<![\w.+-])" + re.escape(address) + r"(?![\w.+-])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _classify(payload, contact):
    """(code, detail) for a bounce report about `contact`, or (None, None).

    Reads the RFC 3464 per-recipient Status field when there is one, which is
    the only way to get this right without guessing. Falls back to the daemon's
    human-readable text for MTAs that send no usable delivery-status part, where
    a code is only trusted by position: see _SMTP_ANCHORED_CODE and
    _LEADING_CODE, and _code_in_field_value for the order they are tried in.

    Returns no code for Action: delayed. That notice means the MTA is still
    retrying, not that delivery failed; the mail commonly lands on a later
    attempt, so the row must stay exactly as it is and stay in reply-check
    scope. Treating a delay as a bounce is how you stop reading the mail of a
    prospect who is about to answer."""
    reports = _dsn_reports(payload)
    if reports:
        ours = [r for r in reports if not contact or _report_address(r) == contact]
        # A block naming no recipient at all is still ours to read: a terse MTA
        # may omit Final-Recipient, and find_bounce's sender and address checks
        # have already established this report concerns us.
        if not ours:
            ours = [r for r in reports if not _report_address(r)]
        if not ours:
            # The report enumerates its recipients and our contact is not among
            # them. That is evidence the bounce is about somebody else, so stop
            # here: reading the human-readable text, which is shared across every
            # recipient, would pin another address's failure on our prospect.
            return None, None

        for report in ours:
            if report.get("action", "").strip().lower().startswith("delayed"):
                return None, None
            # Status: is the code, but some MTAs append commentary to it
            # ("5.1.1 (bad destination mailbox address)"), so read it by position.
            # Diagnostic-Code is the second choice: it is free-form quoted server
            # text, which is where an IP address can turn up.
            code = _LEADING_CODE.match(report.get("status", ""))
            code = code.group(1) if code else _code_in_field_value(report.get("diagnostic-code", ""))
            if code:
                detail = report.get("diagnostic-code") or report.get("action") or ""
                return code, " ".join(detail.split())[:200]

    text = _report_text(payload)
    detail = " ".join(text.split())[:200]

    # A code on a line that names our contact beats one found anywhere else.
    # Postfix writes one line per recipient -- "<john@x.com>: host mx.x.com
    # [5.10.20.30] said: 452 4.2.2 Mailbox full" -- so on a bounce covering
    # several addresses, the line is what ties a code to a person.
    if contact:
        for line in text.splitlines():
            if _mentions_address(line, contact):
                match = _SMTP_ANCHORED_CODE.search(line)
                if match:
                    return match.group(1), detail

    for value in _STATUS_FIELD_LINE.findall(text):
        code = _code_in_field_value(value)
        if code:
            return code, detail

    match = _SMTP_ANCHORED_CODE.search(text)
    if match:
        return match.group(1), detail
    return None, None


def find_bounce(account, thread_id, contact_email, thread=None):
    """Looks for a delivery failure on a thread we sent outreach to.

    Returns None, or {"permanent": bool, "code": "5.1.1", "detail": str}.
    permanent is true only for a 5.x.x, which is the only class that suppresses.

    Bounces are invisible to get_latest_reply_with_history(): it matches the
    newest message's From against the contact, and a bounce comes from the
    receiving server's daemon rather than the person. Without this the row sits
    "Sent" forever, the customer never learns the address was bad, and the same
    guessed address stays eligible for the next campaign.

    thread accepts an already-fetched thread so a caller that has just read it
    (watch_replies checks for a reply first) does not pay for a second full
    fetch of the same messages."""
    thread = thread if thread is not None else get_thread(account, thread_id)
    contact = (contact_email or "").strip().lower()

    for msg in reversed(thread.get("messages", [])):
        headers = msg["payload"]["headers"]
        sender = parseaddr(_header(headers, "From"))[1].strip().lower()
        content_type = _header(headers, "Content-Type").lower().replace(" ", "")
        looks_like_report = "report-type=delivery-status" in content_type
        if not looks_like_report and not any(hint in sender for hint in _BOUNCE_SENDER_HINTS):
            continue

        # Only treat this as *our contact's* bounce if the failed address is
        # actually theirs. Prefer the explicit header; fall back to looking for
        # the address anywhere in the report, including the attached original
        # message. Guessing here would suppress the wrong person, which is why
        # the match is a whole-address one rather than a substring -- see
        # _mentions_address.
        failed = _header(headers, "X-Failed-Recipients").strip()
        if contact:
            named = failed if failed else _all_text(msg["payload"])
            if not _mentions_address(named, contact):
                continue

        code, detail = _classify(msg["payload"], contact)
        if not code:
            # Either a daemon message we can't classify, or a delay notice.
            # Skipping is the safe default both ways: a missed bounce costs one
            # stale row, a misread one suppresses a real prospect permanently.
            continue
        return {"permanent": code.startswith("5"), "code": code, "detail": detail}
    return None
