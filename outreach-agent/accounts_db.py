"""Supabase-backed account storage: one row per customer, holding their login,
their encrypted Google OAuth token, their sheet id, and their outreach settings.
Also records per-account usage of metered third-party services (LLM, search)."""
import base64
import hashlib
import re

from cryptography.fernet import Fernet
from supabase import create_client

from config import SUPABASE_URL, SUPABASE_SECRET_KEY, APP_SECRET_KEY

ACCOUNTS_TABLE = "accounts"
USAGE_TABLE = "usage_events"

# Settings a customer may edit about themselves via the API.
EDITABLE_SETTINGS = (
    "sender_name",
    "meeting_purpose",
    "calendar_booking_link",
    "notify_email",
    "google_sheet_id",
)

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
    return _client


# --- Google token encryption at rest ---------------------------------------

_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(APP_SECRET_KEY.encode()).digest()))


def encrypt_secret(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    return _fernet.decrypt(ciphertext.encode()).decode()


# --- Accounts ---------------------------------------------------------------

def create_account(email: str, password_hash: str) -> dict:
    """Raises ValueError if the email is already registered."""
    existing = get_account_by_email(email)
    if existing:
        raise ValueError("An account with this email already exists")
    result = _get_client().table(ACCOUNTS_TABLE).insert({
        "email": email,
        "password_hash": password_hash,
    }).execute()
    return result.data[0]


def get_account_by_email(email: str) -> dict | None:
    result = _get_client().table(ACCOUNTS_TABLE).select("*").eq("email", email).execute()
    return result.data[0] if result.data else None


def get_account(account_id: str) -> dict | None:
    result = _get_client().table(ACCOUNTS_TABLE).select("*").eq("id", account_id).execute()
    return result.data[0] if result.data else None


def get_account_by_google_id(google_id: str) -> dict | None:
    result = _get_client().table(ACCOUNTS_TABLE).select("*").eq("google_id", google_id).execute()
    return result.data[0] if result.data else None


def link_or_create_google_account(email: str, google_id: str) -> dict:
    """Signs in with a Google identity: reuses an account already linked to
    this Google id, links it to an existing password account with the same
    (verified) email, or creates a fresh password-less account."""
    existing = get_account_by_google_id(google_id)
    if existing:
        return existing

    by_email = get_account_by_email(email)
    if by_email:
        result = (
            _get_client().table(ACCOUNTS_TABLE)
            .update({"google_id": google_id}).eq("id", by_email["id"]).execute()
        )
        return result.data[0]

    result = _get_client().table(ACCOUNTS_TABLE).insert({
        "email": email,
        "google_id": google_id,
        "password_hash": None,
    }).execute()
    return result.data[0]


_SHEET_URL_ID = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


def _normalize_sheet_id(value: str) -> str:
    """Accepts either a bare sheet ID or a full Google Sheets URL (people
    reliably paste the URL from their browser bar) and returns just the ID.
    Falls back to stripping query string, fragment, and trailing path
    segments so accidental junk on a bare ID (or an unrecognized URL shape)
    still comes out clean instead of passing through unchanged."""
    match = _SHEET_URL_ID.search(value)
    if match:
        return match.group(1)
    return value.split("?")[0].split("#")[0].split("/")[0]


def update_settings(account_id: str, settings: dict) -> dict:
    fields = {k: v for k, v in settings.items() if k in EDITABLE_SETTINGS and v is not None}
    if "google_sheet_id" in fields:
        fields["google_sheet_id"] = _normalize_sheet_id(fields["google_sheet_id"])
    if not fields:
        return get_account(account_id)
    result = (
        _get_client().table(ACCOUNTS_TABLE).update(fields).eq("id", account_id).execute()
    )
    return result.data[0]


def set_google_token(account_id: str, token_json: str | None):
    """Stores the account's Google OAuth token (encrypted), or clears it."""
    value = encrypt_secret(token_json) if token_json else None
    _get_client().table(ACCOUNTS_TABLE).update({"google_token": value}).eq("id", account_id).execute()


def get_google_token(account: dict) -> str | None:
    """Returns the decrypted token JSON for an account row, or None."""
    if not account.get("google_token"):
        return None
    return decrypt_secret(account["google_token"])


# --- Usage metering ----------------------------------------------------------

def record_usage(account_id: str, kind: str, quantity: int = 1, detail: str = ""):
    _get_client().table(USAGE_TABLE).insert({
        "account_id": account_id,
        "kind": kind,
        "quantity": quantity,
        "detail": detail[:200],
    }).execute()


def usage_summary(account_id: str) -> dict:
    """Totals per kind, e.g. {"llm": {"events": 12, "quantity": 48211}, ...}."""
    result = (
        _get_client().table(USAGE_TABLE)
        .select("kind, quantity")
        .eq("account_id", account_id)
        .execute()
    )
    summary: dict[str, dict] = {}
    for row in result.data:
        bucket = summary.setdefault(row["kind"], {"events": 0, "quantity": 0})
        bucket["events"] += 1
        bucket["quantity"] += row["quantity"]
    return summary
