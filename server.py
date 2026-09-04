import contextlib
import concurrent.futures
import html
import hmac
import json
import os
import posixpath
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, quote

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "outreach-agent"))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from googleapiclient.errors import HttpError
from pydantic import BaseModel, Field

import accounts_db
import account_export
import agent
import auth
import billing
import bounces
import commitments
import commitments_db
import config
import dns_check
import dogfood_db
import drafts_db
import drive
import gmail
import google_auth
import leads
import outcomes_db
import notifications_db
import notify
import plans
import providers
import ratelimit
import reviews_db
import send_capacity_db
import send_outreach
import sheet_template
import sheets
import suppressions_db
import tracked_threads
import usage
import watch_replies
import worker_db

# FastAPI's auto-docs are off unless explicitly enabled. Left on, /docs, /redoc
# and /openapi.json hand every logged-in tenant an interactive map of all 33
# endpoints with request/response schemas. Per-endpoint auth still applies, so
# this is disclosure rather than a hole, but a multi-tenant app has no reason to
# publish its own blueprint. /docs also collided with the marketing site's
# "Docs" link, so customers clicking for help landed in Swagger UI.
# Set ENABLE_API_DOCS=1 locally when you want them.
_API_DOCS = os.environ.get("ENABLE_API_DOCS", "").strip().lower() in ("1", "true", "yes")

def report_degraded_controls():
    """Print anything that is not enforcing what the code around it assumes.

    All of these share a failure mode: they are invisible in normal operation. A
    missing column only surfaces when an account is already paused; an
    under-enforcing rate limiter is indistinguishable from a working one until
    someone is actually attacking a login. Announcing them at boot is the only
    point where nobody is depending on them yet.

    Production refuses to start when the contract is incomplete. Local
    development warns by default so a developer can run and repair migrations;
    SCHEMA_ENFORCEMENT=strict gives local runs the production behavior."""
    try:
        schema_warnings = accounts_db.check_schema()
    except Exception as e:  # a diagnostic failure is itself schema-unverified
        schema_warnings = [f"check skipped ({e})"]
    strict_schema = (
        os.environ.get("SCHEMA_ENFORCEMENT", "").strip().lower() == "strict"
        or os.environ.get("APP_ENV", "development").strip().lower() == "production"
    )
    if schema_warnings and strict_schema:
        joined = " | ".join(schema_warnings)
        raise RuntimeError(f"Database schema is incompatible with this release: {joined}")

    for label, probe in (("SCHEMA", lambda: schema_warnings),
                         ("RATELIMIT", ratelimit.enforcement_warnings)):
        try:
            for warning in probe():
                print(f"{label}: {warning}")
        except Exception as e:  # noqa: BLE001 - a diagnostic must never block boot
            print(f"{label}: check skipped ({e})")


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    # Lifespan rather than @app.on_event: on_event is deprecated as of the
    # FastAPI version pinned here and warns on import.
    report_degraded_controls()
    yield


app = FastAPI(
    lifespan=_lifespan,
    docs_url="/docs" if _API_DOCS else None,
    redoc_url="/redoc" if _API_DOCS else None,
    openapi_url="/openapi.json" if _API_DOCS else None,
)

STATIC_DIR = ROOT / "static"

PUBLIC_PATHS = {"/login", "/signup", "/logout", "/auth/google/login", "/auth/google/callback"}

# Reachable logged out on purpose. These are linked from the marketing footer,
# so a prospect evaluating the product reads them before they have an account.
# Google's OAuth verification also requires a publicly resolvable privacy policy.
LEGAL_PAGES = {
    "/privacy": "privacy.html",
    "/terms": "terms.html",
    "/acceptable-use": "acceptable-use.html",
    "/dpa": "dpa.html",
    "/security": "security.html",
}
PUBLIC_PATHS |= set(LEGAL_PAGES)
# The stylesheet those pages share. Exact-match, so unlike a prefix rule it
# cannot be walked ("/static/legal.css/../outreach.html" is simply not equal).
# Without it a logged-out visitor gets the markup and no styling.
PUBLIC_PATHS.add("/static/legal.css")
# Shared light product tokens are also used by the public login and 404 pages.
# Keep this exact-match exemption narrow, like legal.css above; logged-out
# visitors should not be able to walk the rest of /static/.
PUBLIC_PATHS.add("/static/product-theme.css")

# The getting-started guide is the product documentation the marketing site
# links to as "Docs", so a prospect has to be able to read it before signing up.
PUBLIC_PATHS.add("/getting-started")

# The starter files contain only reserved example rows and are linked from the
# public getting-started guide. They do not expose account data, so requiring a
# session here makes the public guide's download links fail for logged-out
# visitors.
PUBLIC_PATHS.update({
    "/api/lead-sheet-template.xlsx",
    "/api/lead-sheet-template.csv",
})

# The one-click opt-out endpoint. It MUST be reachable with no session: the
# person clicking it is an email recipient, never a logged-in user of this app.
# Authenticity comes from the signed token in the URL, not from a cookie.
PUBLIC_PATHS.add("/unsubscribe")

# The Stripe webhook. Unauthenticated by necessity -- the caller is Stripe, which
# has no session and never will. Authenticity comes from the HMAC signature on
# the request body, verified in billing.verify_webhook, which refuses outright
# when no signing secret is configured rather than falling back to trusting the
# caller. Nothing else about this endpoint may rely on the session.
PUBLIC_PATHS.add("/api/billing/webhook")

# The external uptime monitor cannot carry a Sendkeep session cookie. The route
# is public at the middleware layer but still requires WORKER_HEALTH_TOKEN in a
# request header, and it returns only aggregate liveness data.
WORKER_HEALTH_PATH = "/internal/health/worker"
PUBLIC_PATHS.add(WORKER_HEALTH_PATH)

# Only these known application pages should turn a missing session into a
# login redirect. Unknown browser paths should reach the 404 handler so a
# developer can distinguish a typo or stale link from an authentication issue.
PROTECTED_PAGE_PATHS = {"/outreach", "/leads", "/settings"}
if _API_DOCS:
    PROTECTED_PAGE_PATHS.update({"/docs", "/redoc", "/openapi.json"})

# Prefixes served without auth. /static/landing/ is the marketing bundle;
# /static/app/assets/ holds the compiled React chunks the guide needs (it shares
# them with Settings). Shipping compiled frontend code to anonymous visitors is
# how every SPA works and leaks nothing: the bundles carry UI code and API paths,
# and every one of those APIs is independently auth-gated. No page template is
# reachable this way, only assets/.
_PUBLIC_ASSET_PREFIXES = ("/static/landing/", "/static/app/assets/")


def _is_public_asset(path: str) -> bool:
    # Normalize first: without this, "/static/landing/../outreach.html" starts
    # with the prefix, skips auth, and StaticFiles then resolves the "../" into an
    # auth-gated page template. normpath collapses the traversal so only paths
    # that genuinely live under a public prefix get the bypass.
    normalized = posixpath.normpath(path)
    return normalized.startswith(_PUBLIC_ASSET_PREFIXES)


def _login_next_target(request: Request) -> str:
    """Return the same-site path and query that the user originally opened."""
    target = request.url.path
    if request.url.query:
        target += f"?{request.url.query}"
    return target


@app.middleware("http")
async def require_auth(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS or _is_public_asset(request.url.path):
        return await call_next(request)
    account_id = auth.verify_session_token(request.cookies.get(auth.COOKIE_NAME))
    if not account_id:
        if request.url.path == "/":
            # Logged-out visitors see the marketing landing page; the app
            # home (agents) stays behind auth.
            response = FileResponse(STATIC_DIR / "landing" / "index.html")
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Vary"] = "Cookie"
            return response
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if request.url.path in PROTECTED_PAGE_PATHS or request.url.path.startswith("/static/"):
            # Encode the nested target so its own query string is not parsed as
            # parameters of /login. Keep slashes readable for existing simple
            # /login?next=/settings links while preserving tabs and filters.
            next_target = quote(_login_next_target(request), safe="/")
            response = RedirectResponse(f"/login?next={next_target}", status_code=303)
            response.headers["Cache-Control"] = "private, no-store"
            return response
        return await call_next(request)
    request.state.account_id = account_id
    response = await call_next(request)
    if request.url.path == "/":
        # The same URL serves public marketing when logged out and the agent
        # dashboard when logged in. Shared proxies and browser caches must not
        # reuse one representation for the other.
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Vary"] = "Cookie"
    return response


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    # Browsers get a page; API clients keep the JSON body they parse. Without
    # the split, a mistyped URL showed a raw {"detail":"Not Found"} to a person.
    wants_html = "text/html" in request.headers.get("accept", "")
    if request.url.path.startswith("/api/") or not wants_html:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return FileResponse(STATIC_DIR / "404.html", status_code=404)


@app.exception_handler(RuntimeError)
async def runtime_error_handler(request: Request, exc: RuntimeError):
    # Modules raise RuntimeError for "this account isn't set up yet" states
    # (no Gmail connected, no sheet chosen, no calendar link).
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.exception_handler(send_outreach.SendFailure)
async def send_failure_handler(request: Request, exc: send_outreach.SendFailure):
    status = {
        "send_temporarily_blocked": 503,
        "capacity_unavailable": 503,
        "send_uncertain": 502,
    }.get(exc.code, 409)
    return JSONResponse(
        {"detail": exc.detail, "code": exc.code, "retryable": exc.retryable},
        status_code=status,
    )


@app.exception_handler(HttpError)
async def google_api_error_handler(request: Request, exc: HttpError):
    # A wrong/typo'd Sheet ID or a Gmail thread that no longer exists surfaces
    # here as a raw Google API error — translate it instead of 500ing.
    if exc.status_code == 404:
        detail = "Google couldn't find that — double check the Sheet ID in Settings."
    elif exc.status_code == 403:
        detail = "Google denied access — make sure the connected account can open that Sheet."
    else:
        detail = f"Google API error ({exc.status_code}). Try again in a moment."
    return JSONResponse({"detail": detail}, status_code=409)


def _account(request: Request) -> dict:
    """Loads the logged-in account's full row. 401s if it was deleted."""
    account = accounts_db.get_account(request.state.account_id)
    if not account:
        raise HTTPException(status_code=401, detail="Account no longer exists")
    return account


ACCOUNT_MONITORING_STALE_AFTER_SECONDS = 900


def _assert_send_capacity(account):
    """Fail closed when the rolling per-inbox send cap is exhausted."""
    limit = plans.daily_send_limit_for(account)
    sent = drafts_db.count_sent_last_24_hours(account["id"])
    if sent >= limit:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This inbox has reached its {limit}-send rolling 24-hour safety cap. "
                "Try again after it resets."
            ),
        )


def _account_monitoring(account: dict) -> dict:
    """Project the worker's per-account heartbeat into an actionable UI state.

    A Google OAuth token proves authorization, not that Gmail Sent monitoring is
    currently working. The worker records ``worker_heartbeat_at`` and
    ``last_error`` for exactly this distinction.
    """
    if not google_auth.capability_status(account)["monitor"]:
        return {
            "status": "not_connected",
            "message": "Connect Gmail to start Gmail Sent monitoring.",
            "last_checked_at": None,
        }

    last_error = (account.get("last_error") or "").strip()
    heartbeat = account.get("worker_heartbeat_at")
    if last_error:
        return {
            "status": "attention",
            "message": last_error,
            "last_checked_at": heartbeat,
        }

    if not heartbeat:
        return {
            "status": "waiting",
            "message": "Gmail is connected. Waiting for the first worker check.",
            "last_checked_at": None,
        }

    try:
        parsed = datetime.fromisoformat(str(heartbeat).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age_seconds = max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
    except (TypeError, ValueError):
        age_seconds = ACCOUNT_MONITORING_STALE_AFTER_SECONDS + 1

    if age_seconds > ACCOUNT_MONITORING_STALE_AFTER_SECONDS:
        return {
            "status": "stale",
            "message": "Gmail Sent monitoring is delayed. Check the worker or reconnect Gmail.",
            "last_checked_at": heartbeat,
        }

    return {
        "status": "healthy",
        "message": "Gmail Sent monitoring is active.",
        "last_checked_at": heartbeat,
    }


# Set SECURE_COOKIES=1 (or true/yes) when serving over HTTPS (e.g. behind the
# cloudflared tunnel) so session cookies are never sent over plain http.
# Parsed explicitly -- bool(os.environ.get(...)) would treat "0" and "false"
# as ON, and the resulting Secure cookie over plain http fails as a silent
# login loop with nothing in any log. Read at call time so tests (and a
# restarted tunnel setup) see env changes without a module reload.
def _secure_cookies() -> bool:
    return os.environ.get("SECURE_COOKIES", "").strip().lower() in ("1", "true", "yes")


def _set_session_cookie(response, account_id: str):
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.create_session_token(account_id),
        httponly=True,
        samesite="lax",
        secure=_secure_cookies(),
        max_age=auth.SESSION_TTL_SECONDS,
    )
    return response


# --- Pages -------------------------------------------------------------------

@app.get("/")
def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Serve the existing brand mark at the conventional browser URL."""
    return FileResponse(
        STATIC_DIR / "landing" / "favicon.svg",
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/outreach")
def outreach_page():
    return FileResponse(STATIC_DIR / "outreach.html")


@app.get("/leads")
def leads_page():
    return FileResponse(STATIC_DIR / "leads.html")


@app.get("/settings")
def settings_page():
    # React build (app/, output to static/app/) -- first page converted from
    # the old hand-written static/settings.html, which stays on disk
    # unreferenced as a rollback/diff reference during the migration.
    return FileResponse(STATIC_DIR / "app/settings.html")


def _legal_page(filename: str):
    return lambda: FileResponse(STATIC_DIR / filename)


for _path, _file in LEGAL_PAGES.items():
    # Registered in a loop rather than five near-identical handlers. Each is a
    # plain static page; the middleware already lets them through logged out.
    app.get(_path)(_legal_page(_file))


@app.get("/getting-started")
def getting_started_page():
    # React build (app/, output to static/app/). Auth-gated like every other
    # app page: its bundle lives under /static/app/, which the middleware only
    # exempts for /static/landing/, so serving it logged-out would render a
    # blank page. Steps 2-8 all require being signed in anyway.
    return FileResponse(STATIC_DIR / "app/getting-started.html")


@app.get("/login")
def login_page():
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/signup")
def signup_page():
    # No separate signup flow anymore -- creating an account and signing in
    # are the same action (see M2, PLAN-WEEK-2026-08-05.md). Kept as a route
    # rather than deleted because the marketing site's "Connect Gmail" CTA and
    # any bookmarked/shared links point here.
    return RedirectResponse("/auth/google/login")


# --- Accounts & sessions -------------------------------------------------------

@app.post("/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE_NAME)
    return response


NEXT_COOKIE_NAME = "post_login_next"
OAUTH_STATE_COOKIE = "oauth_state"
OAUTH_STATE_LIMIT = 4
_SAFE_NEXT_RE = re.compile(r"^/[^/\\]")


def _safe_next(value: str) -> str | None:
    """Same-site absolute paths only. Rejects "//evil.com", "https://…", and
    "\\evil.com" so a crafted /login?next=… link can't redirect a freshly
    authenticated user off to a phishing page. Mirrors the client-side check
    in static/login.html, which is defense in depth, not the authority --
    this is the check that actually gates the cookie."""
    return value if value and _SAFE_NEXT_RE.match(value) else None


def _oauth_state_values(raw: str) -> list[str]:
    return [value for value in (raw or "").split("|") if auth.verify_oauth_state(value)]


def _store_oauth_states(response: Response, states: list[str]):
    states = states[-OAUTH_STATE_LIMIT:]
    if states:
        response.set_cookie(
            OAUTH_STATE_COOKIE, "|".join(states), httponly=True,
            samesite="lax", secure=_secure_cookies(), max_age=600,
        )
    else:
        response.delete_cookie(OAUTH_STATE_COOKIE)


@app.get("/auth/google/login")
def google_login_start(next: str = "", request: Request = None):
    state = auth.create_oauth_state()
    response = RedirectResponse(google_auth.build_login_auth_url(state))
    previous = _oauth_state_values(request.cookies.get(OAUTH_STATE_COOKIE, "")) if request else []
    _store_oauth_states(response, [*previous, state])
    safe_next = _safe_next(next)
    if safe_next:
        # Short-lived and HttpOnly: this only needs to survive the round trip
        # to Google and back, and nothing on the page needs to read it. Kept
        # out of the OAuth `state` param on purpose -- state is a fixed-shape
        # CSRF nonce (see auth.create_oauth_state), and widening it to also
        # carry a path would make it easier to confuse for the differently
        # shaped session token it deliberately can't be mistaken for.
        response.set_cookie(
            NEXT_COOKIE_NAME, safe_next,
            httponly=True, samesite="lax", secure=_secure_cookies(), max_age=600,
        )
    return response


@app.get("/auth/google/callback")
def google_login_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    if error:
        response = RedirectResponse(f"/login?google_error={quote(error[:80], safe='')}")
        response.delete_cookie(OAUTH_STATE_COOKIE)
        response.delete_cookie(NEXT_COOKIE_NAME)
        return response
    cookie_states = _oauth_state_values(request.cookies.get(OAUTH_STATE_COOKIE, ""))
    if (
        not auth.verify_oauth_state(state)
        or not any(hmac.compare_digest(candidate, state) for candidate in cookie_states)
    ):
        # A callback URL is single-use and short-lived. Browser history, a
        # restored tab, or a second sign-in attempt can therefore return here
        # with a dead state. Keep rejecting it, but do not strand a person on a
        # raw API error page. The authorization code is deliberately discarded;
        # only a fresh trip through /auth/google/login may continue.
        safe_next = _safe_next(request.cookies.get(NEXT_COOKIE_NAME, ""))
        location = "/login?google_error=oauth_state_expired"
        if safe_next:
            location += f"&next={quote(safe_next, safe='')}"
        response = RedirectResponse(location, status_code=303)
        _store_oauth_states(response, cookie_states)
        response.delete_cookie(NEXT_COOKIE_NAME)
        response.headers["Cache-Control"] = "private, no-store"
        return response
    try:
        identity = google_auth.exchange_login_code(code)
    except Exception:
        response = RedirectResponse("/login?google_error=auth_failed")
        response.delete_cookie(OAUTH_STATE_COOKIE)
        response.delete_cookie(NEXT_COOKIE_NAME)
        return response

    try:
        account = accounts_db.link_or_create_google_account(
            identity["email"].lower(), identity["google_id"]
        )
    except accounts_db.GoogleIdentityConflict:
        response = RedirectResponse("/login?google_error=identity_conflict", status_code=303)
        _store_oauth_states(response, [candidate for candidate in cookie_states if candidate != state])
        response.delete_cookie(NEXT_COOKIE_NAME)
        return response
    # The core promise desk only needs Gmail. Sheets and a calendar link belong
    # to the optional first-touch lane and must not force a Gmail-only user
    # through setup before they can see their queue.
    onboarded = google_auth.capability_status(account)["monitor"]
    safe_next = _safe_next(request.cookies.get(NEXT_COOKIE_NAME, ""))
    # Use an unambiguous app URL after onboarding. `/` has two representations
    # (marketing when logged out, dashboard when logged in), so returning there
    # makes stale intermediary caches look like a failed login.
    target = safe_next or ("/outreach" if onboarded else "/settings")
    response = _set_session_cookie(RedirectResponse(target), account["id"])
    response.delete_cookie(NEXT_COOKIE_NAME)
    _store_oauth_states(response, [candidate for candidate in cookie_states if candidate != state])
    return response


@app.get("/api/me")
def me(request: Request):
    account = _account(request)
    google_capabilities = google_auth.capability_status(account)
    return {
        "email": account["email"],
        "googleConnected": google_capabilities["monitor"],
        "googleCapabilities": google_capabilities,
        "autoSendEnabled": config.AUTO_SEND_ENABLED,
        "settings": {key: account.get(key) for key in accounts_db.EDITABLE_SETTINGS},
        "onboarded": google_capabilities["monitor"],
        # Same blockers the campaign preview gates sending on (see
        # agent.account_send_blockers) -- surfaced here too so Settings can show
        # them next to the field that fixes each one, instead of only at send time.
        "sendBlockers": agent.account_send_blockers(account),
        "monitoring": _account_monitoring(account),
    }


class SettingsBody(BaseModel):
    sender_name: str | None = None
    sender_company: str | None = None
    # "One sentence, used in every email" per the Settings UI copy -- 500 is
    # generous for that while bounding agent._is_near_duplicate_of_default's
    # difflib comparison, which has no cap otherwise (eng review 2026-08-05:
    # measured ~0.2s at 100KB of adversarial input on an otherwise-uncapped
    # field; low severity on its own, but free to close outright).
    meeting_purpose: str | None = Field(default=None, max_length=500)
    custom_instructions: str | None = None
    calendar_booking_link: str | None = None
    notify_email: str | None = None
    google_sheet_id: str | None = None
    outreach_send_mode: Literal["manual", "auto"] | None = None
    follow_up_delay_days: int | None = Field(default=None, ge=1, le=14)


@app.put("/api/settings")
def update_settings(request: Request, payload: SettingsBody):
    if payload.outreach_send_mode == "auto" and not config.AUTO_SEND_ENABLED:
        raise HTTPException(
            status_code=409,
            detail=(
                "Auto-send is disabled in safety-first mode. Keep manual review "
                "on, or enable AUTO_SEND_ENABLED for a controlled deployment."
            ),
        )
    account = accounts_db.update_settings(
        request.state.account_id, payload.model_dump(exclude_none=True)
    )
    return {key: account.get(key) for key in accounts_db.EDITABLE_SETTINGS}


@app.post("/api/alerts/test")
def send_test_alert(request: Request):
    """Queue and attempt one privacy-safe alert to the signed-in email."""
    account = _account(request)
    if not (os.environ.get("TRANSACTIONAL_EMAIL_API_KEY", "").strip()
            and os.environ.get("TRANSACTIONAL_EMAIL_FROM", "").strip()):
        raise HTTPException(
            status_code=503,
            detail="Transactional email is not configured on this deployment.",
        )
    event_id = notifications_db.queue_test(account["id"])
    notify.dispatch_event(account_id=account["id"], event_id=event_id)
    event = notifications_db.get_event(account["id"], event_id) or {}
    status = event.get("status")
    if status == "delivered":
        return {"ok": True, "recipient": account["email"]}
    if event.get("last_error_code") == "provider_not_configured":
        raise HTTPException(
            status_code=503,
            detail="Transactional email is not configured on this deployment.",
        )
    if status == "failed":
        raise HTTPException(
            status_code=502,
            detail="The email provider rejected the test alert. Check the provider configuration before retrying.",
        )
    if status == "delivery_uncertain":
        raise HTTPException(
            status_code=502,
            detail="The test alert result is uncertain and cannot be retried safely. Check the email provider activity.",
        )
    if status == "cancelled":
        raise HTTPException(
            status_code=409,
            detail="The test alert was cancelled before delivery.",
        )
    raise HTTPException(
        status_code=502,
        detail="The test alert is queued for retry; the email provider did not confirm delivery.",
    )


@app.get("/api/lead-sheet-template.xlsx")
def lead_sheet_template():
    """The starter workbook linked from Settings: the exact nine-label header
    row, sample rows covering each Status, and a notes tab. Built per request
    rather than served from disk so it can never drift from EXPECTED_HEADER."""
    return Response(
        content=sheet_template.build_xlsx(),
        media_type=sheet_template.MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{sheet_template.FILENAME}"'
        },
    )


@app.get("/api/lead-sheet-template.csv")
def lead_sheet_template_csv():
    """Same rows as the .xlsx, for people who keep leads as CSV. Still a
    starting point for a Google Sheet -- nothing here parses CSV as input."""
    return Response(
        content=sheet_template.build_csv(),
        media_type=sheet_template.CSV_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{sheet_template.CSV_FILENAME}"'
        },
    )


@app.get("/api/usage")
def get_usage(request: Request):
    return accounts_db.usage_summary(request.state.account_id)


@app.get("/api/deliverability")
def deliverability(request: Request):
    """Read-only SPF/DKIM/DMARC report for the domain this account sends from.
    The optional first-touch send path uses the same concrete result as a
    fail-closed safety gate; reply monitoring does not depend on it."""
    account = _account(request)
    return send_outreach.domain_safety(account)


@app.get("/api/llm-providers")
def llm_providers(request: Request):
    """Which language model endpoints this deployment is actually using, and
    which of them are allowed to draft replies.

    The DPA claims its sub-processor list is "derived from the code, not from
    memory", and the privacy policy tells customers to ask which endpoint their
    account is on before trusting reply drafting with a prospect's words. This is
    the answer to both, read live off the routing policy instead of a doc someone
    has to remember to update. No key material, only names and tiers."""
    _account(request)
    return {"providers": providers.describe()}


@app.exception_handler(plans.QuotaExceeded)
async def quota_exceeded_handler(request: Request, exc: plans.QuotaExceeded):
    """402 Payment Required, which is what this literally is.

    Registered app-wide rather than caught per-endpoint because the gate lives
    on the operation (send_outreach, leads), not on the routes -- so any
    endpoint that reaches one of those inherits the right status without having
    to remember to. The plan and bucket travel with it so the UI can offer the
    upgrade instead of just reporting a failure."""
    return JSONResponse(
        {
            "detail": str(exc),
            "plan": exc.plan_name,
            "bucket": exc.bucket,
            "used": exc.used,
            "limit": exc.limit,
        },
        status_code=402,
    )


@app.get("/api/plan")
def get_plan(request: Request):
    """This account's plan, its limits, and how much of them is left.

    Read from the account row rather than hardcoded, which is what it used to
    be: one dict naming a tier every account was assumed to be on, next to a
    daily limit that was the same global constant for everybody."""
    account = _account(request)
    summary = plans.describe(account)
    _, drafts_left, drafts_used = plans.headroom(account, usage.UNIT_DRAFT_EMAIL)
    _, leads_left, leads_used = plans.headroom(account, usage.UNIT_LEAD_SOURCED)
    return {
        **summary,
        "usage": {
            "drafts_used": drafts_used,
            "drafts_remaining": drafts_left,
            "leads_used": leads_used,
            "leads_remaining": leads_left,
        },
        "checkout_available": billing.configured(),
    }


class CheckoutRequest(BaseModel):
    plan: str
    yearly: bool = False


@app.post("/api/billing/checkout")
def start_checkout(request: Request, payload: CheckoutRequest):
    """Returns a Stripe Checkout URL for the requested tier.

    The account is taken from the session, never from the request body -- it is
    what billing.create_checkout_session stamps onto the session as
    client_reference_id, and it is the only thing the webhook will trust when
    deciding whose plan to change."""
    account = _account(request)
    if payload.plan not in plans.PAID_PLANS:
        raise HTTPException(
            status_code=400,
            detail=f"{payload.plan!r} is not a paid plan. Choose one of: {', '.join(plans.PAID_PLANS)}.",
        )
    try:
        return {"url": billing.create_checkout_session(account, payload.plan, payload.yearly)}
    except billing.BillingNotConfigured as e:
        # 503, not 500: the deployment has not been given Stripe keys. Nothing
        # is broken and nothing the customer did caused it.
        raise HTTPException(status_code=503, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/billing/webhook")
async def stripe_webhook(request: Request):
    """Stripe tells us a payment settled or a subscription ended.

    This is the ONLY place a plan is granted. It is public (Stripe has no
    session), so the signature check is the whole of its security -- see
    billing.verify_webhook. The raw body is passed through unparsed because the
    signature covers exact bytes; re-serializing a parsed dict would reorder
    keys and fail verification on genuine events.

    A rejected signature answers 400 and changes nothing. An event we do not
    handle answers 200, so Stripe stops retrying something we are deliberately
    ignoring rather than queueing it for days."""
    payload = await request.body()
    try:
        event = billing.verify_webhook(payload, request.headers.get("stripe-signature"))
    except billing.WebhookRejected as e:
        print(f"Rejected Stripe webhook: {e}")
        raise HTTPException(status_code=400, detail=str(e))

    change = billing.plan_change_from_event(event)
    if not change:
        return {"handled": False}

    outcome = accounts_db.apply_billing_event(change)
    if outcome != "applied":
        return {"handled": True, "applied": False, "reason": outcome}
    print(f"Stripe {event.get('type')}: account {change['account_id']} is now on {change['plan']}")
    return {"handled": True, "applied": True, "plan": change["plan"]}


def _google_error_reason(exc: HttpError) -> str:
    details = exc.error_details
    if details and isinstance(details, list) and details[0].get("reason"):
        return details[0]["reason"]
    return ""


@app.get("/api/google/sheets")
def list_google_sheets(request: Request):
    account = _account(request)
    try:
        return drive.list_spreadsheets(account)
    except HttpError as e:
        reason = _google_error_reason(e)
        if reason == "accessNotConfigured":
            # The app's Google Cloud project hasn't enabled the Drive API —
            # an app-wide setup gap, not something any one account can fix.
            raise HTTPException(
                status_code=409,
                detail="The Google Drive API isn't enabled for this app yet — an admin needs to enable it in Google Cloud Console.",
            )
        if e.status_code in (401, 403):
            # Actual missing-scope case: the stored token predates the
            # Drive scope being added — re-consent grants it.
            raise HTTPException(
                status_code=409,
                detail="Reconnect Google (Settings → Reconnect) to grant access to your Sheets list.",
            )
        raise


# --- Google OAuth (per-account "Connect your Google account") ------------------

OAUTH_STATE_TTL_SECONDS = 600


@app.get("/api/google/connect")
def google_connect(request: Request, capability: Literal["monitor", "send", "sheets"] = "monitor"):
    if capability == "monitor":
        account = _account(request)
        if not google_auth.capability_status(account)["monitor"]:
            precision = commitments_db.onboarding_precision()
            if precision["stop_onboarding"]:
                rate = round(float(precision["false_positive_rate"] or 0) * 100)
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "New Gmail monitoring connections are temporarily paused: "
                        f"{rate}% of reviewed promise flags were marked incorrect. "
                        "Fix and revalidate extraction precision before onboarding more inboxes."
                    ),
                )
    state = auth.create_google_grant_state(
        request.state.account_id, capability, OAUTH_STATE_TTL_SECONDS
    )
    return RedirectResponse(google_auth.build_auth_url(state, capability))


@app.get("/api/google/callback")
def google_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    if error:
        return RedirectResponse(f"/settings?google_error={error}")
    # The signed state must match the logged-in account (CSRF protection).
    grant = auth.verify_google_grant_state(state)
    if not grant or grant[0] != request.state.account_id:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    state_account_id, capability = grant
    account = accounts_db.get_account(state_account_id)
    existing_token = accounts_db.get_google_token(account)
    try:
        token_json = google_auth.exchange_code(code, capability)
        merged_token = google_auth.merge_token_json(existing_token, token_json)
    except google_auth.GoogleIdentityMismatch as e:
        raise HTTPException(
            status_code=409,
            detail="This Google grant belongs to a different Gmail account. Reconnect the same mailbox in Settings.",
        ) from e
    accounts_db.set_google_token(state_account_id, merged_token)
    return RedirectResponse(f"/settings?connected={capability}")


@app.post("/api/google/disconnect")
def google_disconnect(request: Request):
    accounts_db.set_google_token(request.state.account_id, None)
    return {"ok": True}


@app.get("/api/account/export")
def export_account(request: Request):
    """Download the account's Sendkeep records without OAuth credentials.

    This is intentionally separate from Google disconnect: disconnecting
    revokes Sendkeep's stored access, while this endpoint gives the operator a
    portable copy of the queue and audit history before they leave.
    """
    account = _account(request)
    payload = account_export.build(request.state.account_id, account)
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="sendkeep-account-export.json"',
            "Cache-Control": "private, no-store",
        },
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --- Background jobs -------------------------------------------------------
# Long-running agent actions (send a batch, poll for replies) run on a
# background thread so the request returns immediately; the frontend polls
# /api/jobs/{id} for the result instead of blocking on the original request.

JOBS: dict[str, dict] = {}
JOBS_TTL_SECONDS = 3600
JOBS_MAX_ENTRIES = 500
_jobs_guard = threading.Lock()
_job_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="sendkeep-job"
)


def _cleanup_jobs(now=None):
    """Bound terminal job memory while never deleting work still running."""
    now = time.monotonic() if now is None else now
    with _jobs_guard:
        expired = [
            job_id for job_id, job in JOBS.items()
            if job.get("status") != "running"
            and now - job.get("finished_at", now) >= JOBS_TTL_SECONDS
        ]
        for job_id in expired:
            JOBS.pop(job_id, None)
        terminal = sorted(
            (
                (job.get("finished_at", 0), job_id)
                for job_id, job in JOBS.items() if job.get("status") != "running"
            )
        )
        excess = max(0, len(JOBS) - JOBS_MAX_ENTRIES)
        for _, job_id in terminal[:excess]:
            JOBS.pop(job_id, None)

# Accounts with a send batch currently in flight. Guards against the same
# account double-clicking "Send pending batch" or triggering it from two open
# tabs before the first job finishes -- without this, both jobs read the same
# pending rows and every contact gets emailed twice. Single-process in-memory
# state is sufficient because this server runs as one uvicorn process (see
# TODOS.md for the multi-process case).
_send_lock_guard = threading.Lock()
_accounts_sending: set[str] = set()


def _run_job(job_id, account_id, fn):
    try:
        result = usage.run_as(account_id, fn)
        job = {"account_id": account_id, "status": "done", "result": result, "error": None}
    except RuntimeError as e:
        # RuntimeError is the app's convention for hand-written, user-safe
        # messages (see runtime_error_handler) -- surface it to the client.
        job = {"account_id": account_id, "status": "error", "result": None, "error": str(e)}
    except Exception as e:
        # Anything else is unexpected: a raw str(e) here would ship internal
        # details straight to the frontend (Google API JSON payloads, Supabase
        # errors, KeyErrors). Log the real cause server-side, return a generic
        # message.
        print(f"Job {job_id} (account {account_id}) failed unexpectedly: {e!r}")
        job = {
            "account_id": account_id, "status": "error", "result": None,
            "error": "Something went wrong on our end. Please try again.",
        }
    job["finished_at"] = time.monotonic()
    with _jobs_guard:
        JOBS[job_id] = job


def start_job(account_id, fn) -> str:
    _cleanup_jobs()
    job_id = uuid.uuid4().hex
    with _jobs_guard:
        running = sum(job.get("status") == "running" for job in JOBS.values())
        if len(JOBS) >= JOBS_MAX_ENTRIES and running >= JOBS_MAX_ENTRIES:
            raise RuntimeError("The job queue is full. Try again in a moment.")
        JOBS[job_id] = {
            "account_id": account_id, "status": "running", "result": None,
            "error": None, "created_at": time.monotonic(),
        }
    _job_executor.submit(_run_job, job_id, account_id, fn)
    return job_id


@app.get("/api/jobs/{job_id}")
def get_job(request: Request, job_id: str):
    _cleanup_jobs()
    job = JOBS.get(job_id)
    # A job is only visible to the account that started it.
    if not job or job["account_id"] != request.state.account_id:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        k: v for k, v in job.items()
        if k not in {"account_id", "created_at", "finished_at"}
    }


# --- Outreach agent -----------------------------------------------------------

@app.get("/api/outreach/campaigns")
def list_campaigns(request: Request):
    account = _account(request)
    # One account-scoped queue read drives every Threads-row action. Do not ask
    # Gmail once per row merely to decide whether a button should be visible.
    try:
        actionable_threads = {
            review.get("thread_id")
            for review in reviews_db.list_pending_reviews(account["id"])
            if review.get("status") == "pending"
            and review.get("kind", "reply") != "follow_up"
            and review.get("thread_id")
        }
    except Exception as exc:
        print(f"Could not resolve actionable replies for {account['id']}: {exc}")
        actionable_threads = set()
    if not (account.get("google_sheet_id") or "").strip():
        # The promise desk does not require a Sheet. The worker's durable tracker
        # is the source for threads discovered from Gmail Sent or previously
        # registered by Sendkeep.
        try:
            tracked = tracked_threads.list_active(account["id"])
        except Exception as e:
            print(f"Could not list durable Gmail threads for {account['id']}: {e}")
            raise HTTPException(
                status_code=503,
                detail="Gmail thread monitoring is temporarily unavailable. Refresh to try again.",
            ) from e
        durable_campaigns = [
            {
                "row": None,
                "name": row.get("name") or "",
                "email": row.get("email") or "",
                "company": row.get("company") or "",
                "status": "Replied" if reviews_db.latest_reply_for_thread(
                    account["id"], row.get("thread_id")
                ) else "Sent",
                "sentAt": "",
                "verification": "",
                "hasThread": True,
                "trackedId": row.get("id"),
                "replyActionable": row.get("thread_id") in actionable_threads,
                "source": row.get("source") or "gmail",
                # These are safe operational hints, not provider identifiers.
                # The UI can say whether the worker is keeping watch without
                # exposing a Gmail thread ID to the browser.
                "lastCheckedAt": row.get("last_checked_at") or "",
                "monitoring": "error" if row.get("last_error") else "ok",
            }
            for row in tracked
            if (row.get("thread_id") or "").strip()
        ]
        if durable_campaigns:
            return durable_campaigns
        # Keep the legacy Sheet path as a compatibility fallback for accounts
        # created before durable tracking was enabled. A Gmail-only account has
        # no Sheet and will simply return an empty list from this guarded read;
        # an older Sheet-backed account can still render its sent-thread state.
        try:
            rows = sheets.get_all_rows(account)
        except Exception:
            return []
        return [
            {
                "row": row_index,
                "name": row[sheets.COL_NAME],
                "email": row[sheets.COL_EMAIL],
                "company": row[sheets.COL_COMPANY],
                "status": row[sheets.COL_STATUS] or "Pending",
                "sentAt": row[sheets.COL_SENT_AT],
                "verification": row[sheets.COL_EMAIL_CONFIDENCE] or "unverified",
                "hasThread": bool(row[sheets.COL_THREAD_ID].strip()),
                "replyActionable": row[sheets.COL_THREAD_ID].strip() in actionable_threads,
            }
            for row_index, row in rows
            if row[sheets.COL_EMAIL].strip()
        ]
    rows = sheets.get_all_rows(account)
    try:
        tracked_ids = {
            row.get("thread_id"): row.get("id")
            for row in tracked_threads.list_active(account["id"])
            if row.get("thread_id") and row.get("id")
        }
    except Exception as e:
        print(f"Could not add durable thread actions for {account['id']}: {e}")
        tracked_ids = {}
    return [
        {
            "row": row_index,
            "name": row[sheets.COL_NAME],
            "email": row[sheets.COL_EMAIL],
            "company": row[sheets.COL_COMPANY],
            "status": row[sheets.COL_STATUS] or "Pending",
            "sentAt": row[sheets.COL_SENT_AT],
            "verification": row[sheets.COL_EMAIL_CONFIDENCE] or "unverified",
            # The browser needs to know whether per-campaign reply checking is
            # available, but must not receive the provider's thread identifier.
            "hasThread": bool(row[sheets.COL_THREAD_ID].strip()),
            "trackedId": tracked_ids.get(row[sheets.COL_THREAD_ID].strip()),
            "replyActionable": row[sheets.COL_THREAD_ID].strip() in actionable_threads,
        }
        for row_index, row in rows
        if row[sheets.COL_EMAIL].strip()
    ]


@app.delete("/api/outreach/threads/{tracked_id}")
def remove_tracked_thread(request: Request, tracked_id: int):
    """Delete active contact work and matching Sheet rows, never Gmail."""
    account = _account(request)
    tracked = tracked_threads.get_by_id(account["id"], tracked_id)
    if not tracked or tracked.get("status") == "removed":
        # Account scoping intentionally makes a foreign id indistinguishable
        # from a missing one.
        raise HTTPException(status_code=404, detail="Monitored thread not found.")
    try:
        blocker = tracked_threads.send_blocker(account["id"], tracked.get("thread_id"))
    except Exception as exc:
        print(f"Could not verify send state before deleting contact {tracked_id}: {exc}")
        raise HTTPException(
            status_code=503,
            detail="Sendkeep could not verify whether this contact has an in-progress send. Nothing was deleted.",
        ) from exc
    if blocker == "send_uncertain":
        raise HTTPException(
            status_code=409,
            detail="This contact has a reply whose Gmail delivery is still uncertain. Reconcile it before deleting the contact.",
        )
    if blocker:
        raise HTTPException(
            status_code=409,
            detail="A reply or follow-up is currently being sent. Wait for it to finish before deleting the contact.",
        )
    if not tracked_threads.begin_delete(account["id"], tracked_id):
        raise HTTPException(status_code=409, detail="This contact could not be locked for deletion.")

    sheet_rows_deleted = 0
    try:
        if (account.get("google_sheet_id") or "").strip() and (tracked.get("email") or "").strip():
            sheet_rows_deleted = sheets.delete_contact_rows(account, tracked["email"])
        purged = tracked_threads.purge_contact(account["id"], tracked_id)
        if not purged or not purged.get("ok"):
            raise RuntimeError("The contact cleanup transaction did not complete.")
    except Exception as exc:
        try:
            tracked_threads.restore_after_delete_failure(account["id"], tracked_id, exc)
        except Exception as restore_exc:
            print(f"Could not restore failed contact deletion {tracked_id}: {restore_exc}")
        print(f"Could not delete contact {tracked_id} for {account['id']}: {exc}")
        raise HTTPException(
            status_code=503,
            detail="The contact could not be fully deleted. Nothing was deleted from Gmail; retry in a moment.",
        ) from exc
    return {
        "ok": True,
        "sheet_rows_deleted": sheet_rows_deleted,
        "gmail_deleted": False,
    }


def _draft_tracked_reply_job(account: dict, tracked_id: int) -> dict:
    """Prepare one review from the newest unanswered reply on a tracked thread.

    This creates the same durable review the worker creates.  It never sends
    mail, never returns a Gmail thread id to the browser, and never invents a
    second delivery path: the modal's Send button still calls send_reply.
    """
    tracked = tracked_threads.get_by_id(account["id"], tracked_id)
    if not tracked or tracked.get("status") != tracked_threads.ACTIVE_STATUS:
        raise RuntimeError("This contact is no longer being monitored.")
    thread_id = (tracked.get("thread_id") or "").strip()
    contact_email = (tracked.get("email") or "").strip().lower()
    if not thread_id or not contact_email:
        raise RuntimeError("This monitored thread does not have a usable Gmail contact.")

    try:
        thread = gmail.get_thread(account, thread_id)
        own_addresses, degraded = gmail.get_own_addresses(account)
    except Exception as exc:
        print(f"Could not read tracked Gmail thread {tracked_id} for {account['id']}: {exc}")
        raise RuntimeError("Gmail could not read this conversation. Reconnect Gmail or try again.") from exc
    if degraded:
        raise RuntimeError(
            "Gmail aliases could not be verified, so Sendkeep refused to guess who sent the message. Try again."
        )

    candidates = gmail.get_reply_candidates_with_history(
        account, thread_id, contact_email, own_addresses, thread=thread,
    )
    matching = [
        candidate for candidate in candidates
        if candidate.get("kind") == "on_sheet"
        and (candidate.get("from_addr") or "").strip().lower() == contact_email
    ]
    if not matching:
        raise RuntimeError("No reply from this contact is waiting for an answer.")
    candidate = matching[-1]
    if candidates[-1].get("message_id") != candidate.get("message_id"):
        raise RuntimeError(
            "A newer message from another participant changed this conversation. Review it in Gmail before drafting."
        )
    if candidate.get("answered_elsewhere"):
        raise RuntimeError("You already answered the latest reply in Gmail. No draft was created.")
    customer_reply = (candidate.get("text") or "").strip()
    message_id = (candidate.get("message_id") or "").strip()
    if not customer_reply or not message_id:
        raise RuntimeError("The latest reply has no readable message text to draft from.")

    existing_id = reviews_db.find_review_id(
        account["id"], thread_id, message_id, customer_reply,
    )
    if existing_id is not None:
        existing = reviews_db.get_review(account["id"], existing_id)
        if existing and existing.get("status") == "pending" and existing.get("kind", "reply") == "reply":
            return {
                "review_id": existing["id"],
                "contact": {
                    "name": existing.get("name") or tracked.get("name") or "",
                    "email": existing.get("email") or contact_email,
                },
                "subject": gmail.get_reply_subject(thread)[:500],
                "latest_message": existing.get("customer_reply") or customer_reply,
                "draft": existing.get("draft_reply") or "",
                "validator_problems": existing.get("validator_problems") or "",
                "existing": True,
            }
        if existing and existing.get("status") in ("sending", "send_uncertain"):
            raise RuntimeError("This reply is already being sent or verified in Gmail.")
        raise RuntimeError("The latest reply was already handled. Refresh the reply desk.")

    try:
        plans.check(account, usage.UNIT_DRAFT_REPLY)
    except plans.QuotaExceeded as exc:
        # _run_job deliberately exposes RuntimeError messages but hides raw
        # exceptions. Preserve the plan-specific allowance guidance instead
        # of turning a normal quota outcome into "Something went wrong".
        raise RuntimeError(str(exc)) from exc
    try:
        draft = agent.draft_reply(
            account,
            tracked.get("name") or contact_email.split("@", 1)[0],
            tracked.get("company") or "",
            customer_reply,
            candidate.get("history") or (),
        )
        problems = agent.reply_problems(account, draft)
    except Exception as exc:
        print(f"Could not draft tracked Gmail reply {tracked_id} for {account['id']}: {exc}")
        raise RuntimeError("The AI could not prepare this reply. Try again or answer it in Gmail.") from exc

    # The contact may have been deleted while the model was working. Re-check
    # immediately before the durable write so a late job cannot resurrect it.
    current = tracked_threads.get_by_id(account["id"], tracked_id)
    if (
        not current
        or current.get("status") != tracked_threads.ACTIVE_STATUS
        or current.get("thread_id") != thread_id
    ):
        raise RuntimeError("This contact was removed while the draft was being prepared.")

    review_id, created = reviews_db.add_manual_review(
        account["id"], current.get("row_index"), current.get("name") or "",
        contact_email, thread_id, customer_reply, draft,
        gmail_message_id=message_id, status="pending",
        degraded_classification=False, validator_problems=problems,
        return_created=True,
    )
    review = reviews_db.get_review(account["id"], review_id)
    if not review or review.get("status") != "pending":
        raise RuntimeError("This reply changed while the draft was being prepared. Refresh the reply desk.")
    return {
        "review_id": review["id"],
        "contact": {
            "name": review.get("name") or current.get("name") or "",
            "email": review.get("email") or contact_email,
        },
        "subject": gmail.get_reply_subject(thread)[:500],
        "latest_message": review.get("customer_reply") or customer_reply,
        "draft": review.get("draft_reply") or "",
        "validator_problems": review.get("validator_problems") or "",
        "existing": not created,
    }


@app.post("/api/outreach/threads/{tracked_id}/draft-reply", status_code=202)
def draft_tracked_reply(request: Request, tracked_id: int):
    account = _account(request)
    tracked = tracked_threads.get_by_id(account["id"], tracked_id)
    if not tracked or tracked.get("status") != tracked_threads.ACTIVE_STATUS:
        raise HTTPException(status_code=404, detail="Monitored thread not found.")
    if not ratelimit.check(
        f"draft-thread-reply:{account['id']}:{tracked_id}", limit=10, window_seconds=3600
    ):
        raise HTTPException(status_code=429, detail="Too many drafts for this conversation. Try again later.")
    return {
        "job_id": start_job(
            account["id"], lambda: _draft_tracked_reply_job(account, tracked_id)
        )
    }


def _campaign_preview(account: dict) -> dict:
    domain_safety = send_outreach.domain_safety(account)
    if not (account.get("google_sheet_id") or "").strip():
        # First-touch preparation is the optional Sheet-backed lane. Keep the
        # safety endpoint useful for a Gmail-only account so the promise desk can
        # render its monitoring state without pretending there are contacts to
        # send.
        return {
            "send_mode": account.get("outreach_send_mode") or "manual",
            "bounces": bounces.stats(account, []),
            "eligible": 0,
            "eligible_total": 0,
            "sent_today": drafts_db.count_sent_last_24_hours(account["id"]),
            "daily_limit": plans.daily_send_limit_for(account),
            "remaining_today": plans.daily_send_limit_for(account),
            "capped": 0,
            "blockers": [
                "First-touch sending is optional and uses a Google Sheet; Gmail Sent monitoring is ready without one."
            ],
            "deliverability": domain_safety,
            "auto_send_enabled": config.AUTO_SEND_ENABLED,
            "reply_desk_only": True,
        }
    suppressed = suppressions_db.list_suppressed_emails(account["id"])
    readiness = sheets.campaign_readiness(
        account,
        sent_today=drafts_db.count_sent_last_24_hours(account["id"]),
        suppressed_emails=suppressed,
    )
    # One read, shared by both bounce calls (pause_reason re-derives the stats
    # in memory rather than hitting the sheet again).
    rows = sheets.get_all_rows(account)
    bounce_stats = bounces.stats(account, rows)
    bounce_pause = bounces.pause_reason(account, rows, current=bounce_stats)
    blockers = (
        agent.account_send_blockers(account)
        + ([bounce_pause] if bounce_pause else [])
        + domain_safety.get("send_blockers", [])
    )
    if (account.get("outreach_send_mode") or "manual") == "auto" and not config.AUTO_SEND_ENABLED:
        blockers.append(
            "Auto-send is disabled in safety-first mode; switch Outreach send mode to Manual review."
        )
    return {
        "send_mode": account.get("outreach_send_mode") or "manual",
        "bounces": bounce_stats,
        "eligible": len(readiness["eligible"]),
        "eligible_total": readiness["eligible_total"],
        "sent_today": readiness["sent_today"],
        "daily_limit": readiness["daily_limit"],
        "remaining_today": readiness["remaining_today"],
        "capped": readiness["capped"],
        # Settings that must be filled before anything can be drafted. When
        # non-empty the UI disables the prepare button and shows why, rather
        # than letting a batch fail every row at generation time. An unsafe
        # bounce rate rides the same channel: it is exactly a reason the user
        # must not send right now, and the prepare endpoint already turns a
        # non-empty blockers list into a 409.
        "blockers": blockers,
        "deliverability": domain_safety,
        "auto_send_enabled": config.AUTO_SEND_ENABLED,
    }


@app.get("/api/outreach/campaigns/preview")
def campaign_preview(request: Request):
    return _campaign_preview(_account(request))


@app.post("/api/outreach/bounces/acknowledge")
def acknowledge_bounces(request: Request):
    """Lifts a bounce pause without deleting anything.

    The alternative remediation -- "remove the bad addresses from your sheet" --
    is the only other way to bring the rate down, and asking a customer to edit
    the record is a strange thing for a product that sells an audit trail to do.
    Every hard bounce counted here is already suppressed, so continuing cannot
    re-send to any of them. The watermark means new bounces pause again."""
    account = _account(request)
    return bounces.acknowledge(account)


class PrepareCampaignBody(BaseModel):
    confirmed: bool = False


class PrepareContactDraftBody(BaseModel):
    email: str


@app.post("/api/outreach/campaigns/row/{row_index}/draft", status_code=202)
def prepare_contact_draft(
    request: Request, row_index: int, payload: PrepareContactDraftBody
):
    """Prepare one human-reviewed first-touch email from a Pending Sheet row."""
    account = _account(request)
    if not (account.get("google_sheet_id") or "").strip():
        raise HTTPException(status_code=409, detail="Connect a Google Sheet before drafting a first email.")
    if row_index < 2 or not payload.email.strip():
        raise HTTPException(status_code=400, detail="Choose a valid Pending contact.")
    if not ratelimit.check(
        f"contact-draft:{account['id']}:{payload.email.strip().lower()}",
        limit=10,
        window_seconds=3600,
    ):
        raise HTTPException(status_code=429, detail="Too many drafts for this contact. Try again later.")

    with _send_lock_guard:
        if account["id"] in _accounts_sending:
            raise HTTPException(
                status_code=409,
                detail="Another draft is already being prepared. Wait for it to finish.",
            )
        _accounts_sending.add(account["id"])

    def run():
        try:
            return send_outreach.prepare_draft_for_contact(
                account, row_index, payload.email
            )
        finally:
            with _send_lock_guard:
                _accounts_sending.discard(account["id"])

    try:
        return {"job_id": start_job(account["id"], run)}
    except Exception:
        with _send_lock_guard:
            _accounts_sending.discard(account["id"])
        raise


@app.post("/api/outreach/campaigns/prepare", status_code=202)
def prepare_campaigns(request: Request, payload: PrepareCampaignBody):
    """Generate validated drafts. Manual accounts queue them for review; auto
    accounts send only the drafts made by this batch."""
    account = _account(request)
    send_mode = account.get("outreach_send_mode") or "manual"
    if send_mode == "auto" and not config.AUTO_SEND_ENABLED:
        raise HTTPException(
            status_code=409,
            detail=(
                "Auto-send is disabled in safety-first mode. Switch to Manual review "
                "before preparing a batch."
            ),
        )
    if send_mode == "auto":
        monitoring = _account_monitoring(account)
        if monitoring["status"] != "healthy":
            raise HTTPException(
                status_code=409,
                detail=(
                    "Optional first-touch preparation is paused because Gmail monitoring "
                    f"is {monitoring['status']}. {monitoring['message']}"
                ),
            )
    if not ratelimit.check(f"batch-prepare:{account['id']}", limit=5, window_seconds=3600):
        raise HTTPException(status_code=429, detail="Too many draft batches this hour. Try again later.")
    preview = _campaign_preview(account)
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="Review the batch and confirm before preparing drafts.")
    if preview["blockers"]:
        raise HTTPException(status_code=409, detail=" ".join(preview["blockers"]))
    if not preview["eligible"]:
        raise HTTPException(status_code=409, detail="No approved contacts are available to draft today. Approve leads on the Leads page or wait for the daily limit to reset.")
    # Checked here as well as inside prepare_drafts. The gate that actually
    # protects the quota is the one on the operation (it covers the CLI too);
    # this one exists so an exhausted account gets an immediate 402 with an
    # upgrade path, instead of a 202 and a job that fails a few seconds later
    # somewhere they have to go looking for it.
    plans.check(account, usage.UNIT_DRAFT_EMAIL)

    # The lock still serializes one prepare batch per account -- generating 25
    # drafts twice concurrently would waste LLM calls and race the dedupe.
    with _send_lock_guard:
        if account["id"] in _accounts_sending:
            raise HTTPException(
                status_code=409,
                detail="A batch is already preparing for this account — wait for it to finish.",
            )
        _accounts_sending.add(account["id"])

    def run():
        try:
            auto_send = (account.get("outreach_send_mode") or "manual") == "auto"
            prepared = send_outreach.prepare_drafts(account, auto_send=auto_send)
            if not auto_send or not prepared["draft_ids"]:
                return prepared
            sent = send_outreach.send_all_prepared(account, draft_ids=prepared["draft_ids"])
            return {**prepared, "auto_sent": sent}
        finally:
            with _send_lock_guard:
                _accounts_sending.discard(account["id"])

    try:
        return {"job_id": start_job(account["id"], run)}
    except Exception:
        # If the job thread never spawned, run()'s finally never fires --
        # release here or the account is wedged on 409 until restart.
        with _send_lock_guard:
            _accounts_sending.discard(account["id"])
        raise


@app.get("/api/outreach/drafts")
def list_drafts(request: Request):
    # `lane` is decided server-side and sent down, rather than letting the
    # frontend work it out. The rule is a safety property (see reviews_db.lane),
    # and a rule the client re-derives is a rule that drifts from this one the
    # first time either side changes.
    drafts = drafts_db.list_visible_drafts(request.state.account_id)
    for d in drafts:
        d["lane"] = drafts_db.lane(d)
    return drafts


class SendDraftBody(BaseModel):
    subject: str
    body: str
    rewritten: bool = False


@app.post("/api/outreach/drafts/{draft_id}/send")
def send_draft(request: Request, draft_id: int, payload: SendDraftBody):
    account = _account(request)
    draft = drafts_db.get_draft(account["id"], draft_id)
    if not draft or draft["status"] != "pending":
        raise HTTPException(status_code=404, detail="Draft not found or already handled")
    # send_prepared_draft records the send in the draft queue itself, immediately
    # after Gmail accepts it -- doing it out here meant a failed sheet write threw
    # past this line and left the draft pending, i.e. queued to send again.
    try:
        result = send_outreach.send_prepared_draft(
            account, draft, subject=payload.subject, body=payload.body,
            rewritten=payload.rewritten,
        )
    except send_capacity_db.SendCapacityExceeded as e:
        raise send_outreach.SendFailure(
            str(e), code="capacity_reached", retryable=True
        ) from e
    # 200, not an error: the email was delivered. Only the sheet needs a fix, and
    # returning this as a failure is what prompted an operator to click Send twice.
    response = {
        "ok": True,
        "sheet_warning": result["sheet_error"],
    }
    # Keep the established response shape on the normal path. Add the new
    # warning only when the durable thread mirror actually needs repair.
    if result.get("tracking_error"):
        response["tracking_warning"] = result["tracking_error"]
    return response


class RewriteDraftBody(BaseModel):
    subject: str
    body: str
    instruction: str


@app.post("/api/outreach/drafts/{draft_id}/rewrite", status_code=202)
def rewrite_draft(request: Request, draft_id: int, payload: RewriteDraftBody):
    account = _account(request)
    draft = drafts_db.get_draft(account["id"], draft_id)
    if not draft or draft["status"] != "pending":
        raise HTTPException(status_code=404, detail="Draft not found or already handled")
    if not payload.instruction.strip():
        raise HTTPException(status_code=400, detail="Tell the AI what to change.")
    # Scoped per draft, not per account: an account-wide limit fails ordinary
    # use (a batch of 25 drafts with one rewrite pass each is 25 calls against
    # a shared counter), not just abuse. This bounds hammering ONE draft.
    if not ratelimit.check(f"rewrite-draft:{account['id']}:{draft_id}", limit=10, window_seconds=3600):
        raise HTTPException(status_code=429, detail="Too many rewrite requests for this draft. Try again later.")

    def job():
        unsub_url = auth.unsubscribe_url(account["id"], draft["email"])
        subject, body = agent.rewrite_outreach_email(
            account, draft["name"], draft["company"], payload.subject, payload.body,
            payload.instruction, unsubscribe_url=unsub_url,
        )
        return {"subject": subject, "body": body}

    return {"job_id": start_job(account["id"], job)}


@app.post("/api/outreach/drafts/{draft_id}/discard")
def discard_draft(request: Request, draft_id: int):
    account = _account(request)
    draft = drafts_db.get_draft(account["id"], draft_id)
    if not draft or draft["status"] != "pending":
        raise HTTPException(status_code=404, detail="Draft not found or already handled")
    drafts_db.discard(account["id"], draft_id)
    return {"ok": True}


@app.post("/api/outreach/drafts/send-all", status_code=202)
def send_all_drafts(request: Request):
    """Send every pending draft, human-paced, as a background job. Cap and
    suppression are re-checked per draft inside send_all_prepared."""
    account = _account(request)
    if not ratelimit.check(f"batch-send:{account['id']}", limit=5, window_seconds=3600):
        raise HTTPException(status_code=429, detail="Too many send batches this hour. Try again later.")
    if not drafts_db.list_pending_drafts(account["id"]):
        raise HTTPException(status_code=409, detail="No drafts are waiting to be sent.")

    with _send_lock_guard:
        if account["id"] in _accounts_sending:
            raise HTTPException(status_code=409, detail="A batch is already running for this account — wait for it to finish.")
        _accounts_sending.add(account["id"])

    def run():
        try:
            return send_outreach.send_all_prepared(account)
        finally:
            with _send_lock_guard:
                _accounts_sending.discard(account["id"])

    try:
        return {"job_id": start_job(account["id"], run)}
    except Exception:
        with _send_lock_guard:
            _accounts_sending.discard(account["id"])
        raise



@app.post("/api/outreach/replies/check", status_code=202)
def check_replies(request: Request):
    account = _account(request)
    # 100s auto-poll on the frontend works out to ~36/hour; leave headroom above that.
    if not ratelimit.check(f"replies-check:{account['id']}", limit=45, window_seconds=3600):
        raise HTTPException(status_code=429, detail="Checking replies too often. Try again later.")
    return {"job_id": start_job(account["id"], lambda: watch_replies.check_for_replies(account))}


def _tracked_contact(account_id, thread_id):
    if not thread_id:
        return {}
    try:
        direct = tracked_threads.get(account_id, thread_id)
        if direct:
            return direct
    except Exception:
        pass
    # Compatibility fallback for a pre-migration deployment. Once the direct
    # lookup works, this path is never used in production.
    try:
        return next(
            (row for row in tracked_threads.list_active(account_id)
             if row.get("thread_id") == thread_id),
            {},
        )
    except Exception:
        return {}


@app.get("/api/outreach/replies")
def list_replies(request: Request):
    # Always "full". Sent explicitly anyway, so the fast-approval UI reads the
    # lane off every item uniformly instead of carrying a rule like "replies are
    # the ones you slow down for" -- a rule in the client is a rule that can be
    # forgotten in a refactor, and this is the one that must not be.
    reviews = reviews_db.list_pending_reviews(request.state.account_id)
    for r in reviews:
        r["lane"] = reviews_db.lane(r)
        if r.get("kind") == "follow_up" and r.get("source_draft_id"):
            source = drafts_db.get_draft(request.state.account_id, r["source_draft_id"])
            r["follow_up_status"] = source.get("follow_up_status") if source else "missing"
    return reviews


@app.get("/api/outreach/commitments")
def list_commitments(request: Request):
    """Returns promises needing confirmation or a due-date check."""
    active_commitments = commitments_db.list_active(request.state.account_id)
    if not active_commitments:
        return []
    for commitment in active_commitments:
        # Repair dates missed by older extractor versions from the durable,
        # user-visible evidence. This is deliberately conservative: the row
        # must still have no date, the parser must recover an explicit one, and
        # the compare-and-set update cannot confirm or otherwise advance it.
        if not commitment.get("due_at"):
            candidates = commitments.extract_commitments(
                commitment.get("evidence") or commitment.get("action_text") or "",
                reference_at=commitment.get("created_at"),
                actor=commitment.get("actor") or "contact",
            )
            dated = [candidate for candidate in candidates if candidate.get("due_at")]
            if len(dated) == 1:
                candidate = dated[0]
                repaired = commitments_db.repair_missing_due_date(
                    request.state.account_id,
                    commitment.get("id"),
                    candidate["due_at"],
                    candidate.get("due_text") or "",
                    candidate.get("confidence"),
                )
                if repaired:
                    commitment.update(repaired)
        contact = _tracked_contact(request.state.account_id, commitment.get("thread_id"))
        if contact:
            commitment.setdefault("contact_name", contact.get("name") or "")
            commitment.setdefault("contact_email", contact.get("email") or "")
    return active_commitments


@app.get("/api/outreach/commitments/resolved")
def list_resolved_commitments(request: Request):
    """Return recently completed promises for the explicit outcome nudge."""
    commitments = commitments_db.list_completed(request.state.account_id)
    if not commitments:
        return []
    for commitment in commitments:
        contact = _tracked_contact(request.state.account_id, commitment.get("thread_id"))
        if contact:
            commitment.setdefault("contact_name", contact.get("name") or "")
            commitment.setdefault("contact_email", contact.get("email") or "")
    return commitments


@app.get("/api/outreach/commitments/summary")
def commitment_summary(request: Request):
    try:
        result = commitments_db.summary(request.state.account_id)
        oldest = result.get("oldest_overdue")
        if oldest:
            try:
                contact = _tracked_contact(
                    request.state.account_id, oldest.get("thread_id")
                )
            except Exception:
                contact = None
            if contact:
                oldest["contact_name"] = contact.get("name") or ""
                oldest["contact_email"] = contact.get("email") or ""
        return result
    except Exception as e:
        print(f"Could not load commitment summary for {request.state.account_id}: {e}")
        raise HTTPException(status_code=503, detail="Promise graph summary is temporarily unavailable") from e


class ConfirmCommitmentBody(BaseModel):
    due_at: datetime | None = None
    estimated_value: float | None = Field(default=None, ge=0, le=100000000)


@app.post("/api/outreach/commitments/{commitment_id}/confirm")
def confirm_commitment(
    request: Request,
    commitment_id: int,
    payload: ConfirmCommitmentBody,
):
    commitment = commitments_db.get(request.state.account_id, commitment_id)
    if not commitment:
        raise HTTPException(status_code=404, detail="Commitment not found")
    due_at = payload.due_at
    if due_at is None and commitment.get("due_at"):
        raw_due_at = commitment.get("due_at")
        try:
            due_at = datetime.fromisoformat(str(raw_due_at).replace("Z", "+00:00"))
        except ValueError as e:
            raise HTTPException(
                status_code=503,
                detail="This promise has an invalid stored date. Refresh the Gmail data before confirming it.",
            ) from e
    if due_at is None:
        raise HTTPException(
            status_code=422,
            detail="Add a date before confirming a promise with no detected date.",
        )
    if due_at and due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=timezone.utc)
    confirmed = commitments_db.confirm(
        request.state.account_id,
        commitment_id,
        due_at=due_at.isoformat() if isinstance(due_at, datetime) else due_at,
        estimated_value=payload.estimated_value,
    )
    if not confirmed:
        raise HTTPException(status_code=409, detail="Commitment was already handled")
    return confirmed


@app.post("/api/outreach/commitments/{commitment_id}/complete")
def complete_commitment(request: Request, commitment_id: int):
    completed = commitments_db.complete(request.state.account_id, commitment_id)
    if not completed:
        raise HTTPException(status_code=404, detail="Promise not found or already dismissed")
    try:
        contact = _tracked_contact(
            request.state.account_id, completed.get("thread_id")
        )
    except Exception:
        contact = None
    if contact:
        completed.setdefault("contact_name", contact.get("name") or "")
        completed.setdefault("contact_email", contact.get("email") or "")
    return completed


@app.post("/api/outreach/commitments/{commitment_id}/dismiss")
def dismiss_commitment(request: Request, commitment_id: int):
    commitment = commitments_db.get(request.state.account_id, commitment_id)
    if not commitment:
        raise HTTPException(status_code=404, detail="Commitment not found or already handled")
    # Rejecting a fresh candidate is an extraction false positive. Clearing a
    # confirmed promise after it becomes due is operational cleanup, not a
    # model-quality judgment, and must not poison the precision metric.
    reason = "incorrect" if commitment.get("status") == "detected" else "not_applicable"
    if not commitments_db.dismiss(request.state.account_id, commitment_id, reason=reason):
        raise HTTPException(status_code=404, detail="Commitment not found or already handled")
    return {"ok": True, "reason": reason}


@app.get("/api/outreach/follow-ups/status")
def follow_up_status(request: Request):
    account_id = request.state.account_id
    return {
        **drafts_db.follow_up_summary(account_id),
        **drafts_db.follow_up_outcomes(account_id),
        **reviews_db.follow_up_edit_metrics(account_id),
    }


@app.get("/api/outreach/evidence")
def outreach_evidence(request: Request):
    """Aggregate-only evidence from the manual review loop.

    This is intentionally not a performance claim: it reports approvals,
    human edits, and replies after follow-ups that this account actually
    recorded. Booked meetings are not inferred from a calendar URL or a reply.
    """
    account_id = request.state.account_id
    try:
        outcome_summary = outcomes_db.summary(account_id)
    except Exception as e:
        print(f"Could not load confirmed outcome summary for {account_id}: {e}")
        outcome_summary = None
    return {
        "first_touch": drafts_db.edit_metrics(account_id),
        "replies": reviews_db.reply_edit_metrics(account_id),
        "follow_ups": drafts_db.follow_up_outcomes(account_id),
        "outcomes": outcome_summary,
    }


@app.get("/api/outreach/dogfood-log")
def list_dogfood_logs(request: Request):
    """Return aggregate-only founder/agency distribution logs for this inbox."""
    try:
        return {"logs": dogfood_db.list_logs(request.state.account_id)}
    except Exception as e:
        print(f"Could not load dogfood log for {request.state.account_id}: {e}")
        raise HTTPException(status_code=503, detail="Dogfood log is temporarily unavailable") from e


class DogfoodLogBody(BaseModel):
    date: str = Field(min_length=10, max_length=10)
    inbox_label: str = Field(default="", max_length=80)
    reviewed: int = Field(default=0, ge=0, le=10000)
    sent: int = Field(default=0, ge=0, le=10000)
    replies: int = Field(default=0, ge=0, le=10000)
    follow_up_replies: int = Field(default=0, ge=0, le=10000)
    meetings: int = Field(default=0, ge=0, le=10000)
    deals: int = Field(default=0, ge=0, le=10000)
    human_edits: int = Field(default=0, ge=0, le=10000)
    minutes_saved: int = Field(default=0, ge=0, le=100000)
    next_inbox: str = Field(default="", max_length=120)
    safety_event: str = Field(default="none", max_length=160)


_DOGFOOD_SAFETY_EVENTS = frozenset({
    "none", "duplicate", "bounce", "opt_out", "stale_worker",
    "uncertain_send", "wrong_recipient", "other",
})


@app.post("/api/outreach/dogfood-log")
def save_dogfood_log(request: Request, payload: DogfoodLogBody):
    """Upsert one aggregate-only operator log row, keyed by UTC date."""
    try:
        datetime.strptime(payload.date, "%Y-%m-%d")
    except ValueError as e:
        raise HTTPException(status_code=422, detail="date must use YYYY-MM-DD") from e

    labels = (payload.inbox_label.strip(), payload.next_inbox.strip())
    if any("@" in value for value in labels):
        raise HTTPException(status_code=422, detail="Use internal labels, not email addresses")
    safety_event = payload.safety_event.strip() or "none"
    if safety_event not in _DOGFOOD_SAFETY_EVENTS:
        raise HTTPException(status_code=422, detail="Unsupported safety event")

    try:
        log = dogfood_db.upsert(
            request.state.account_id,
            date=payload.date,
            inbox_label=payload.inbox_label.strip(),
            reviewed=payload.reviewed,
            sent=payload.sent,
            replies=payload.replies,
            follow_up_replies=payload.follow_up_replies,
            meetings=payload.meetings,
            deals=payload.deals,
            human_edits=payload.human_edits,
            minutes_saved=payload.minutes_saved,
            next_inbox=payload.next_inbox.strip(),
            safety_event=safety_event,
        )
        return log
    except Exception as e:
        print(f"Could not save dogfood log for {request.state.account_id}: {e}")
        raise HTTPException(status_code=503, detail="Dogfood log could not be saved") from e


class OutcomeBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    outcome: Literal["meeting_booked", "deal_won", "deal_lost"]
    value_usd: float | None = Field(default=None, ge=0, le=100000000)


def _public_outcome(row):
    details = row.get("details") or {}
    return {
        "id": row.get("id"),
        "outcome": row.get("event_type") or "",
        "status": row.get("status") or "confirmed",
        "email": details.get("email") or "",
        "name": details.get("name") or "",
        "company": details.get("company") or "",
        "createdAt": row.get("created_at") or "",
        "valueUsd": details.get("value_usd"),
    }


@app.get("/api/outreach/outcomes")
def list_outcomes(request: Request):
    account_id = request.state.account_id
    try:
        rows = outcomes_db.list_outcomes(account_id)
        return {
            "events": [_public_outcome(row) for row in rows],
            "summary": outcomes_db.summary(account_id, rows),
        }
    except Exception as e:
        print(f"Could not load confirmed outcomes for {account_id}: {e}")
        raise HTTPException(status_code=503, detail="Outcome history is temporarily unavailable") from e


@app.post("/api/outreach/outcomes")
def record_outcome(request: Request, payload: OutcomeBody):
    email = payload.email.strip().lower()
    if not email:
        raise HTTPException(status_code=422, detail="Email is required")

    # An outcome must attach to a conversation Sendkeep is already monitoring;
    # this prevents an arbitrary email address from becoming a fake CRM row.
    campaign = next(
        (row for row in list_campaigns(request)
         if (row.get("email") or "").strip().lower() == email),
        None,
    )
    if not campaign:
        raise HTTPException(status_code=404, detail="That conversation is not in the monitored queue")

    try:
        existing = next(
            (row for row in outcomes_db.list_outcomes(request.state.account_id)
             if (row.get("event_type") == payload.outcome
                 and ((row.get("details") or {}).get("email") or "").lower() == email)),
            None,
        )
        if existing:
            return _public_outcome(existing)
        saved = outcomes_db.record(
            request.state.account_id,
            payload.outcome,
            email=email,
            name=campaign.get("name") or "",
            company=campaign.get("company") or "",
            source=campaign.get("source") or "",
            value_usd=payload.value_usd,
        )
        return _public_outcome(saved)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Could not record confirmed outcome for {request.state.account_id}: {e}")
        raise HTTPException(status_code=503, detail="Outcome could not be saved") from e


@app.get(WORKER_HEALTH_PATH)
def worker_health(request: Request):
    expected = os.environ.get("WORKER_HEALTH_TOKEN", "").strip()
    provided = request.headers.get("x-worker-health-token", "")
    if not expected:
        return JSONResponse(
            {"ok": False, "status": "not_configured"},
            status_code=503,
        )
    if not provided or not hmac.compare_digest(provided, expected):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    snapshot = worker_db.health_snapshot()
    return JSONResponse(snapshot, status_code=200 if snapshot["ok"] else 503)


@app.post("/api/outreach/campaigns/{row}/check-reply")
def check_single_reply(request: Request, row: int):
    """Lightweight per-contact check: has this one person replied yet?
    Read-only — unlike /api/outreach/replies/check, it doesn't draft a
    response, touch the sheet, or send a notification."""
    account = _account(request)
    match = next((r for idx, r in sheets.get_all_rows(account) if idx == row), None)
    if not match:
        raise HTTPException(status_code=404, detail="Row not found")

    thread_id = match[sheets.COL_THREAD_ID].strip()
    if not thread_id:
        raise HTTPException(status_code=400, detail="No outreach sent to this contact yet")

    reply_text = gmail.get_latest_reply(account, thread_id, match[sheets.COL_EMAIL].strip())
    return {"replied": bool(reply_text), "replyText": reply_text}


class SendReplyBody(BaseModel):
    body: str = Field(max_length=20_000)
    rewritten: bool = False


class ReconcileFollowUpBody(BaseModel):
    outcome: Literal["sent", "retry"]


class SnoozeFollowUpBody(BaseModel):
    days: int = Field(default=1, ge=1, le=14)


@app.post("/api/outreach/follow-ups/{draft_id}/reconcile")
def reconcile_follow_up(request: Request, draft_id: int, payload: ReconcileFollowUpBody):
    """Legacy manual resolver for an ambiguous Gmail send.

    The normal product path is now automatic: the worker reads the original
    Gmail Sent thread and closes an exact match without asking the operator to
    guess. This endpoint remains for old tabs and explicit recovery tooling;
    its compare-and-set transitions still prevent a stale tab from reopening a
    duplicate-send risk.
    """
    account = _account(request)
    source = drafts_db.get_draft(account["id"], draft_id)
    review = reviews_db.find_follow_up(account["id"], draft_id)
    if not source or not review or review.get("status") != "pending":
        raise HTTPException(status_code=404, detail="Uncertain follow-up not found.")
    source_status = source.get("follow_up_status")

    if payload.outcome == "retry":
        if source_status != "sending":
            raise HTTPException(status_code=409, detail="This follow-up can no longer be returned to the queue.")
        if suppressions_db.is_suppressed(account["id"], review["email"]):
            if drafts_db.cancel_claimed_follow_up(account["id"], draft_id, "opt_out"):
                reviews_db.dismiss(account["id"], review["id"])
            raise HTTPException(
                status_code=409,
                detail=f"{review['email']} has opted out; the follow-up was cancelled instead of re-queued.",
            )
        if not drafts_db.release_follow_up_send(account["id"], draft_id):
            raise HTTPException(status_code=409, detail="This follow-up was resolved in another window.")
        return {"ok": True, "status": "queued"}

    if source_status == "sending":
        if not drafts_db.mark_follow_up_sent(account["id"], draft_id):
            raise HTTPException(status_code=409, detail="This follow-up was resolved in another window.")
    elif source_status != "sent":
        raise HTTPException(status_code=409, detail="This follow-up is no longer awaiting reconciliation.")
    # Source first is the duplicate-send fence. If the secondary review write
    # fails, retries cannot resend because the source is already `sent`. The
    # endpoint also accepts sent+pending so the operator can finish this
    # secondary write after a transient database failure without lying about
    # Gmail or reopening delivery.
    send_outreach._with_retries(
        lambda: reviews_db.mark_sent(account["id"], review["id"], review.get("draft_reply") or ""),
        f"Follow-up {draft_id} was reconciled as sent, but closing its review failed",
    )
    return {"ok": True, "status": "sent"}


@app.post("/api/outreach/replies/{review_id}/send")
def send_reply(request: Request, review_id: int, payload: SendReplyBody):
    account = _account(request)
    review = reviews_db.get_review(account["id"], review_id)
    # Allowlist, not `!= "pending"`. Statuses are being added and they do not
    # all behave the same: a flagged off-sheet review becomes sendable once the
    # user confirms the sender, while answered_elsewhere never is -- there is
    # nothing to send, the operator already replied from Gmail. Getting this
    # wrong sends a second reply to a prospect, which is the failure the whole
    # detector rework exists to prevent.
    if not review or review["status"] not in reviews_db.SENDABLE_STATUSES:
        raise HTTPException(status_code=404, detail="Review not found or already handled")
    # review["email"] is the sheet contact's address for a normal reply, or
    # the observed off-sheet sender's for a confirmed flagged one -- either
    # way, the address a reply is actually about to reach. Unlike
    # _guard_draft_send's outreach-send check, nothing gated this path before
    # today; an unsubscribed contact could still receive a reply.
    is_follow_up = review.get("kind") == "follow_up"
    source_draft = None
    if is_follow_up:
        source_draft = drafts_db.get_draft(account["id"], review.get("source_draft_id"))
        if not source_draft or source_draft.get("follow_up_status") != "queued":
            if source_draft and source_draft.get("follow_up_status") == "sending":
                # A send claim that never finalized. The outcome is unknown --
                # never resend, and never silently treat it as gone.
                raise HTTPException(
                    status_code=409,
                    detail="This follow-up is being verified against Gmail Sent. It is locked "
                           "and will not be sent again automatically.",
                )
            reviews_db.dismiss(account["id"], review_id)
            raise HTTPException(status_code=409, detail="This follow-up was cancelled or handled elsewhere.")

    body = payload.body
    if not (body or "").strip():
        raise HTTPException(status_code=422, detail="The message body cannot be blank.")

    if suppressions_db.is_suppressed(account["id"], review["email"]):
        if source_draft:
            drafts_db.cancel_follow_up(account["id"], source_draft["id"], "opt_out")
            reviews_db.dismiss(account["id"], review_id)
        raise HTTPException(status_code=409, detail=f"{review['email']} has opted out; this reply won't be sent.")

    if source_draft:
        # Claim FIRST, then recheck. Claiming is the atomic ownership hand-off
        # (queued -> sending), so no second tab can dispatch while this request
        # performs its final Gmail and suppression checks. A reply can always
        # arrive after the last Gmail read and an opt-out can arrive after the
        # last database read; that is the unavoidable already-dispatching
        # boundary, not a guarantee we can make across two external systems.
        if not drafts_db.claim_follow_up_send(account["id"], source_draft["id"]):
            current = drafts_db.get_draft(account["id"], source_draft["id"])
            if current and current.get("follow_up_status") == "sending":
                # A concurrent tab won the claim. The send is in flight or
                # stranded uncertain. The winner owns the outcome -- success
                # marks the review sent, failure leaves it pending with the
                # uncertain message. Dismissing here hides a possibly-sent
                # follow-up behind a dismissed card that nothing else looks at.
                raise HTTPException(
                    status_code=409,
                    detail="This follow-up is being sent in another window — wait for it to finish.",
                )
            reviews_db.dismiss(account["id"], review_id)
            raise HTTPException(status_code=409, detail="This follow-up is already being sent or was cancelled elsewhere.")
        try:
            thread = gmail.get_thread(account, review["thread_id"])
            own_addresses, _ = gmail.get_own_addresses(account)
            candidate = gmail.get_latest_reply_with_history(
                account, review["thread_id"], review["email"], own_addresses, thread=thread
            )
            if candidate:
                if drafts_db.mark_claimed_follow_up_replied(
                    account["id"], source_draft["id"]
                ):
                    reviews_db.dismiss(account["id"], review_id)
                raise HTTPException(status_code=409, detail="This contact replied before the follow-up was sent. The follow-up was cancelled.")
            bounce = gmail.find_bounce(account, review["thread_id"], review["email"], thread=thread)
            if bounce:
                bounces.record(account, review["row_index"], review["email"], bounce)
                if drafts_db.cancel_claimed_follow_up(
                    account["id"], source_draft["id"], "bounce"
                ):
                    reviews_db.dismiss(account["id"], review_id)
                raise HTTPException(status_code=409, detail="A delivery failure arrived before this follow-up. It was cancelled.")
            # This is the final authoritative suppression read, intentionally
            # after the slower Gmail preflight so the check-to-dispatch window
            # is as small as this architecture can make it.
            if suppressions_db.is_suppressed(account["id"], review["email"]):
                cancelled = drafts_db.cancel_claimed_follow_up(
                    account["id"], source_draft["id"], "opt_out"
                )
                if cancelled:
                    reviews_db.dismiss(account["id"], review_id)
                raise HTTPException(status_code=409, detail=f"{review['email']} has opted out; this follow-up won't be sent.")
            unsubscribe_url = auth.unsubscribe_url(account["id"], review["email"])
            if unsubscribe_url not in body:
                body = f"{body.rstrip()}\n\n{agent._opt_out_line(unsubscribe_url)}"
            # Freeze the exact edited copy while the source claim is held. The
            # worker reconciles this value if Gmail's response is lost.
            if not reviews_db.update_claimed_body(account["id"], review_id, body):
                drafts_db.release_follow_up_send(account["id"], source_draft["id"])
                raise HTTPException(
                    status_code=409,
                    detail="This follow-up changed in another window; nothing was sent.",
                )
        except HTTPException:
            raise
        except Exception as e:
            # Nothing irreversible happened: every operation above is a read or
            # a cancellation. Return ownership to the queue so a Gmail read
            # outage does not become a permanent delivery-uncertain row.
            drafts_db.release_follow_up_send(account["id"], source_draft["id"])
            print(f"Follow-up preflight failed for review {review_id}: {e}")
            raise HTTPException(
                status_code=502,
                detail="The final reply check failed; nothing was sent. Try again.",
            ) from e

        operation_key = f"followup:{source_draft['id']}"
        try:
            reserved = send_capacity_db.reserve(
                account["id"], operation_key, plans.daily_send_limit_for(account)
            )
        except Exception as e:
            drafts_db.release_follow_up_send(account["id"], source_draft["id"])
            raise send_outreach.CapacityUnavailable(
                "Send capacity could not be verified; nothing was sent."
            ) from e
        if not reserved:
            drafts_db.release_follow_up_send(account["id"], source_draft["id"])
            raise send_outreach.SendFailure(
                "The rolling 24-hour send cap is reached, or this follow-up is already in progress.",
                code="capacity_reached", retryable=True,
            )
        try:
            gmail.send_reply(
                account, review["thread_id"], review["email"], body,
                unsubscribe_url=unsubscribe_url,
            )
        except Exception:
            # Gmail failed. The message may or may not have left -- a timeout
            # after Gmail accepted it is indistinguishable from a refusal. Do
            # NOT release the claim back to queued: that would put a possibly-
            # sent follow-up back on the Send button and produce a duplicate.
            # Leaving it in `sending` is the "delivery uncertain" state.
            raise send_outreach.DeliveryUncertain(
                "Gmail did not return a final send result. Sendkeep locked this follow-up "
                "and the worker will verify Gmail Sent before closing it; it will not be "
                "sent again automatically."
            )
    else:
        # Normal replies need the same claim-and-recheck boundary as
        # follow-ups. Without it, two browser tabs can both send a pending
        # review, or a direct Gmail reply can race this endpoint.
        if not reviews_db.claim_send(account["id"], review_id, body):
            raise HTTPException(status_code=409, detail="This reply is being handled in another window.")
        try:
            thread = gmail.get_thread(account, review["thread_id"])
            own_addresses, _ = gmail.get_own_addresses(account)
            candidate = gmail.get_latest_reply_with_history(
                account, review["thread_id"], review["email"], own_addresses, thread=thread
            )
            if not candidate:
                reviews_db.release_send_claim(account["id"], review_id)
                raise HTTPException(
                    status_code=409,
                    detail="Gmail could not re-validate this reply. Refresh the queue before sending.",
                )
            if (
                candidate.get("message_id") != review.get("gmail_message_id")
                or candidate.get("answered_elsewhere")
            ):
                reviews_db.release_send_claim(account["id"], review_id)
                raise HTTPException(
                    status_code=409,
                    detail="A newer Gmail reply or direct answer was found. Refresh the queue before sending.",
                )
            bounce = gmail.find_bounce(
                account, review["thread_id"], review["email"], thread=thread
            )
            if bounce:
                bounces.record(account, review.get("row_index"), review["email"], bounce)
                reviews_db.release_send_claim(account["id"], review_id)
                raise HTTPException(
                    status_code=409,
                    detail="A delivery failure was found in Gmail. This reply was not sent.",
                )
            if suppressions_db.is_suppressed(account["id"], review["email"]):
                reviews_db.release_send_claim(account["id"], review_id)
                raise HTTPException(status_code=409, detail=f"{review['email']} has opted out; this reply won't be sent.")
        except HTTPException:
            raise
        except Exception as e:
            reviews_db.release_send_claim(account["id"], review_id)
            raise HTTPException(
                status_code=502,
                detail="The final Gmail safety check failed; nothing was sent. Try again.",
            ) from e
        operation_key = f"review:{review_id}"
        try:
            reserved = send_capacity_db.reserve(
                account["id"], operation_key, plans.daily_send_limit_for(account)
            )
        except Exception as e:
            reviews_db.release_send_claim(account["id"], review_id)
            raise send_outreach.CapacityUnavailable(
                "Send capacity could not be verified; nothing was sent."
            ) from e
        if not reserved:
            reviews_db.release_send_claim(account["id"], review_id)
            raise send_outreach.SendFailure(
                "The rolling 24-hour send cap is reached, or this reply is already in progress.",
                code="capacity_reached", retryable=True,
            )
        try:
            gmail.send_reply(account, review["thread_id"], review["email"], body)
        except Exception as e:
            raise send_outreach.DeliveryUncertain(
                "Gmail did not return a final send result. Sendkeep locked this reply and the worker will verify Gmail Sent before closing it."
            ) from e
    try:
        if source_draft:
            # This is the durable duplicate-send fence after Gmail's irreversible
            # action. Match first-touch sending: retry the authoritative state
            # before touching the secondary review record.
            def record_follow_up_sent():
                result = drafts_db.mark_follow_up_sent(account["id"], source_draft["id"])
                if result is False:
                    raise RuntimeError("the source row was no longer in sending state")
                return result

            send_outreach._with_retries(
                record_follow_up_sent,
                f"Follow-up was sent to {review['email']}, but recording it failed",
            )
        send_outreach._with_retries(
            lambda: reviews_db.mark_sent(account["id"], review_id, body),
            f"Reply was sent to {review['email']}, but closing its review failed",
        )
    except Exception as exc:
        raise send_outreach.DeliveryUncertain(
            "Gmail accepted the message, but Sendkeep could not record delivery. This action remains locked; verify Gmail Sent before taking another action."
        ) from exc
    try:
        send_capacity_db.complete(account["id"], operation_key)
    except Exception as e:
        print(f"Send reservation {operation_key} could not be closed: {e}")
    # Separate, best-effort -- never part of mark_sent's update. See
    # reviews_db.mark_rewritten for why this must never be able to make that
    # write (or this send) fail.
    if payload.rewritten:
        reviews_db.mark_rewritten(account["id"], review_id)
    return {"ok": True}


class RewriteReplyBody(BaseModel):
    body: str
    instruction: str


@app.post("/api/outreach/replies/{review_id}/rewrite", status_code=202)
def rewrite_reply_draft(request: Request, review_id: int, payload: RewriteReplyBody):
    account = _account(request)
    review = reviews_db.get_review(account["id"], review_id)
    # SENDABLE_STATUSES, not VISIBLE_STATUSES: excludes flagged (nothing
    # drafted yet to adjust until the sender is confirmed) and
    # answered_elsewhere (nothing to send).
    if not review or review["status"] not in reviews_db.SENDABLE_STATUSES:
        raise HTTPException(status_code=404, detail="Review not found or already handled")
    if review.get("kind") == "follow_up":
        raise HTTPException(status_code=409, detail="Edit the follow-up directly before sending.")
    if not payload.instruction.strip():
        raise HTTPException(status_code=400, detail="Tell the AI what to change.")
    if not ratelimit.check(f"rewrite-reply:{account['id']}:{review_id}", limit=10, window_seconds=3600):
        raise HTTPException(status_code=429, detail="Too many rewrite requests for this reply. Try again later.")

    def job():
        body = agent.rewrite_reply(
            account, review["name"], "", review["customer_reply"], payload.body, payload.instruction,
        )
        return {"body": body}

    return {"job_id": start_job(account["id"], job)}


@app.post("/api/outreach/replies/{review_id}/dismiss")
def dismiss_reply(request: Request, review_id: int):
    account = _account(request)
    review = reviews_db.get_review(account["id"], review_id)
    # Anything the operator can see, they can clear -- including the statuses
    # that are not sendable. An answered_elsewhere card with no way to dismiss
    # it is a queue item that never goes away.
    if not review or review["status"] not in reviews_db.VISIBLE_STATUSES:
        raise HTTPException(status_code=404, detail="Review not found or already handled")

    if review.get("kind") == "follow_up" and review.get("source_draft_id"):
        cancelled = drafts_db.cancel_follow_up(
            account["id"], review["source_draft_id"], "dismissed_by_sender"
        )
        if cancelled is False:
            raise HTTPException(
                status_code=409,
                detail="This follow-up is already being sent or was handled elsewhere.",
            )
    reviews_db.dismiss(account["id"], review_id)
    return {"ok": True}


@app.post("/api/outreach/replies/{review_id}/snooze")
def snooze_follow_up(request: Request, review_id: int, payload: SnoozeFollowUpBody):
    """Move one queued follow-up out of today's desk without creating another touch."""
    account = _account(request)
    review = reviews_db.get_review(account["id"], review_id)
    if (
        not review
        or review.get("status") != "pending"
        or review.get("kind") != "follow_up"
        or not review.get("source_draft_id")
    ):
        raise HTTPException(status_code=404, detail="Follow-up not found or already handled")
    due_at = datetime.now(timezone.utc) + timedelta(days=payload.days)
    if not drafts_db.snooze_follow_up(
        account["id"], review["source_draft_id"], due_at.isoformat()
    ):
        raise HTTPException(
            status_code=409,
            detail="This follow-up is already being sent or was handled elsewhere.",
        )
    # Source first is the safety fence. If this secondary write fails, the
    # send endpoint still sees `waiting` and refuses to dispatch the old card.
    reviews_db.dismiss(account["id"], review_id)
    return {"ok": True, "status": "snoozed", "due_at": due_at.isoformat()}


def _confirm_sender_job(account, review):
    """Drafts a reply for a flagged (off-sheet sender) review and confirms it,
    once the operator has looked at it and decided the sender is worth
    replying to. Runs as a background job (see start_job below) because
    agent.draft_reply carries the same retry loop and per-attempt timeout as
    every other drafting call in this codebase -- worst case spans minutes,
    and every other long-running agent action in this file already runs off
    the request thread.

    Re-checks the review's status first: a double-click, or the operator
    dismissing the review while an earlier click's job is still running,
    must not spend a quota check and an LLM call on a write that
    confirm_sender's `status = flagged` scoping was always going to refuse."""
    review = reviews_db.get_review(account["id"], review["id"])
    if not review or review["status"] != "flagged":
        return {"warning": "This review was handled elsewhere while confirming."}

    # Gmail-discovered threads are intentionally allowed to exist without a
    # Sheet row.  Do not turn sender confirmation into a hidden Sheet
    # dependency for those reviews.  A real row index remains authoritative
    # when one exists; an off-Sheet review simply drafts without company
    # metadata.
    company = ""
    if review.get("row_index") not in (None, reviews_db.LEGACY_NO_SHEET_ROW):
        rows = sheets.get_all_rows(account)
        row = next((r for i, r in rows if i == review["row_index"]), None)
        company = row[sheets.COL_COMPANY].strip() if row else ""
    own_addresses, _ = gmail.get_own_addresses(account)
    history = gmail.get_history_before(
        account, review["thread_id"], review["gmail_message_id"], review["email"], own_addresses,
    )

    # Drafting failures are deliberately never fatal here: confirming the
    # sender is a real, independent action the operator already took, and an
    # LLM problem must not undo it. Same split as the on-sheet path in
    # watch_replies.py -- QuotaExceeded gets its own branch so its rich,
    # plan-specific message survives instead of being buried in a generic one.
    warning = None
    try:
        plans.check(account, usage.UNIT_DRAFT_REPLY)
        draft = agent.draft_reply(account, review["name"], company, review["customer_reply"], history)
    except plans.QuotaExceeded as e:
        draft = ""
        warning = str(e)
    except Exception as e:
        draft = ""
        warning = f"Confirmed, but drafting failed ({e}). Write the reply yourself."

    confirmed = reviews_db.confirm_sender(
        account["id"], review["id"], draft_reply=draft,
        validator_problems=agent.reply_problems(account, draft),
    )
    if not confirmed:
        # Dismissed (or otherwise moved off "flagged") between the re-check
        # above and this write -- the draft just generated has nowhere to go.
        return {"warning": "This review was handled elsewhere while confirming."}
    return {"warning": warning}


@app.post("/api/outreach/replies/{review_id}/confirm-sender", status_code=202)
def confirm_reply_sender(request: Request, review_id: int):
    account = _account(request)
    review = reviews_db.get_review(account["id"], review_id)
    if not review or review["status"] != "flagged":
        raise HTTPException(status_code=404, detail="Review not found or already handled")

    return {"job_id": start_job(account["id"], lambda: _confirm_sender_job(account, review))}


# --- One-click opt-out (public, no session) -----------------------------------
# The recipient of an outreach email clicks this; there is no logged-in user.
# The signed token in the URL is the only credential. GET only ever renders a
# page -- the actual suppression happens on POST -- because mail clients and
# security scanners routinely prefetch GET links, and a GET that unsubscribed
# would silently opt people out who never clicked. The POST doubles as the RFC
# 8058 One-Click endpoint (List-Unsubscribe-Post), which Gmail fires directly.

def _unsub_page(title: str, message: str, token: str = "", status: int = 200) -> Response:
    button = (
        f'<form method="post" action="/unsubscribe">'
        f'<input type="hidden" name="t" value="{html.escape(token)}">'
        f'<button type="submit">Unsubscribe me</button></form>'
        if token else ""
    )
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{html.escape(title)}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    background: #0f0e0c; color: #ece7df; display: grid; place-items: center;
    min-height: 100vh; margin: 0; padding: 1.5rem; }}
  .card {{ max-width: 30rem; background: #1a1815; border: 1px solid #302c26;
    border-radius: 1rem; padding: 2.5rem; text-align: center; }}
  h1 {{ font-size: 1.35rem; margin: 0 0 0.75rem; }}
  p {{ color: #b8b0a4; line-height: 1.6; margin: 0 0 1.5rem; }}
  button {{ font: inherit; background: #d97757; color: #0f0e0c; border: 0;
    border-radius: 0.6rem; padding: 0.7rem 1.4rem; font-weight: 600; cursor: pointer; }}
  button:hover {{ background: #e0876a; }}
</style></head>
<body><div class="card"><h1>{html.escape(title)}</h1><p>{message}</p>{button}</div></body></html>"""
    return Response(content=body, media_type="text/html", status_code=status)


def _resolve_unsub_token(token: str | None):
    """(account, email) for a valid token, or (None, None). Accepts a
    tampered/garbage token by returning None rather than raising, so the
    handler can show a friendly page instead of a 500."""
    parsed = auth.verify_unsubscribe_token(token)
    if not parsed:
        return None, None
    account_id, email = parsed
    return accounts_db.get_account(account_id), email


@app.get("/unsubscribe")
def unsubscribe_confirm(t: str = ""):
    account, email = _resolve_unsub_token(t)
    if not account:
        return _unsub_page(
            "Link expired or invalid",
            "We couldn't read this unsubscribe link. If you keep receiving emails, "
            "reply to one and ask to be removed.",
            status=400,
        )
    if suppressions_db.is_suppressed(account["id"], email):
        return _unsub_page("Already unsubscribed", f"<b>{html.escape(email)}</b> is already opted out. No more emails will be sent.")
    return _unsub_page(
        "Confirm unsubscribe",
        f"Stop sending outreach emails to <b>{html.escape(email)}</b>? This can't be undone by you, "
        "but the sender can re-add you on request.",
        token=t,
    )


@app.post("/unsubscribe")
async def unsubscribe_apply(request: Request):
    # Token can arrive as a query param (Gmail's RFC 8058 One-Click POST hits
    # the List-Unsubscribe URL directly, token in the query, body is just
    # "List-Unsubscribe=One-Click") or as a form field (our confirmation page).
    # Parse the urlencoded body by hand rather than request.form(), which would
    # require the python-multipart dependency just for this one field.
    token = request.query_params.get("t", "")
    if not token:
        raw = (await request.body()).decode("utf-8", "replace")
        token = parse_qs(raw).get("t", [""])[0]
    account, email = _resolve_unsub_token(token)
    if not account:
        return _unsub_page(
            "Link expired or invalid",
            "We couldn't process this unsubscribe request. Reply to one of the emails to be removed.",
            status=400,
        )
    suppressions_db.add(account["id"], email, source="unsubscribe_link")
    try:
        drafts_db.cancel_follow_ups_for_email(account["id"], email, "opt_out")
    except Exception as e:
        # The suppression above is the irreversible user promise. Follow-up
        # cleanup is defence in depth; never turn a successful opt-out into an
        # error page because the additive schedule table is unavailable.
        print(f"unsubscribe: follow-up cleanup failed for {email}: {e}")
    # Reflect it in the sheet too, best-effort -- the suppression above is the
    # authoritative stop, so a sheet hiccup must not turn into an error page for
    # someone who just opted out.
    try:
        sheets.mark_unsubscribed(account, email)
    except Exception as e:  # noqa: BLE001 - opt-out must always succeed for the user
        print(f"unsubscribe: sheet update failed for {email}: {e}")
    return _unsub_page("You're unsubscribed", f"<b>{html.escape(email)}</b> won't receive any more emails. Thanks.")


# --- Lead sourcing agent --------------------------------------------------------

class LeadSearchBody(BaseModel):
    query: str
    limit: int = 10


@app.post("/api/leads/search", status_code=202)
def search_leads(request: Request, payload: LeadSearchBody):
    account = _account(request)
    # Per-plan now, not a hardcoded 10 -- the pricing page sells the hourly
    # search rate as a plan feature, so it has to come from the plan.
    searches_per_hour = plans.for_account(account).lead_searches_per_hour
    if not ratelimit.check(f"leads-search:{account['id']}", limit=searches_per_hour, window_seconds=3600):
        raise HTTPException(status_code=429, detail="Too many lead searches this hour. Try again later.")
    # Same reasoning as the prepare endpoint: find_leads enforces this itself
    # (the CLI needs it to), but checking here turns a job that dies on arrival
    # into an immediate 402 that says what to do about it.
    plans.check(account, usage.UNIT_LEAD_SOURCED)
    limit = max(1, min(payload.limit, 25))
    return {"job_id": start_job(account["id"], lambda: leads.find_leads(account, payload.query, limit))}


@app.get("/api/leads")
def list_leads(request: Request):
    account = _account(request)
    return [
        {
            "row": row_index,
            "name": row[sheets.COL_NAME],
            "email": row[sheets.COL_EMAIL],
            "company": row[sheets.COL_COMPANY],
            "reason": row[sheets.COL_LEAD_REASON],
            "emailConfidence": row[sheets.COL_EMAIL_CONFIDENCE],
        }
        for row_index, row in sheets.get_all_rows(account)
        if row[sheets.COL_STATUS] in ("Ready for review", "Needs verification")
    ]


@app.post("/api/leads/{row}/approve")
def approve_lead(request: Request, row: int):
    account = _account(request)
    lead = next((data for index, data in sheets.get_all_rows(account) if index == row), None)
    if not lead or lead[sheets.COL_STATUS] not in ("Ready for review", "Needs verification"):
        raise HTTPException(status_code=409, detail="Lead not found or already actioned.")
    sheets.update_row(account, row, status="", expect_email=lead[sheets.COL_EMAIL])
    return {"ok": True}


@app.post("/api/leads/{row}/discard")
def discard_lead(request: Request, row: int):
    account = _account(request)
    lead = next((data for index, data in sheets.get_all_rows(account) if index == row), None)
    if not lead or lead[sheets.COL_STATUS] not in ("Ready for review", "Needs verification"):
        raise HTTPException(status_code=409, detail="Lead not found or already actioned.")
    sheets.update_row(
        account, row, status="Discarded", expect_email=lead[sheets.COL_EMAIL]
    )
    return {"ok": True}
