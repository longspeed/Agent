"""Per-account Google OAuth. The app's OAuth client (credentials.json) is
shared; each account completes its own in-browser consent flow and its token
is stored encrypted on its `accounts` row — there is no token.json on disk."""
import json

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

import accounts_db
from config import (
    SCOPES,
    CREDENTIALS_FILE,
    GOOGLE_OAUTH_REDIRECT_URI,
    GOOGLE_LOGIN_REDIRECT_URI,
    LOGIN_SCOPES,
)


def _flow() -> Flow:
    # PKCE is off: the code_verifier it generates lives on this Flow instance
    # and would need to survive from build_auth_url() (one request) to
    # exchange_code() (a separate request, a separate Flow instance) —
    # there's nowhere to carry it across the redirect. Unnecessary anyway:
    # this is a confidential "Web application" OAuth client (has a client
    # secret), so PKCE isn't required the way it is for public clients.
    return Flow.from_client_secrets_file(
        CREDENTIALS_FILE,
        scopes=SCOPES,
        redirect_uri=GOOGLE_OAUTH_REDIRECT_URI,
        autogenerate_code_verifier=False,
    )


def build_auth_url(state: str) -> str:
    """URL to send the customer's browser to for the Google consent screen.
    `state` must be a signed token identifying the account (verified on callback)."""
    auth_url, _ = _flow().authorization_url(
        access_type="offline",       # we need a refresh token for background sends
        prompt="consent",            # force refresh_token even on re-consent
        include_granted_scopes="true",
        state=state,
    )
    return auth_url


def exchange_code(code: str) -> str:
    """Exchanges the callback ?code= for credentials; returns the token JSON."""
    flow = _flow()
    flow.fetch_token(code=code)
    return flow.credentials.to_json()


def get_credentials(account: dict) -> Credentials:
    """Builds Credentials for one account from its stored token, refreshing
    (and persisting the refresh) if expired. Raises RuntimeError if the
    account hasn't connected Google yet."""
    token_json = accounts_db.get_google_token(account)
    if not token_json:
        raise RuntimeError("This account hasn't connected its Google account yet")

    creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
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
        accounts_db.set_google_token(account["id"], creds.to_json())

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
