"""Per-account Google OAuth. The app's OAuth client (credentials.json) is
shared; each account completes its own in-browser consent flow and its token
is stored encrypted on its `accounts` row — there is no token.json on disk."""
import base64
import json

from cryptography.fernet import InvalidToken
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

import accounts_db
from config import (
    CREDENTIALS_FILE,
    GOOGLE_OAUTH_REDIRECT_URI,
    GOOGLE_LOGIN_REDIRECT_URI,
    GMAIL_MONITOR_SCOPES,
    GMAIL_SEND_SCOPES,
    SHEETS_SCOPES,
    LOGIN_SCOPES,
)

CAPABILITY_SCOPES = {
    "monitor": tuple(GMAIL_MONITOR_SCOPES),
    "send": tuple(GMAIL_SEND_SCOPES),
    "sheets": tuple(SHEETS_SCOPES),
}
LEGACY_GMAIL_MODIFY = "https://www.googleapis.com/auth/gmail.modify"


class MissingGoogleCapability(RuntimeError):
    def __init__(self, capability: str):
        super().__init__(f"Google permission required: {capability}")
        self.capability = capability


class GoogleIdentityMismatch(RuntimeError):
    """Raised when an incremental grant belongs to another Google account."""


def _flow(capability: str) -> Flow:
    # PKCE is off: the code_verifier it generates lives on this Flow instance
    # and would need to survive from build_auth_url() (one request) to
    # exchange_code() (a separate request, a separate Flow instance) —
    # there's nowhere to carry it across the redirect. Unnecessary anyway:
    # this is a confidential "Web application" OAuth client (has a client
    # secret), so PKCE isn't required the way it is for public clients.
    return Flow.from_client_secrets_file(
        CREDENTIALS_FILE,
        scopes=CAPABILITY_SCOPES[capability],
        redirect_uri=GOOGLE_OAUTH_REDIRECT_URI,
        autogenerate_code_verifier=False,
    )


def build_auth_url(state: str, capability: str = "monitor") -> str:
    """URL to send the customer's browser to for the Google consent screen.
    `state` must be a signed token identifying the account (verified on callback)."""
    auth_url, _ = _flow(capability).authorization_url(
        access_type="offline",       # we need a refresh token for background sends
        prompt="consent",            # force refresh_token even on re-consent
        include_granted_scopes="true",
        state=state,
    )
    return auth_url


def _email_from_id_token(credentials: Credentials) -> str:
    """Mailbox identity from the ID token that came back with the code
    exchange. Only trustworthy at exchange time -- the token arrived directly
    from Google's token endpoint over TLS, so the claims need no second
    verification here. Returns "" when no ID token was issued (grants that
    requested neither openid nor email scopes)."""
    id_token = getattr(credentials, "id_token", None)
    if not id_token:
        return ""
    try:
        payload_b64 = id_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        return (claims.get("email") or "").strip().lower()
    except Exception as e:
        print(f"Could not read an email out of the Google ID token ({e}).")
        return ""


_MAIL_SCOPES = {GMAIL_MONITOR_SCOPES[0], GMAIL_SEND_SCOPES[0], LEGACY_GMAIL_MODIFY}


def exchange_code(code: str, capability: str = "monitor") -> str:
    """Exchanges the callback ?code= for credentials; returns the token JSON."""
    flow = _flow(capability)
    flow.fetch_token(code=code)
    token = json.loads(flow.credentials.to_json())
    # Gmail profile is available under a mail grant and gives us a stable
    # mailbox identity without requesting broader OpenID/userinfo scopes.
    granted_scopes = set(flow.credentials.scopes or [])
    if _MAIL_SCOPES & granted_scopes:
        profile = build("gmail", "v1", credentials=flow.credentials).users().getProfile(
            userId="me"
        ).execute()
        email = (profile.get("emailAddress") or "").strip().lower()
        if not email:
            raise RuntimeError("Google did not return a Gmail mailbox identity")
    else:
        # A sheets-only grant carries no Gmail scope, so the Gmail API would
        # answer 403 and the whole connect flow would die with a misleading
        # error. The consent response echoes the account's already-granted
        # openid scopes, so the ID token names the mailbox instead.
        email = _email_from_id_token(flow.credentials)
        if not email:
            raise RuntimeError(
                "Google did not return a verifiable mailbox identity for this grant"
            )
    token["google_account_email"] = email
    return json.dumps(token)


def merge_token_json(existing_json: str | None, granted_json: str) -> str:
    """Preserve refresh access and scope history across incremental grants."""
    granted = json.loads(granted_json)
    existing = json.loads(existing_json) if existing_json else {}
    existing_email = (existing.get("google_account_email") or "").strip().lower()
    granted_email = (granted.get("google_account_email") or "").strip().lower()
    if existing_json and (not existing_email or not granted_email):
        raise GoogleIdentityMismatch(
            "Reconnect Google so Sendkeep can verify the mailbox identity before merging permissions"
        )
    if existing_email and granted_email and existing_email != granted_email:
        raise GoogleIdentityMismatch(
            f"The new Google grant belongs to {granted_email}, not {existing_email}"
        )
    if not granted.get("refresh_token") and existing.get("refresh_token"):
        granted["refresh_token"] = existing["refresh_token"]
    scopes = set(existing.get("scopes") or []) | set(granted.get("scopes") or [])
    if scopes:
        granted["scopes"] = sorted(scopes)
    if existing_email:
        granted["google_account_email"] = existing_email
    return json.dumps(granted)


def _scope_set(account: dict) -> set[str]:
    try:
        token_json = accounts_db.get_google_token(account)
    except InvalidToken:
        return set()
    if not token_json:
        return set()
    try:
        return set(json.loads(token_json).get("scopes") or [])
    except (TypeError, ValueError):
        return set()


def capability_status(account: dict) -> dict[str, bool]:
    scopes = _scope_set(account)
    legacy = LEGACY_GMAIL_MODIFY in scopes
    return {
        "monitor": legacy or set(GMAIL_MONITOR_SCOPES).issubset(scopes),
        "send": legacy or set(GMAIL_SEND_SCOPES).issubset(scopes),
        "sheets": set(SHEETS_SCOPES).issubset(scopes),
    }


def get_credentials(account: dict, capability: str = "monitor") -> Credentials:
    """Builds Credentials for one account from its stored token, refreshing
    (and persisting the refresh) if expired. Raises RuntimeError if the
    account hasn't connected Google yet."""
    try:
        token_json = accounts_db.get_google_token(account)
    except InvalidToken:
        # The Fernet key derived from APP_SECRET_KEY no longer matches the one
        # this token was encrypted under (the secret was rotated). The token
        # is permanently undecryptable, not just temporarily invalid -- clear
        # it the same way an unrefreshable token is cleared below, so the
        # account isn't stuck showing "connected" while dead.
        accounts_db.set_google_token(account["id"], None)
        raise RuntimeError("Google connection expired — reconnect Google in Settings")
    if not token_json:
        raise RuntimeError("This account hasn't connected its Google account yet")

    if capability not in CAPABILITY_SCOPES:
        raise ValueError(f"Unknown Google capability: {capability}")
    if not capability_status(account)[capability]:
        raise MissingGoogleCapability(capability)
    token_info = json.loads(token_json)
    creds = Credentials.from_authorized_user_info(token_info, token_info.get("scopes"))
    if not creds.valid:
        if not (creds.expired and creds.refresh_token):
            raise RuntimeError("Stored Google credentials are invalid — reconnect Google")
        try:
            creds.refresh(Request())
        except RefreshError:
            # Token was issued under a different OAuth client (e.g. credentials.json
            # was replaced) or was revoked externally — it will never refresh again.
            # Clear it so the account isn't stuck showing "connected" while dead.
            accounts_db.set_google_token(account["id"], None)
            raise RuntimeError("Google connection expired — reconnect Google in Settings")
        refreshed = json.loads(creds.to_json())
        # Credentials.to_json() drops our mailbox-binding metadata. Losing it
        # makes the next incremental grant unverifiable and falsely looks like
        # an account switch. Keep identity and the full accumulated scope set.
        refreshed["google_account_email"] = token_info.get("google_account_email", "")
        refreshed["scopes"] = sorted(
            set(token_info.get("scopes") or []) | set(refreshed.get("scopes") or [])
        )
        if not refreshed.get("refresh_token") and token_info.get("refresh_token"):
            refreshed["refresh_token"] = token_info["refresh_token"]
        accounts_db.set_google_token(account["id"], json.dumps(refreshed))

    return creds


# --- "Sign in with Google" (identity only, no Gmail/Sheets access) ---------
# Deliberately separate from the flow above: logging in with a Google
# identity doesn't imply that Google account should also be the one sending
# mail — someone may sign in with a personal Google account but connect a
# work Gmail from Settings afterward.

def _login_flow() -> Flow:
    # See the comment on _flow() above — same reason PKCE is off here.
    return Flow.from_client_secrets_file(
        CREDENTIALS_FILE,
        scopes=LOGIN_SCOPES,
        redirect_uri=GOOGLE_LOGIN_REDIRECT_URI,
        autogenerate_code_verifier=False,
    )


def build_login_auth_url(state: str) -> str:
    auth_url, _ = _login_flow().authorization_url(
        access_type="online",  # identity only — no refresh token to store
        include_granted_scopes="true",
        state=state,
    )
    return auth_url


def exchange_login_code(code: str) -> dict:
    """Exchanges the callback ?code= for the signed-in Google identity.
    Returns {"email": ..., "google_id": ...}."""
    flow = _login_flow()
    flow.fetch_token(code=code)
    userinfo = build("oauth2", "v2", credentials=flow.credentials).userinfo().get().execute()
    return {"email": userinfo["email"], "google_id": userinfo["id"]}
