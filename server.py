import html
import os
import posixpath
import sys
import threading
import uuid
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "outreach-agent"))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from googleapiclient.errors import HttpError
from pydantic import BaseModel

import accounts_db
import agent
import auth
import drafts_db
import drive
import gmail
import google_auth
import leads
import ratelimit
import reviews_db
import send_outreach
import sheet_template
import sheets
import suppressions_db
import usage
import watch_replies

# FastAPI's auto-docs are off unless explicitly enabled. Left on, /docs, /redoc
# and /openapi.json hand every logged-in tenant an interactive map of all 33
# endpoints with request/response schemas. Per-endpoint auth still applies, so
# this is disclosure rather than a hole, but a multi-tenant app has no reason to
# publish its own blueprint. /docs also collided with the marketing site's
# "Docs" link, so customers clicking for help landed in Swagger UI.
# Set ENABLE_API_DOCS=1 locally when you want them.
_API_DOCS = os.environ.get("ENABLE_API_DOCS", "").strip().lower() in ("1", "true", "yes")

app = FastAPI(
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

# The getting-started guide is the product documentation the marketing site
# links to as "Docs", so a prospect has to be able to read it before signing up.
PUBLIC_PATHS.add("/getting-started")

# The one-click opt-out endpoint. It MUST be reachable with no session: the
# person clicking it is an email recipient, never a logged-in user of this app.
# Authenticity comes from the signed token in the URL, not from a cookie.
PUBLIC_PATHS.add("/unsubscribe")

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


@app.middleware("http")
async def require_auth(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS or _is_public_asset(request.url.path):
        return await call_next(request)
    account_id = auth.verify_session_token(request.cookies.get(auth.COOKIE_NAME))
    if not account_id:
        if request.url.path == "/":
            # Logged-out visitors see the marketing landing page; the app
            # home (agents) stays behind auth.
            return FileResponse(STATIC_DIR / "landing" / "index.html")
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return RedirectResponse(f"/login?next={request.url.path}")
    request.state.account_id = account_id
    return await call_next(request)


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


# Set SECURE_COOKIES=1 (or true/yes) when serving over HTTPS (e.g. behind the
# cloudflared tunnel) so session cookies are never sent over plain http.
# Parsed explicitly -- bool(os.environ.get(...)) would treat "0" and "false"
# as ON, and the resulting Secure cookie over plain http fails as a silent
# login loop with nothing in any log. Read at call time so tests (and a
# restarted tunnel setup) see env changes without a module reload.
def _secure_cookies() -> bool:
    return os.environ.get("SECURE_COOKIES", "").strip().lower() in ("1", "true", "yes")


# Behind the Cloudflare tunnel every request reaches uvicorn from 127.0.0.1
# (the cloudflared process), so request.client.host collapses every visitor
# into one bucket -- making the per-IP rate limits on /login and /signup
# global. Anyone could then lock every customer out with 10 login attempts.
# Cloudflare's edge sets CF-Connecting-IP to the real client IP and overwrites
# any value the client tries to send, so it is authoritative here. Fall back
# to the socket peer for local/dev where the header is absent (matching the
# pre-tunnel behavior, so tests that don't set the header are unaffected).
# X-Forwarded-For is deliberately NOT trusted: without a known proxy chain it
# is client-spoofable, which would let an attacker bypass the limiter entirely
# by rotating fake IPs -- strictly worse than the shared-bucket bug.
def _client_ip(request: Request) -> str:
    cf_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_ip:
        return cf_ip
    return request.client.host if request.client else "unknown"


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


def _session_response(payload: dict, account_id: str, status_code: int = 200) -> JSONResponse:
    return _set_session_cookie(JSONResponse(payload, status_code=status_code), account_id)


# --- Pages -------------------------------------------------------------------

@app.get("/")
def home():
    return FileResponse(STATIC_DIR / "index.html")


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
    return FileResponse(STATIC_DIR / "signup.html")


# --- Accounts & sessions -------------------------------------------------------

class CredentialsBody(BaseModel):
    email: str
    password: str


@app.post("/signup", status_code=201)
def signup_submit(request: Request, payload: CredentialsBody):
    if not ratelimit.check(f"signup:{_client_ip(request)}", limit=5, window_seconds=3600):
        raise HTTPException(status_code=429, detail="Too many signup attempts. Try again later.")
    email = payload.email.strip().lower()
    if "@" not in email or "." not in email.partition("@")[2]:
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    try:
        account = accounts_db.create_account(email, auth.hash_password(payload.password))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _session_response({"ok": True}, account["id"], status_code=201)


@app.post("/login")
def login_submit(request: Request, payload: CredentialsBody):
    if not ratelimit.check(f"login:{_client_ip(request)}", limit=10, window_seconds=300):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again in a few minutes.")
    account = accounts_db.get_account_by_email(payload.email.strip().lower())
    if account and not account.get("password_hash"):
        raise HTTPException(status_code=401, detail='This account signs in with Google — use "Continue with Google."')
    if not account or not auth.verify_password(payload.password, account["password_hash"]):
        raise HTTPException(status_code=401, detail="Wrong email or password")
    return _session_response({"ok": True}, account["id"])


@app.post("/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE_NAME)
    return response


@app.get("/auth/google/login")
def google_login_start():
    return RedirectResponse(google_auth.build_login_auth_url(auth.create_oauth_state()))


@app.get("/auth/google/callback")
def google_login_callback(state: str = "", code: str = "", error: str = ""):
    if error:
        return RedirectResponse(f"/login?google_error={error}")
    if not auth.verify_oauth_state(state):
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
    try:
        identity = google_auth.exchange_login_code(code)
    except Exception:
        return RedirectResponse("/login?google_error=auth_failed")

    try:
        account = accounts_db.link_or_create_google_account(
            identity["email"].lower(), identity["google_id"]
        )
    except accounts_db.AccountLinkBlocked:
        # This email already has a password account (unverified). Don't silently
        # merge -- send them to sign in with their password (see AccountLinkBlocked).
        return RedirectResponse("/login?google_error=account_exists")
    onboarded = bool(account.get("google_token")) and bool((account.get("google_sheet_id") or "").strip())
    return _set_session_cookie(RedirectResponse("/" if onboarded else "/settings"), account["id"])


@app.get("/api/me")
def me(request: Request):
    account = _account(request)
    return {
        "email": account["email"],
        "googleConnected": bool(account.get("google_token")),
        "settings": {key: account.get(key) for key in accounts_db.EDITABLE_SETTINGS},
        "onboarded": bool(account.get("google_token"))
        and bool((account.get("google_sheet_id") or "").strip())
        and bool((account.get("calendar_booking_link") or "").strip()),
    }


class SettingsBody(BaseModel):
    sender_name: str | None = None
    sender_company: str | None = None
    meeting_purpose: str | None = None
    custom_instructions: str | None = None
    calendar_booking_link: str | None = None
    notify_email: str | None = None
    google_sheet_id: str | None = None


@app.put("/api/settings")
def update_settings(request: Request, payload: SettingsBody):
    account = accounts_db.update_settings(
        request.state.account_id, payload.model_dump(exclude_none=True)
    )
    return {key: account.get(key) for key in accounts_db.EDITABLE_SETTINGS}


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


@app.get("/api/plan")
def get_plan(request: Request):
    """Validation-plan pricing and hard product limits."""
    _account(request)
    return {
        "name": "Pilot",
        "price_monthly_usd": 49,
        "daily_send_limit": sheets.DAILY_SEND_LIMIT,
        "lead_searches_per_hour": 10,
        "note": "Human-approved leads, human-reviewed emails and replies, one-click opt-out, daily send cap. Checkout is coming next.",
    }


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
def google_connect(request: Request):
    state = auth.create_session_token(request.state.account_id, OAUTH_STATE_TTL_SECONDS)
    return RedirectResponse(google_auth.build_auth_url(state))


@app.get("/api/google/callback")
def google_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    if error:
        return RedirectResponse(f"/settings?google_error={error}")
    # The signed state must match the logged-in account (CSRF protection).
    state_account_id = auth.verify_session_token(state)
    if not state_account_id or state_account_id != request.state.account_id:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    token_json = google_auth.exchange_code(code)
    accounts_db.set_google_token(state_account_id, token_json)
    return RedirectResponse("/settings?connected=1")


@app.post("/api/google/disconnect")
def google_disconnect(request: Request):
    accounts_db.set_google_token(request.state.account_id, None)
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --- Background jobs -------------------------------------------------------
# Long-running agent actions (send a batch, poll for replies) run on a
# background thread so the request returns immediately; the frontend polls
# /api/jobs/{id} for the result instead of blocking on the original request.

JOBS: dict[str, dict] = {}

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
        JOBS[job_id] = {"account_id": account_id, "status": "done", "result": result, "error": None}
    except RuntimeError as e:
        # RuntimeError is the app's convention for hand-written, user-safe
        # messages (see runtime_error_handler) -- surface it to the client.
        JOBS[job_id] = {"account_id": account_id, "status": "error", "result": None, "error": str(e)}
    except Exception as e:
        # Anything else is unexpected: a raw str(e) here would ship internal
        # details straight to the frontend (Google API JSON payloads, Supabase
        # errors, KeyErrors). Log the real cause server-side, return a generic
        # message.
        print(f"Job {job_id} (account {account_id}) failed unexpectedly: {e!r}")
        JOBS[job_id] = {
            "account_id": account_id, "status": "error", "result": None,
            "error": "Something went wrong on our end. Please try again.",
        }


def start_job(account_id, fn) -> str:
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"account_id": account_id, "status": "running", "result": None, "error": None}
    threading.Thread(target=_run_job, args=(job_id, account_id, fn), daemon=True).start()
    return job_id


@app.get("/api/jobs/{job_id}")
def get_job(request: Request, job_id: str):
    job = JOBS.get(job_id)
    # A job is only visible to the account that started it.
    if not job or job["account_id"] != request.state.account_id:
        raise HTTPException(status_code=404, detail="Job not found")
    return {k: v for k, v in job.items() if k != "account_id"}


# --- Outreach agent -----------------------------------------------------------

@app.get("/api/outreach/campaigns")
def list_campaigns(request: Request):
    account = _account(request)
    rows = sheets.get_all_rows(account)
    return [
        {
            "row": row_index,
            "name": row[sheets.COL_NAME],
            "email": row[sheets.COL_EMAIL],
            "company": row[sheets.COL_COMPANY],
            "status": row[sheets.COL_STATUS] or "Pending",
            "sentAt": row[sheets.COL_SENT_AT],
            "verification": row[sheets.COL_EMAIL_CONFIDENCE] or "unverified",
        }
        for row_index, row in rows
        if row[sheets.COL_EMAIL].strip()
    ]


def _campaign_preview(account: dict) -> dict:
    suppressed = suppressions_db.list_suppressed_emails(account["id"])
    readiness = sheets.campaign_readiness(account, suppressed_emails=suppressed)
    return {
        "eligible": len(readiness["eligible"]),
        "eligible_total": readiness["eligible_total"],
        "sent_today": readiness["sent_today"],
        "daily_limit": readiness["daily_limit"],
        "remaining_today": readiness["remaining_today"],
        "capped": readiness["capped"],
        # Settings that must be filled before anything can be drafted. When
        # non-empty the UI disables the prepare button and shows why, rather
        # than letting a batch fail every row at generation time.
        "blockers": agent.account_send_blockers(account),
    }


@app.get("/api/outreach/campaigns/preview")
def campaign_preview(request: Request):
    return _campaign_preview(_account(request))


class PrepareCampaignBody(BaseModel):
    confirmed: bool = False


@app.post("/api/outreach/campaigns/prepare", status_code=202)
def prepare_campaigns(request: Request, payload: PrepareCampaignBody):
    """Generate + validate an email per eligible contact and queue them for
    review. Nothing is emailed here; approval happens per-draft below."""
    account = _account(request)
    if not ratelimit.check(f"batch-prepare:{account['id']}", limit=5, window_seconds=3600):
        raise HTTPException(status_code=429, detail="Too many draft batches this hour. Try again later.")
    preview = _campaign_preview(account)
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="Review the batch and confirm before preparing drafts.")
    if preview["blockers"]:
        raise HTTPException(status_code=409, detail=" ".join(preview["blockers"]))
    if not preview["eligible"]:
        raise HTTPException(status_code=409, detail="No approved contacts are available to draft today. Approve leads on the Lead Agent page or wait for the daily limit to reset.")

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
            return send_outreach.prepare_drafts(account)
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
    return drafts_db.list_pending_drafts(request.state.account_id)


class SendDraftBody(BaseModel):
    subject: str
    body: str


def _guard_draft_send(account: dict, draft: dict):
    """Shared pre-send checks for a single approved draft. Both run at SEND
    time, not draft time: the cap can be reached and an address can opt out
    between preparing a batch and approving it."""
    if suppressions_db.is_suppressed(account["id"], draft["email"]):
        drafts_db.discard(account["id"], draft["id"])
        raise HTTPException(status_code=409, detail=f"{draft['email']} unsubscribed after this draft was prepared; it won't be sent.")
    readiness = sheets.campaign_readiness(account, suppressed_emails=suppressions_db.list_suppressed_emails(account["id"]))
    if readiness["remaining_today"] <= 0:
        raise HTTPException(status_code=409, detail="Daily send limit reached. Try again after it resets.")


@app.post("/api/outreach/drafts/{draft_id}/send")
def send_draft(request: Request, draft_id: int, payload: SendDraftBody):
    account = _account(request)
    draft = drafts_db.get_draft(account["id"], draft_id)
    if not draft or draft["status"] != "pending":
        raise HTTPException(status_code=404, detail="Draft not found or already handled")
    _guard_draft_send(account, draft)

    _, sent_body = send_outreach.send_prepared_draft(
        account, draft, subject=payload.subject, body=payload.body
    )
    drafts_db.mark_sent(account["id"], draft_id, payload.subject, sent_body)
    return {"ok": True}


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


@app.get("/api/outreach/replies")
def list_replies(request: Request):
    return reviews_db.list_pending_reviews(request.state.account_id)


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
    body: str


@app.post("/api/outreach/replies/{review_id}/send")
def send_reply(request: Request, review_id: int, payload: SendReplyBody):
    account = _account(request)
    review = reviews_db.get_review(account["id"], review_id)
    if not review or review["status"] != "pending":
        raise HTTPException(status_code=404, detail="Review not found or already handled")

    gmail.send_reply(account, review["thread_id"], review["email"], payload.body)
    reviews_db.mark_sent(account["id"], review_id, payload.body)
    return {"ok": True}


@app.post("/api/outreach/replies/{review_id}/dismiss")
def dismiss_reply(request: Request, review_id: int):
    account = _account(request)
    review = reviews_db.get_review(account["id"], review_id)
    if not review or review["status"] != "pending":
        raise HTTPException(status_code=404, detail="Review not found or already handled")

    reviews_db.dismiss(account["id"], review_id)
    return {"ok": True}


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
    if not ratelimit.check(f"leads-search:{account['id']}", limit=10, window_seconds=3600):
        raise HTTPException(status_code=429, detail="Too many lead searches this hour. Try again later.")
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
    sheets.update_row(account, row, status="")
    return {"ok": True}


@app.post("/api/leads/{row}/discard")
def discard_lead(request: Request, row: int):
    sheets.update_row(_account(request), row, status="Discarded")
    return {"ok": True}
