import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")
load_dotenv()  # also pick up a repo-root .env if one exists

# Google silently includes any scope the user has already granted this OAuth
# client (e.g. openid/userinfo.email from a prior "Sign in with Google")
# alongside whatever a given flow explicitly requested. oauthlib treats that
# scope superset as a hard error by default instead of the harmless thing it
# is — this must be set before any Flow is constructed.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

# App-level config only. Anything that differs per customer (Gmail token,
# sheet id, sender name, calendar link, notify email) lives on their row in
# the Supabase `accounts` table, not here.

OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-oss-20b:free")

# Local-dev-only alternative backend: a CLIProxyAPI instance running on this
# machine, fronting a personal Claude/Kimi OAuth login instead of a metered
# OpenRouter key. Unset in every real deployment — when unset, _chat() calls
# OpenRouter exactly as before. Never point this at anything but localhost;
# CLIProxyAPI's auth is tied to whatever machine is running it.
CLIPROXY_BASE_URL = os.environ.get("CLIPROXY_BASE_URL", "")
CLIPROXY_API_KEY = os.environ.get("CLIPROXY_API_KEY", "")
CLIPROXY_MODEL = os.environ.get("CLIPROXY_MODEL", "claude-haiku-4-5-20251001")

APP_SECRET_KEY = os.environ["APP_SECRET_KEY"]

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")  # only needed for the lead sourcing agent
DAILY_SEND_LIMIT = int(os.environ.get("DAILY_SEND_LIMIT", "25"))

# Must stay byte-identical to the `meeting_purpose` column default in the
# accounts table (see README). An account still carrying this string has never
# told us what the meeting is actually for, and the model cannot write "what's
# in it for them" out of it -- agent.py refuses to generate until it changes.
DEFAULT_MEETING_PURPOSE = "a quick intro call to see if there's a fit to work together"

# Absolute, publicly reachable origin for links that must survive outside the
# app -- today just the unsubscribe link baked into every outreach email. It
# has to work from a stranger's mail client, so localhost is only ever right in
# development.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SECRET_KEY = os.environ["SUPABASE_SECRET_KEY"]

# Name, Email, Company, Status, ThreadID, SentAt, EmailBody, LeadReason, EmailConfidence (first tab).
# EmailConfidence is informational only (a leftover verification state) --
# there is no verification gate: any approved row with an email can send.
# The sheet owner is trusted to only approve addresses they're comfortable
# emailing (verification via NeverBounce was removed 2026-07-18, see TODOS.md).
SHEET_RANGE = "A:I"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.modify",
    # Read-only file listing (name + id only, no file contents) — lets
    # customers pick their lead sheet from a list instead of pasting an ID.
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]
# The OAuth *client* (this app's identity with Google) is shared; each
# account runs its own consent flow against it and stores its own token.
CREDENTIALS_FILE = str(BASE_DIR / "credentials.json")
GOOGLE_OAUTH_REDIRECT_URI = os.environ.get(
    "GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8000/api/google/callback"
)
# Separate, lower-privilege flow used for "Sign in with Google" (identity
# only) — distinct from GOOGLE_OAUTH_REDIRECT_URI above, which is the
# post-login "connect Gmail + Sheets" flow. Both must be registered as
# authorized redirect URIs on the same OAuth client.
GOOGLE_LOGIN_REDIRECT_URI = os.environ.get(
    "GOOGLE_LOGIN_REDIRECT_URI", "http://localhost:8000/auth/google/callback"
)
LOGIN_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email"]
