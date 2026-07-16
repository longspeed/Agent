"""Per-account authentication: PBKDF2 password hashing and signed session
tokens that carry which account is logged in (not just "authenticated")."""
import hashlib
import hmac
import secrets
import time

from config import APP_SECRET_KEY

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
