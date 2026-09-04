from googleapiclient.discovery import build

from google_auth import get_credentials

MAX_SHEETS = 200


def list_spreadsheets(account):
    """Returns [{"id": ..., "name": ...}] for spreadsheets the account's
    connected Google user can see (owned or shared with them), most recently
    modified first. Read-only — no file contents are ever fetched."""
    service = build("drive", "v3", credentials=get_credentials(account, "sheets"))
    files = []
    page_token = None
    while True:
        resp = service.files().list(
            q="mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
            fields="nextPageToken, files(id, name)",
            orderBy="modifiedByMeTime desc",
            pageSize=100,
            pageToken=page_token,
        ).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token or len(files) >= MAX_SHEETS:
            break
    return [{"id": f["id"], "name": f["name"]} for f in files]
