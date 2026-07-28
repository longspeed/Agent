"""Per-account authentication: PBKDF2 password hashing and signed session
tokens that carry which account is logged in (not just "authenticated")."""
import base64
import hashlib
import hmac
import secrets
import time

from urllib.parse import quote

from config import APP_SECRET_KEY, PUBLIC_BASE_URL

COOKIE_NAME = "session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 14  # 14 days
PBKDF2_ITERATIONS = 260_000


# --- Passwords ---------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False  # account was created via Google sign-in, has no password
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != "pbkdf2":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
        ).hex()
        return hmac.compare_digest(candidate, digest)
    except (ValueError, AttributeError):
        return False


# --- Session tokens ----------------------------------------------------------
# Format: "<account_id>.<expires_at>.<hmac>". The same signed format doubles
# as the short-lived OAuth `state` parameter (see server.py).

def _sign(payload: str) -> str:
    return hmac.new(APP_SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


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


# --- Pre-login OAuth state ----------------------------------------------------
# CSRF nonce for the "Sign in with Google" flow, used before an account_id
# exists. Format: "<expires_at>.<hmac>" — one dot, so it can never be
# mistaken for (or forged from) a session token, which always has two.

def create_oauth_state(ttl_seconds: int = 600) -> str:
    expires_at = str(int(time.time()) + ttl_seconds)
    return f"{expires_at}.{_sign(expires_at)}"


def verify_oauth_state(token: str | None) -> bool:
    if not token or token.count(".") != 1:
        return False
    expires_at, _, signature = token.partition(".")
    if not expires_at.isdigit() or not hmac.compare_digest(signature, _sign(expires_at)):
        return False
    return int(expires_at) > int(time.time())


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
    if not hmac.compare_digest(signature, _sign(payload)):
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
