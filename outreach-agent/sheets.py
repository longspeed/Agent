from googleapiclient.discovery import build

from config import SHEET_RANGE
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
    rows = []
    for i, row in enumerate(values[1:], start=2):  # skip header row, sheet rows are 1-based
        padded = row + [""] * (9 - len(row))
        rows.append((i, padded))
    return rows


def get_pending_rows(account):
    """Rows with no Status yet (not sent)."""
    return [(idx, row) for idx, row in get_all_rows(account) if not row[COL_STATUS].strip()]


def get_sent_rows(account):
    """Rows already sent, awaiting replies."""
    return [(idx, row) for idx, row in get_all_rows(account) if row[COL_STATUS].strip() == "Sent"]


def _row_update_cells(row_index, status=None, thread_id=None, sent_at=None, email_body=None):
    cells = []
    if status is not None:
        cells.append((f"D{row_index}", status))
    if thread_id is not None:
        cells.append((f"E{row_index}", thread_id))
    if sent_at is not None:
        cells.append((f"F{row_index}", sent_at))
    if email_body is not None:
        cells.append((f"G{row_index}", email_body))
    return cells


def update_row(account, row_index, status=None, thread_id=None, sent_at=None, email_body=None):
    cells = _row_update_cells(row_index, status, thread_id, sent_at, email_body)
    if not cells:
        return

    _get_service(account).spreadsheets().values().batchUpdate(
        spreadsheetId=_sheet_id(account),
        body={
            "valueInputOption": "RAW",
            "data": [{"range": cell_range, "values": [[value]]} for cell_range, value in cells],
        },
    ).execute()


def batch_update_rows(account, updates):
    """updates: list of dicts with row_index and any of status/thread_id/sent_at/email_body.
    Writes every row in one API round-trip instead of one call per row."""
    cells = [
        cell
        for u in updates
        for cell in _row_update_cells(
            u["row_index"], u.get("status"), u.get("thread_id"), u.get("sent_at"), u.get("email_body")
        )
    ]
    if not cells:
        return

    _get_service(account).spreadsheets().values().batchUpdate(
        spreadsheetId=_sheet_id(account),
        body={
            "valueInputOption": "RAW",
            "data": [{"range": cell_range, "values": [[value]]} for cell_range, value in cells],
        },
    ).execute()


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
