import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")
load_dotenv()  # also pick up a repo-root .env if one exists


def _make_output_utf8_safe(stream):
    """Stop a stray non-Latin character in generated text from crashing a print.

    Windows consoles default to cp1252, and this app routinely prints text a
    language model wrote -- including the validator's own complaint about a stray
    non-Latin token, which quotes the offending characters back so the retry
    prompt can name them. Encoding that to cp1252 raises UnicodeEncodeError, and
    it does so from inside the except handler that was reporting the problem
    (send_outreach logs each failed contact there), so the error escapes the
    per-contact loop and one garbled draft takes down the whole batch.

    Applied to the real streams below, at the one import every module shares."""
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        # Not a reconfigurable text stream (already wrapped, or a pipe a host
        # process swapped in). Nothing to do, and nothing worth failing over.
        pass
    return stream


for _stream in (sys.stdout, sys.stderr):
    _make_output_utf8_safe(_stream)

# Google silently includes any scope the user has already granted this OAuth
# client (e.g. openid/userinfo.email from a prior "Sign in with Google")
# alongside whatever a given flow explicitly requested. oauthlib treats that
# scope superset as a hard error by default instead of the harmless thing it
# is — this must be set before any Flow is constructed.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

# App-level config only. Anything that differs per customer (Gmail token,
# sheet id, sender name, calendar link, notify email) lives on their row in
# the Supabase `accounts` table, not here.

APP_SECRET_KEY = os.environ["APP_SECRET_KEY"]

# Signs session/OAuth-state/unsubscribe tokens (auth.py). Kept separate from
# APP_SECRET_KEY -- which derives the Fernet key encrypting stored Google
# tokens in accounts_db.py -- so that one secret leaking doesn't also hand
# over the other. Defaults to APP_SECRET_KEY when unset, so no deployment
# needs to change anything until it deliberately sets a distinct value.
SESSION_SIGNING_KEY = os.environ.get("SESSION_SIGNING_KEY") or APP_SECRET_KEY


# --- LLM providers -----------------------------------------------------------
# An ordered fallback chain rather than one endpoint, for two reasons.
#
# Free tiers are rationed in requests per day, not dollars, and the ceilings are
# low: OpenRouter allows 50/day on an account that has never been funded, which
# a single 25-send day plus the two-attempt retry loop in agent.py exhausts
# outright. A second free provider behind the first is the difference between
# degrading and stopping.
#
# And the tiers differ in what the provider may do with the prompt. A free tier
# generally reserves the right to train on what you send it, which decides what
# each endpoint is allowed to draft -- see providers.py for that policy.
#
# Every provider here speaks the OpenAI /chat/completions shape, response usage
# block included, so a new one is a row in this table and nothing else. Each
# reads {NAME}_BASE_URL, {NAME}_API_KEY, {NAME}_MODEL and {NAME}_TIER, which is
# why the pre-existing OPENROUTER_* and CLIPROXY_* variables keep working here
# unchanged.
#
# Default model ids are the current free-tier flagships; verify against the
# provider's own model list before trusting one, since they get retired.
#
# `disclosed_as` is the name this provider must appear under in privacy.html and
# dpa.html, because it receives lead data and both pages promise a complete
# sub-processor list. A test asserts the pages contain it, so adding a row here
# fails the suite until the disclosure catches up. Empty means the endpoint never
# serves a customer (local dev only) and so is not a sub-processor.
_PROVIDER_ROWS = (
    # name, label, default base url, default model, declared tier, enabled when,
    # disclosed as
    #
    # Local-dev-only backend: a CLIProxyAPI instance on this machine fronting a
    # personal Claude/Kimi OAuth login instead of a metered key. Unset in every
    # real deployment. Never point it at anything but localhost -- CLIProxyAPI's
    # auth is tied to the machine running it. Not a public free tier: the data
    # goes to the provider under the developer's own account terms, so it counts
    # as paid for routing purposes and may draft replies.
    ("cliproxy", "CLIProxyAPI", "", "claude-haiku-4-5-20251001", "paid", "url", ""),
    # Highest free daily ceiling of the three by a wide margin (~14.4k
    # requests/day, 30 RPM) and no card required. Open-weight models only.
    ("groq", "Groq", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "free", "api_key", "Groq"),
    # Google AI Studio, via its OpenAI-compatible endpoint so this shares the
    # one code path. ~1.5k requests/day free. Google states free-tier data may
    # be used to improve their products, hence tier=free.
    ("gemini", "Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash", "free", "api_key", "Google AI Studio"),
    # Last, not first: the unfunded daily ceiling is too low to be a primary.
    ("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "openai/gpt-oss-20b:free", "auto", "api_key", "OpenRouter"),
)

# Also acts as an allowlist: a provider left out of this list is not used even
# if its key is set, which is how you pin traffic to one endpoint.
LLM_PROVIDER_ORDER = [
    n.strip().lower()
    for n in os.environ.get("LLM_PROVIDER_ORDER", "cliproxy,groq,gemini,openrouter").split(",")
    if n.strip()
]

# Hard-fail a reply draft rather than send the prospect's own words to a
# free-tier endpoint that may train on them. Off by default because that would
# break the only configuration a pre-revenue install has; turn it on once a paid
# endpoint is configured and a customer DPA depends on it.
REPLY_DRAFTS_REQUIRE_PRIVATE_ENDPOINT = (
    os.environ.get("REPLY_DRAFTS_REQUIRE_PRIVATE_ENDPOINT", "").strip().lower()
    in ("1", "true", "yes")
)


def _provider_tier(name: str, declared: str, model: str) -> str:
    """"free" or "paid" -- meaning the provider's free-tier data terms apply, or
    they don't. Unrecognised values resolve to "free" on purpose: mislabelling a
    paid endpoint as free only costs us a routing option, while mislabelling a
    free one as paid sends a prospect's words somewhere they may be trained on.
    Fail toward the endpoint being untrusted."""
    tier = (os.environ.get(f"{name.upper()}_TIER") or declared).strip().lower()
    if tier == "auto":
        # OpenRouter encodes the tier in the model id, so it needs no env var.
        return "free" if model.strip().endswith(":free") else "paid"
    if tier not in ("free", "paid"):
        print(f"Unknown {name.upper()}_TIER={tier!r}; treating this endpoint as free-tier.")
        return "free"
    return tier


def _build_providers() -> list[dict]:
    rows = {r[0]: r for r in _PROVIDER_ROWS}
    for name in LLM_PROVIDER_ORDER:
        if name not in rows:
            print(f"Ignoring unknown provider {name!r} in LLM_PROVIDER_ORDER.")
    built = []
    for name in LLM_PROVIDER_ORDER:
        row = rows.get(name)
        if row is None:
            continue
        _, label, default_url, default_model, declared_tier, enabled_when, disclosed_as = row
        prefix = name.upper()
        model = os.environ.get(f"{prefix}_MODEL") or default_model
        built.append({
            "name": name,
            "label": label,
            "url": (os.environ.get(f"{prefix}_BASE_URL") or default_url).rstrip("/"),
            "api_key": os.environ.get(f"{prefix}_API_KEY", ""),
            "model": model,
            "free_tier": _provider_tier(name, declared_tier, model) == "free",
            "enabled_when": enabled_when,
            "disclosed_as": disclosed_as,
        })
    return built


LLM_PROVIDERS: list[dict] = _build_providers()


# --- COGS metering -----------------------------------------------------------
# Provider list prices in USD per 1M tokens, as comma-separated
# "model=input/output" pairs:
#   MODEL_PRICES="openai/gpt-4o=2.5/10,anthropic/claude-sonnet-5=3/15"
# A model with no price here is metered into a separate "cost:unpriced" bucket
# rather than as $0 -- a silent zero reads as "great margins" on the usage
# dashboard when what it actually means is "we don't know what this costs".
def _parse_model_prices(raw: str) -> dict[str, tuple[float, float]]:
    prices: dict[str, tuple[float, float]] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            model, rates = entry.split("=", 1)
            input_rate, output_rate = rates.split("/", 1)
            prices[model.strip()] = (float(input_rate), float(output_rate))
        except ValueError:
            print(f"Ignoring malformed MODEL_PRICES entry: {entry!r}")
    return prices


# A model served on a provider's free tier genuinely costs $0, so it is a known
# price, not a blind spot. Derived from the tier rather than hardcoded by model
# id: point the same model at a paid tier and it stops claiming to be free,
# falling back to "unpriced" until a real rate is configured for it.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    p["model"]: (0.0, 0.0) for p in LLM_PROVIDERS if p["free_tier"]
}
# An explicit rate always wins, so a free-tier model can still be priced at what
# it would cost once the free allowance runs out.
MODEL_PRICES.update(_parse_model_prices(os.environ.get("MODEL_PRICES", "")))

# Tavily list price per search, USD. Left at 0 (unset) searches are metered as
# unpriced, same reasoning as MODEL_PRICES above.
TAVILY_COST_PER_SEARCH_USD = float(os.environ.get("TAVILY_COST_PER_SEARCH_USD") or 0)

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")  # only needed for the lead sourcing agent
# The daily send cap is per-plan now (see plans.py); this env var survives as a
# deployment-wide OVERRIDE for self-hosters running a single tenant, who have no
# plans and no Stripe. It is None when unset, and that is the load-bearing part:
# a plain int default here would silently outrank every plan's limit, which is
# how "per-plan" quietly becomes global again. Only an explicitly set variable
# wins.
_DAILY_SEND_LIMIT_ENV = os.environ.get("DAILY_SEND_LIMIT", "").strip()
DAILY_SEND_LIMIT_OVERRIDE = int(_DAILY_SEND_LIMIT_ENV) if _DAILY_SEND_LIMIT_ENV else None
# Kept for the handful of read-only callers that just want a number to show
# (and for the trial default, which the pricing page prints as 25/day).
DAILY_SEND_LIMIT = DAILY_SEND_LIMIT_OVERRIDE if DAILY_SEND_LIMIT_OVERRIDE is not None else 25

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

# --- Stripe -------------------------------------------------------------------
# Deliberately optional: every one of these is empty on a self-hosted install and
# on the test suite, and billing.py treats "no secret key" as "checkout is not
# configured" rather than raising at import. Payments are a hosted-deployment
# concern, and a missing Stripe key must not stop someone running the agent for
# themselves.
#
# Test-mode keys (sk_test_/whsec_) work unchanged -- there is nothing in this
# code that distinguishes them from live keys, which is the point of test mode.
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
# Verifies that a webhook actually came from Stripe. Without it the endpoint
# would accept any POST claiming a payment succeeded, which is a free upgrade
# for anyone who can guess the URL -- so billing.py refuses to process events
# when this is unset rather than trusting them.
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
# Price ids per tier and billing period, created in the Stripe dashboard. Named
# {TIER}_{PERIOD} so plans.stripe_price_env can find them by convention.
STRIPE_PRICE_PILOT = os.environ.get("STRIPE_PRICE_PILOT", "")
STRIPE_PRICE_PILOT_YEARLY = os.environ.get("STRIPE_PRICE_PILOT_YEARLY", "")
STRIPE_PRICE_TEAM = os.environ.get("STRIPE_PRICE_TEAM", "")
STRIPE_PRICE_TEAM_YEARLY = os.environ.get("STRIPE_PRICE_TEAM_YEARLY", "")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SECRET_KEY = os.environ["SUPABASE_SECRET_KEY"]

# Name, Email, Company, Status, ThreadID, SentAt, EmailBody, LeadReason, EmailConfidence (first tab).
# EmailConfidence is a hard gate: rows the lead sourcing agent stamps
# "unverified" are never drafted and never sent until a human confirms the
# address and sets the cell to "verified" (clearing it is the owner-entered
# case -- a row the owner typed or imported themselves is their own contact).
# See sheets.campaign_readiness and the send-time re-checks in server.py /
# send_outreach.py. NeverBounce-style automated verification was removed
# 2026-07-18 (see TODOS.md); confirmation is manual by design.
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
