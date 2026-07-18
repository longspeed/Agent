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

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SECRET_KEY = os.environ["SUPABASE_SECRET_KEY"]

# Name, Email, Company, Status, ThreadID, SentAt, EmailBody, LeadReason, EmailConfidence (first tab).
# EmailConfidence is now a verification state: "verified", "unverified", or
# "invalid". Only "verified" addresses are eligible to send.
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
