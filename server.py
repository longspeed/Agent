import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "outreach-agent"))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import auth
import gmail
import leads
import reviews_db
import send_outreach
import sheets
import watch_replies

app = FastAPI()

STATIC_DIR = ROOT / "static"

PUBLIC_PATHS = {"/login", "/logout"}


@app.middleware("http")
async def require_auth(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    if not auth.verify_session_token(request.cookies.get(auth.COOKIE_NAME)):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return RedirectResponse(f"/login?next={request.url.path}")
    return await call_next(request)


@app.get("/")
def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/outreach")
def outreach_page():
    return FileResponse(STATIC_DIR / "outreach.html")


@app.get("/leads")
def leads_page():
    return FileResponse(STATIC_DIR / "leads.html")


@app.get("/login")
def login_page():
    return FileResponse(STATIC_DIR / "login.html")


class LoginBody(BaseModel):
    password: str


@app.post("/login")
def login_submit(payload: LoginBody):
    if not auth.check_password(payload.password):
        raise HTTPException(status_code=401, detail="Wrong password")
    response = JSONResponse({"ok": True})
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.create_session_token(),
        httponly=True,
        samesite="lax",
        max_age=auth.SESSION_TTL_SECONDS,
    )
    return response


@app.post("/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE_NAME)
    return response


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --- Background jobs -------------------------------------------------------
# Long-running agent actions (send a batch, poll for replies) run on a
# background thread so the request returns immediately; the frontend polls
# /api/jobs/{id} for the result instead of blocking on the original request.

JOBS: dict[str, dict] = {}


def _run_job(job_id, fn):
    try:
        JOBS[job_id] = {"status": "done", "result": fn(), "error": None}
    except Exception as e:
        JOBS[job_id] = {"status": "error", "result": None, "error": str(e)}


def start_job(fn) -> str:
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "running", "result": None, "error": None}
    threading.Thread(target=_run_job, args=(job_id, fn), daemon=True).start()
    return job_id


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/outreach/campaigns")
def list_campaigns():
    rows = sheets.get_all_rows()
    return [
        {
            "row": row_index,
            "name": row[sheets.COL_NAME],
            "email": row[sheets.COL_EMAIL],
            "company": row[sheets.COL_COMPANY],
            "status": row[sheets.COL_STATUS] or "Pending",
            "sentAt": row[sheets.COL_SENT_AT],
        }
        for row_index, row in rows
        if row[sheets.COL_EMAIL].strip()
    ]


@app.post("/api/outreach/campaigns/send", status_code=202)
def send_campaigns():
    return {"job_id": start_job(send_outreach.main)}


@app.post("/api/outreach/replies/check", status_code=202)
def check_replies():
    return {"job_id": start_job(watch_replies.check_for_replies)}


@app.get("/api/outreach/replies")
def list_replies():
    return reviews_db.list_pending_reviews()


@app.post("/api/outreach/campaigns/{row}/check-reply")
def check_single_reply(row: int):
    """Lightweight per-contact check: has this one person replied yet?
    Read-only — unlike /api/outreach/replies/check, it doesn't draft a
    response, touch the sheet, or send a notification."""
    match = next((r for idx, r in sheets.get_all_rows() if idx == row), None)
    if not match:
        raise HTTPException(status_code=404, detail="Row not found")

    thread_id = match[sheets.COL_THREAD_ID].strip()
    if not thread_id:
        raise HTTPException(status_code=400, detail="No outreach sent to this contact yet")

    reply_text = gmail.get_latest_reply(thread_id)
    return {"replied": bool(reply_text), "replyText": reply_text}


class SendReplyBody(BaseModel):
    body: str


@app.post("/api/outreach/replies/{review_id}/send")
def send_reply(review_id: int, payload: SendReplyBody):
    review = reviews_db.get_review(review_id)
    if not review or review["status"] != "pending":
        raise HTTPException(status_code=404, detail="Review not found or already handled")

    gmail.send_reply(review["thread_id"], review["email"], payload.body)
    reviews_db.mark_sent(review_id, payload.body)
    return {"ok": True}


class LeadSearchBody(BaseModel):
    query: str
    limit: int = 10


@app.post("/api/leads/search", status_code=202)
def search_leads(payload: LeadSearchBody):
    limit = max(1, min(payload.limit, 25))
    return {"job_id": start_job(lambda: leads.find_leads(payload.query, limit))}


@app.get("/api/leads")
def list_leads():
    return [
        {
            "row": row_index,
            "name": row[sheets.COL_NAME],
            "email": row[sheets.COL_EMAIL],
            "company": row[sheets.COL_COMPANY],
            "reason": row[sheets.COL_LEAD_REASON],
            "emailConfidence": row[sheets.COL_EMAIL_CONFIDENCE],
        }
        for row_index, row in sheets.get_all_rows()
        if row[sheets.COL_STATUS] == "New Lead"
    ]


@app.post("/api/leads/{row}/approve")
def approve_lead(row: int):
    sheets.update_row(row, status="")
    return {"ok": True}


@app.post("/api/leads/{row}/discard")
def discard_lead(row: int):
    sheets.update_row(row, status="Discarded")
    return {"ok": True}
