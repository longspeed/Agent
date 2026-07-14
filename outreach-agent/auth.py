import hashlib
import hmac
import time

from config import APP_PASSWORD, APP_SECRET_KEY

COOKIE_NAME = "session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 14  # 14 days


def _sign(payload: str) -> str:
    return hmac.new(APP_SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def create_session_token() -> str:
    expires_at = str(int(time.time()) + SESSION_TTL_SECONDS)
    return f"{expires_at}.{_sign(expires_at)}"


def verify_session_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expires_at, _, signature = token.partition(".")
    if not expires_at.isdigit() or not hmac.compare_digest(signature, _sign(expires_at)):
        return False
    return int(expires_at) > int(time.time())


def check_password(password: str) -> bool:
    return hmac.compare_digest(password, APP_PASSWORD)
