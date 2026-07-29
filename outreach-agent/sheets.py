from googleapiclient.discovery import build

import plans
from config import DAILY_SEND_LIMIT, SHEET_RANGE
from google_auth import get_credentials

# Column order: Name, Email, Company, Status, ThreadID, SentAt, EmailBody, LeadReason, EmailConfidence
(
    COL_NAME, COL_EMAIL, COL_COMPANY, COL_STATUS, COL_THREAD_ID, COL_SENT_AT, COL_EMAIL_BODY,
    COL_LEAD_REASON, COL_EMAIL_CONFIDENCE,
) = range(9)


def _get_service(account):
    # Not cached: credentials differ per account, and the underlying
    # httplib2.Http connection isn't safe to share across threads.
    return build("sheets", "v4", credentials=get_credentials(account))


def _sheet_id(account):
    sheet_id = (account.get("google_sheet_id") or "").strip()
    if not sheet_id:
        raise RuntimeError("This account hasn't connected a Google Sheet yet")
    return sheet_id


EXPECTED_HEADER = [
    "Name", "Email", "Company", "Status", "ThreadID", "SentAt", "EmailBody",
    "LeadReason", "EmailConfidence",
]


def _validate_header(values):
    """Fails loudly when the sheet's header row doesn't match the expected
    columns. Every read and write in this module addresses columns by
    position, so a reordered or renamed header would otherwise cause silently
    wrong behavior (emailing the wrong field, statuses in the wrong column) --
    a worse failure mode than a clear error naming the fix.

    Only the cells actually present in row 1 are checked, as a prefix of the
    expected header (at least Name, Email): real sheets in the wild -- the
    founder's own included -- often only carry the first few header cells,
    and columns still line up positionally. What must never pass is a
    reordered or foreign layout ("Email, Name, ..." / "First Name, ...")."""
    if not values:
        raise RuntimeError(
            "The connected sheet is empty. Row 1 must be this header: "
            + ", ".join(EXPECTED_HEADER)
        )
    found = [cell.strip().lower() for cell in values[0][:9]]
    while found and not found[-1]:
        found.pop()
    expected = [name.lower() for name in EXPECTED_HEADER]
    if len(found) < 2 or found != expected[:len(found)]:
        raise RuntimeError(
            "The sheet's header row doesn't match what Agent Hub expects. "
            f"Row 1 must start with: {', '.join(EXPECTED_HEADER[:2])} (full header: "
            f"{', '.join(EXPECTED_HEADER)}) — found: "
            f"{', '.join(cell for cell in values[0][:9] if cell.strip()) or '(blank)'}"
        )


def get_all_rows(account):
    """Returns list of (row_index, row_values). row_index is 1-based sheet row number."""
    service = _get_service(account)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=_sheet_id(account), range=SHEET_RANGE)
        .execute()
    )
    values = result.get("values", [])
    _validate_header(values)
    rows = []
    for i, row in enumerate(values[1:], start=2):  # skip header row, sheet rows are 1-based
        padded = row + [""] * (9 - len(row))
        rows.append((i, padded))
    return rows


def get_pending_rows(account):
    """Rows approved for sending but not yet sent."""
    return [(idx, row) for idx, row in get_all_rows(account) if not row[COL_STATUS].strip()]


def parse_sent_at(value):
    """The SentAt cell as an aware datetime, or None when it is absent or not a
    timestamp this app wrote.

    Imported/manual timestamps are not trusted as evidence that this app sent a
    message; they never make an address eligible either. Callers treat None as
    "no usable date" rather than as a date that passes any test."""
    from datetime import datetime, timezone

    value = (value or "").strip()
    if not value:
        return None
    try:
        sent_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return sent_at.replace(tzinfo=timezone.utc) if sent_at.tzinfo is None else sent_at


def sent_in_last_24_hours(rows):
    """Count SentAt timestamps in the preceding 24 hours, as the SHEET reports
    them. Display only -- see campaign_readiness.

    Not usable as the daily cap, which is why it no longer is. Every one of
    deleting the Sent rows, clearing this column, and changing its date format
    drives this to zero, and the cap it used to feed is the control protecting the
    sending domain."""
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    return sum(
        1 for _, row in rows
        if (sent_at := parse_sent_at(row[COL_SENT_AT])) is not None and sent_at >= cutoff
    )


def campaign_readiness(account, sent_today, suppressed_emails=None, rows=None):
    """Return the current sendable rows and the conservative daily allowance.

    Sendable means: approved (blank Status), has an email address, and not on
    the opt-out list. There's no verification gate -- the sheet owner is trusted
    to only approve leads they're comfortable emailing.

    sent_today is how many emails this account has genuinely sent in the trailing
    24 hours, supplied by the caller from the send log
    (drafts_db.count_sent_last_24_hours). It is a REQUIRED argument and
    deliberately has no default: the cap is a domain-reputation control, and a
    default that quietly fell back to the sheet-derived count is exactly the bug
    this parameter exists to remove. Making every caller state where the number
    came from is the point.

    suppressed_emails is an optional set of lower-cased opted-out addresses; the
    caller supplies it (rather than this module querying Supabase) so the sheet
    layer stays free of a database dependency -- same reason sent_today is passed
    in rather than looked up here.

    rows is accepted so a caller that needs the raw sheet for something else
    (the send path also wants already_emailed_rows) does not read it twice.
    """
    suppressed = suppressed_emails or set()
    rows = rows if rows is not None else get_all_rows(account)
    eligible = [
        (idx, row) for idx, row in rows
        if not row[COL_STATUS].strip()
        and row[COL_EMAIL].strip()
        and row[COL_EMAIL].strip().lower() not in suppressed
    ]
    # Per-plan, read off the account row already in hand. plans.daily_send_limit_for
    # makes no database call, so this module keeps the no-database property the
    # sent_today argument exists to preserve.
    daily_limit = plans.daily_send_limit_for(account)
    remaining = max(0, daily_limit - sent_today)
    return {
        "eligible": eligible[:remaining],
        "eligible_total": len(eligible),
        "sent_today": sent_today,
        # What the operator's own sheet reports, for a discrepancy message. Never
        # an input to the allowance -- they can edit it.
        "sheet_sent_today": sent_in_last_24_hours(rows),
        "daily_limit": daily_limit,
        "remaining_today": remaining,
        "capped": max(0, len(eligible) - remaining),
    }


# Every status meaning "this row has already been emailed". One definition,
# because three things depend on agreeing about it: the bounce-rate denominator,
# the reply-check scope, and the send path's guard against emailing a row twice.
SENT_STATUSES = ("Sent", "Replied", "Bounced", "Delayed")


def already_emailed_rows(rows):
    """Row indexes whose status says an email has already gone out to them.

    Used by the send path as a second, independent guard against a duplicate:
    the draft queue is the primary record of what has been sent, and this is what
    catches a draft whose queue update was the thing that failed."""
    return {
        idx for idx, row in rows
        if row[COL_STATUS].strip() in SENT_STATUSES
    }


def get_reply_check_rows(account):
    """Rows that could have a new reply worth checking: "Sent" (no reply yet),
    "Replied" (a reply already came in, but a conversation can have more than one
    message -- a "Replied" row must stay in scope or a second message on the same
    thread is never looked at again), and "Delayed".

    "Delayed" is in scope on purpose. It marks a transient 4.x.x failure, which
    is a full mailbox or a greylist, and that mail commonly lands on a retry. A
    prospect who then writes back must not be talking to nobody -- dropping
    these was the quiet half of a bug that took real trouble to avoid blocking
    them and then stopped reading their mail."""
    return [
        (idx, row) for idx, row in get_all_rows(account)
        if row[COL_STATUS].strip() in ("Sent", "Replied", "Delayed")
    ]


def _row_update_cells(row_index, status=None, thread_id=None, sent_at=None, email_body=None, email_confidence=None):
    cells = []
    if status is not None:
        cells.append((f"D{row_index}", status))
    if thread_id is not None:
        cells.append((f"E{row_index}", thread_id))
    if sent_at is not None:
        cells.append((f"F{row_index}", sent_at))
    if email_body is not None:
        cells.append((f"G{row_index}", email_body))
    if email_confidence is not None:
        cells.append((f"I{row_index}", email_confidence))
    return cells


def require_full_header(account):
    """Gate for every WRITE path (send batch, reply marking, lead
    approve/discard): row 1 must carry all nine labels before the app writes
    into columns D-I. Reads accept a Name+Email prefix (_validate_header),
    but writing into unlabeled columns could silently overwrite data the
    owner keeps there -- the full header is their explicit consent that
    those columns belong to Agent Hub. The error hands them the exact row
    to paste."""
    service = _get_service(account)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=_sheet_id(account), range="A1:I1")
        .execute()
    )
    values = result.get("values", [])
    header = [cell.strip().lower() for cell in (values[0] if values else [])]
    if header != [name.lower() for name in EXPECTED_HEADER]:
        raise RuntimeError(
            "Before Agent Hub can write statuses, row 1 must contain the full "
            "header (this protects any data you keep in unlabeled columns). "
            "Paste this into row 1: " + ", ".join(EXPECTED_HEADER) + ". "
            "Starting a fresh sheet? Settings has a starter file to download "
            "with the header already in place."
        )


_EMAIL_COL_LETTER = chr(ord("A") + COL_EMAIL)


def _verify_row_index(account, row_index, expect_email):
    """Guards status writes against stale row indexes. Row numbers are
    captured when the sheet is read; if the owner sorts or inserts rows
    before the write lands, the index now points at a different contact and
    the write would corrupt their row. If the expected email still lives at
    row_index, use it; if it moved, follow it; if it's gone, fail loudly
    rather than write into the wrong row.

    Fast path first: read just the one email cell at row_index. In the
    overwhelmingly common case -- nothing moved -- that's one cell instead of
    the whole sheet (get_all_rows, range A:I), which matters once this runs
    continuously for every account rather than occasionally for one. Only
    falls through to the full scan when the fast path doesn't confirm the
    row is still there, and that fallback is byte-for-byte the same
    follow-or-fail logic as before."""
    wanted = expect_email.strip().lower()

    result = (
        _get_service(account).spreadsheets().values()
        .get(spreadsheetId=_sheet_id(account), range=f"{_EMAIL_COL_LETTER}{row_index}")
        .execute()
    )
    values = result.get("values", [])
    if values and values[0] and values[0][0].strip().lower() == wanted:
        return row_index

    rows = get_all_rows(account)
    for idx, row in rows:
        if row[COL_EMAIL].strip().lower() == wanted:
            return idx
    raise RuntimeError(
        f"Couldn't find {expect_email} in the sheet anymore — it may have been "
        "edited or the row deleted. No status was written for this contact."
    )


def update_row(account, row_index, status=None, thread_id=None, sent_at=None, email_body=None, email_confidence=None, expect_email=None):
    if expect_email:
        row_index = _verify_row_index(account, row_index, expect_email)
    cells = _row_update_cells(row_index, status, thread_id, sent_at, email_body, email_confidence)
    if not cells:
        return

    _get_service(account).spreadsheets().values().batchUpdate(
        spreadsheetId=_sheet_id(account),
        body={
            "valueInputOption": "RAW",
            "data": [{"range": cell_range, "values": [[value]]} for cell_range, value in cells],
        },
    ).execute()


def mark_unsubscribed(account, email):
    """Best-effort: set every row matching this email to status "Unsubscribed"
    so it drops out of future batches and is visible in the sheet. Returns the
    number of rows updated. Called from the public unsubscribe handler, so it
    must never raise on a missing/duplicate address -- a recipient opting out
    cannot be shown an error because our sheet is in an unexpected state; the
    suppression list is the authoritative stop, this is the human-visible echo."""
    wanted = (email or "").strip().lower()
    if not wanted:
        return 0
    updated = 0
    for idx, row in get_all_rows(account):
        if row[COL_EMAIL].strip().lower() == wanted:
            update_row(account, idx, status="Unsubscribed", expect_email=email)
            updated += 1
    return updated


def append_rows(account, rows):
    """rows: list of 9-value lists, in COL_* order. Appended after the last row."""
    if not rows:
        return
    service = _get_service(account)
    service.spreadsheets().values().append(
        spreadsheetId=_sheet_id(account),
        range=SHEET_RANGE,
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()
