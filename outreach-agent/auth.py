"""Per-account authentication: signed session tokens that carry which account
is logged in (not just "authenticated")."""
import base64
import hashlib
import hmac
import secrets
import time

from urllib.parse import quote

from config import APP_SECRET_KEY, SESSION_SIGNING_KEY, PUBLIC_BASE_URL

COOKIE_NAME = "session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days


# --- Session tokens ----------------------------------------------------------
# Format: "<account_id>.<expires_at>.<hmac>". The same signed format doubles
# as the short-lived OAuth `state` parameter (see server.py).

def _sign(payload: str, key: str | None = None) -> str:
    return hmac.new((key or SESSION_SIGNING_KEY).encode(), payload.encode(), hashlib.sha256).hexdigest()


def create_session_token(account_id: str, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    payload = f"{account_id}.{int(time.time()) + ttl_seconds}"
    return f"{payload}.{_sign(payload)}"


def verify_session_token(token: str | None) -> str | None:
    """Returns the account_id if the token is valid and unexpired, else None."""
    if not token or token.count(".") != 2:
        return None
    account_id, expires_at, signature = token.split(".")
    payload = f"{account_id}.{expires_at}"
    if not expires_at.isdigit() or not hmac.compare_digest(signature, _sign(payload)):
        return None
    if int(expires_at) <= int(time.time()):
        return None
    return account_id


# --- Incremental Google grant state -----------------------------------------

def create_google_grant_state(account_id: str, capability: str, ttl_seconds: int = 600) -> str:
    """Signed OAuth state binding a capability grant to one account."""
    expires_at = str(int(time.time()) + ttl_seconds)
    payload = f"google-grant:{account_id}:{capability}:{expires_at}"
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{encoded}.{_sign(payload)}"


def verify_google_grant_state(token: str | None) -> tuple[str, str] | None:
    if not token or token.count(".") != 1:
        return None
    encoded, signature = token.split(".", 1)
    try:
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(signature, _sign(payload)):
        return None
    parts = payload.split(":")
    if len(parts) != 4 or parts[0] != "google-grant" or not parts[3].isdigit():
        return None
    if int(parts[3]) <= int(time.time()):
        return None
    return parts[1], parts[2]


# --- Pre-login OAuth state ----------------------------------------------------
# CSRF nonce for the "Sign in with Google" flow, used before an account_id
# exists. The random nonce makes two starts in the same second independent.

def create_oauth_state(ttl_seconds: int = 600) -> str:
    payload = f"oauth:{int(time.time()) + ttl_seconds}:{secrets.token_urlsafe(24)}"
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{encoded}.{_sign(payload)}"


def verify_oauth_state(token: str | None) -> bool:
    if not token or token.count(".") != 1:
        return False
    encoded, _, signature = token.partition(".")
    try:
        payload = base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4)
        ).decode()
    except (ValueError, UnicodeDecodeError):
        return False
    parts = payload.split(":", 2)
    if (
        len(parts) != 3 or parts[0] != "oauth" or not parts[1].isdigit()
        or not parts[2]
        or not hmac.compare_digest(signature, _sign(payload))
    ):
        return False
    return int(parts[1]) > int(time.time())


# --- Unsubscribe tokens -------------------------------------------------------
# Baked into every outreach email so a recipient can opt out in one click,
# with no login. Deliberately has NO expiry: an unsubscribe link must keep
# working forever, including long after the original email. The payload is
# namespaced with a literal "unsub" prefix and signed whole, so a token from
# this family can never be replayed as a session token or OAuth state (whose
# payloads have no such prefix) even though all three share _sign. base64url
# keeps the email address out of the dotted structure and URL-safe.

def create_unsubscribe_token(account_id: str, email: str) -> str:
    payload = f"unsub:{account_id}:{email.strip().lower()}"
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{encoded}.{_sign(payload)}"


def verify_unsubscribe_token(token: str | None) -> tuple[str, str] | None:
    """Returns (account_id, email) if the token is authentic, else None."""
    if not token or token.count(".") != 1:
        return None
    encoded, _, signature = token.partition(".")
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(padded).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    # No expiry, must keep working forever -- so a link signed under
    # APP_SECRET_KEY (SESSION_SIGNING_KEY's default before an operator ever
    # rotates it) still has to verify after that rotation happens. Session
    # and OAuth-state tokens don't get this: they're short-lived, and a
    # rotation logging everyone out is correct, not a defect.
    valid = any(
        hmac.compare_digest(signature, _sign(payload, key))
        for key in {SESSION_SIGNING_KEY, APP_SECRET_KEY}
    )
    if not valid:
        return None
    parts = payload.split(":", 2)
    if len(parts) != 3 or parts[0] != "unsub":
        return None
    return parts[1], parts[2]


def unsubscribe_url(account_id: str, email: str) -> str:
    """Absolute one-click opt-out link for an outreach email. Absolute because
    it has to resolve from a stranger's mail client, where relative paths are
    meaningless (see PUBLIC_BASE_URL)."""
    token = create_unsubscribe_token(account_id, email)
    return f"{PUBLIC_BASE_URL}/unsubscribe?t={quote(token, safe='')}"
