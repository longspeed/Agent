import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "outreach-agent"))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from googleapiclient.errors import HttpError
from pydantic import BaseModel

import accounts_db
import auth
import drive
import email_verification
import gmail
import google_auth
import leads
import ratelimit
import reviews_db
import send_outreach
import sheets
import usage
import watch_replies

app = FastAPI()

STATIC_DIR = ROOT / "static"

PUBLIC_PATHS = {"/login", "/signup", "/logout", "/auth/google/login", "/auth/google/callback"}


@app.middleware("http")
async def require_auth(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    account_id = auth.verify_session_token(request.cookies.get(auth.COOKIE_NAME))
    if not account_id:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return RedirectResponse(f"/login?next={request.url.path}")
    request.state.account_id = account_id
    return await call_next(request)


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


def _session_response(payload: dict, account_id: str, status_code: int = 200) -> JSONResponse:
    response = JSONResponse(payload, status_code=status_code)
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.create_session_token(account_id),
        httponly=True,
        samesite="lax",
        max_age=auth.SESSION_TTL_SECONDS,
    )
    return response


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
    return FileResponse(STATIC_DIR / "settings.html")


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
    if not ratelimit.check(f"signup:{request.client.host}", limit=5, window_seconds=3600):
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
    if not ratelimit.check(f"login:{request.client.host}", limit=10, window_seconds=300):
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

    account = accounts_db.link_or_create_google_account(identity["email"].lower(), identity["google_id"])
    onboarded = bool(account.get("google_token")) and bool((account.get("google_sheet_id") or "").strip())
    response = RedirectResponse("/" if onboarded else "/settings")
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.create_session_token(account["id"]),
        httponly=True,
        samesite="lax",
        max_age=auth.SESSION_TTL_SECONDS,
    )
    return response


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
    meeting_purpose: str | None = None
    calendar_booking_link: str | None = None
    notify_email: str | None = None
    google_sheet_id: str | None = None


@app.put("/api/settings")
def update_settings(request: Request, payload: SettingsBody):
    account = accounts_db.update_settings(
        request.state.account_id, payload.model_dump(exclude_none=True)
    )
    return {key: account.get(key) for key in accounts_db.EDITABLE_SETTINGS}


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
        "note": "Human-approved, verified-email outreach. Checkout is coming next.",
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


def _run_job(job_id, account_id, fn):
    try:
        result = usage.run_as(account_id, fn)
        JOBS[job_id] = {"account_id": account_id, "status": "done", "result": result, "error": None}
    except Exception as e:
        JOBS[job_id] = {"account_id": account_id, "status": "error", "result": None, "error": str(e)}


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
    readiness = sheets.campaign_readiness(account)
    return {
        "eligible": len(readiness["eligible"]),
        "eligible_total": readiness["eligible_total"],
        "needs_verification": len(readiness["unverified"]),
        "sent_today": readiness["sent_today"],
        "daily_limit": readiness["daily_limit"],
        "remaining_today": readiness["remaining_today"],
        "capped": readiness["capped"],
        "verification_configured": email_verification.is_configured(),
    }


@app.get("/api/outreach/campaigns/preview")
def campaign_preview(request: Request):
    return _campaign_preview(_account(request))


class SendCampaignBody(BaseModel):
    confirmed: bool = False


@app.post("/api/outreach/campaigns/send", status_code=202)
def send_campaigns(request: Request, payload: SendCampaignBody):
    account = _account(request)
    if not ratelimit.check(f"batch-send:{account['id']}", limit=5, window_seconds=3600):
        raise HTTPException(status_code=429, detail="Too many send batches this hour. Try again later.")
    preview = _campaign_preview(account)
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="Review the batch and confirm before sending.")
    if not preview["verification_configured"]:
        raise HTTPException(status_code=409, detail="Email verification is not configured. Add NEVERBOUNCE_API_KEY before sending.")
    if not preview["eligible"]:
        raise HTTPException(status_code=409, detail="No verified recipients are available to send today. Verify pending emails or wait for the daily limit to reset.")
    return {"job_id": start_job(account["id"], lambda: send_outreach.main(account))}


@app.post("/api/outreach/campaigns/verify", status_code=202)
def verify_pending_emails(request: Request):
    account = _account(request)
    if not email_verification.is_configured():
        raise HTTPException(status_code=409, detail="Email verification is not configured. Add NEVERBOUNCE_API_KEY first.")
    candidates = [
        (row_index, row) for row_index, row in sheets.get_all_rows(account)
        if row[sheets.COL_EMAIL].strip() and (
            not row[sheets.COL_STATUS].strip() or row[sheets.COL_STATUS].strip() == "Needs verification"
        ) and not sheets.is_verified(row)
    ]

    def run():
        verified = invalid = 0
        for row_index, row in candidates:
            state = email_verification.verify(row[sheets.COL_EMAIL].strip())
            if state == "verified":
                sheets.update_row(account, row_index, status="", email_confidence=state)
                verified += 1
            elif state == "invalid":
                sheets.update_row(account, row_index, status="Needs verification", email_confidence=state)
                invalid += 1
        return {"checked": len(candidates), "verified": verified, "needs_verification": len(candidates) - verified, "invalid": invalid}

    return {"job_id": start_job(account["id"], run)}


@app.post("/api/outreach/replies/check", status_code=202)
def check_replies(request: Request):
    account = _account(request)
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

    reply_text = gmail.get_latest_reply(account, thread_id)
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
    if not lead or lead[sheets.COL_STATUS] != "Ready for review" or not sheets.is_verified(lead):
        raise HTTPException(status_code=409, detail="Only provider-verified leads can be approved for outreach.")
    sheets.update_row(account, row, status="")
    return {"ok": True}


@app.post("/api/leads/{row}/discard")
def discard_lead(request: Request, row: int):
    sheets.update_row(_account(request), row, status="Discarded")
    return {"ok": True}
