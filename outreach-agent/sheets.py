from googleapiclient.discovery import build

from config import GOOGLE_SHEET_ID, SHEET_RANGE
from google_auth import get_credentials

# Column order: Name, Email, Company, Status, ThreadID, SentAt, EmailBody, LeadReason, EmailConfidence
(
    COL_NAME, COL_EMAIL, COL_COMPANY, COL_STATUS, COL_THREAD_ID, COL_SENT_AT, COL_EMAIL_BODY,
    COL_LEAD_REASON, COL_EMAIL_CONFIDENCE,
) = range(9)


_service = None


def _get_service():
    global _service
    if _service is None:
        _service = build("sheets", "v4", credentials=get_credentials())
    return _service


def get_all_rows():
    """Returns list of (row_index, row_values). row_index is 1-based sheet row number."""
    service = _get_service()
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=GOOGLE_SHEET_ID, range=SHEET_RANGE)
        .execute()
    )
    values = result.get("values", [])
    rows = []
    for i, row in enumerate(values[1:], start=2):  # skip header row, sheet rows are 1-based
        padded = row + [""] * (9 - len(row))
        rows.append((i, padded))
    return rows


def get_pending_rows():
    """Rows with no Status yet (not sent)."""
    return [(idx, row) for idx, row in get_all_rows() if not row[COL_STATUS].strip()]


def get_sent_rows():
    """Rows already sent, awaiting replies."""
    return [(idx, row) for idx, row in get_all_rows() if row[COL_STATUS].strip() == "Sent"]


def update_row(row_index, status=None, thread_id=None, sent_at=None, email_body=None):
    service = _get_service()
    updates = []
    if status is not None:
        updates.append((f"D{row_index}", status))
    if thread_id is not None:
        updates.append((f"E{row_index}", thread_id))
    if sent_at is not None:
        updates.append((f"F{row_index}", sent_at))
    if email_body is not None:
        updates.append((f"G{row_index}", email_body))

    if not updates:
        return

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=GOOGLE_SHEET_ID,
        body={
            "valueInputOption": "RAW",
            "data": [{"range": cell_range, "values": [[value]]} for cell_range, value in updates],
        },
    ).execute()


def append_rows(rows):
    """rows: list of 9-value lists, in COL_* order. Appended after the last row."""
    if not rows:
        return
    service = _get_service()
    service.spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=SHEET_RANGE,
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()
