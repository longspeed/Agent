"""Safe local Sendkeep demo server.

This server is intentionally separate from ``server.py``. It gives a new
developer a useful queue to inspect without Google OAuth, Supabase, an LLM
provider, or any possibility of sending email to a real address.

Run from the repository root:

    python dev_server.py

Then open http://127.0.0.1:8000/demo. The local API is documented at
http://127.0.0.1:8000/api/docs.
"""

import argparse
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles


ROOT = Path(__file__).parent
DEMO_PAGE = ROOT / "dev" / "demo.html"
STATIC_DIR = ROOT / "static"
CLI_VERSION = "dev"
sys.path.insert(0, str(ROOT / "outreach-agent"))
import sheet_template  # noqa: E402 - credential-free template builder


SEED_QUEUE: list[dict[str, Any]] = [
    {
        "id": "reply-maya-chen",
        "kind": "Warm reply",
        "contact": "Maya Chen",
        "email": "maya@acme.example",
        "company": "Acme Analytics",
        "subject": "Re: reducing reporting time",
        "preview": "This is interesting. Could you send the short version and pricing?",
        "next_step": "Draft a reply grounded in the thread",
        "age": "2 hours ago",
        "status": "Needs review",
    },
    {
        "id": "follow-up-thomas-lee",
        "kind": "Promised follow-up",
        "contact": "Thomas Lee",
        "email": "thomas@northstar.example",
        "company": "Northstar Labs",
        "subject": "Re: onboarding workflow",
        "preview": "I will send the current process map on Friday.",
        "next_step": "Confirm the promise and keep the date visible",
        "age": "Due today",
        "status": "Needs review",
    },
    {
        "id": "reply-priya-shah",
        "kind": "Reply draft",
        "contact": "Priya Shah",
        "email": "priya@orbit.example",
        "company": "Orbit Systems",
        "subject": "Re: finance team workflow",
        "preview": "Looping in our operations lead. Can you follow up next week?",
        "next_step": "Review the drafted response before sending",
        "age": "Yesterday",
        "status": "Needs review",
    },
]


class DemoState:
    """Small in-memory state store. Restarting the server restores the seed."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.queue = deepcopy(SEED_QUEUE)

    def snapshot(self) -> dict[str, Any]:
        pending = [item for item in self.queue if item["status"] != "Reviewed"]
        return {
            "mode": "local-demo",
            "safe_to_send": False,
            "account": {
                "name": "Demo operator",
                "email": "operator@sendkeep.local",
                "gmail_connected": False,
                "provider": "Fake Gmail",
            },
            "summary": {
                "needs_review": len(pending),
                "promised_follow_ups": sum(
                    item["kind"] == "Promised follow-up" for item in pending
                ),
                "sent": 0,
            },
            "queue": deepcopy(self.queue),
        }

    def review(self, item_id: str) -> dict[str, Any]:
        for item in self.queue:
            if item["id"] == item_id:
                item["status"] = "Reviewed"
                return deepcopy(item)
        raise KeyError(item_id)


state = DemoState()
app = FastAPI(
    title="Sendkeep local demo API",
    version=CLI_VERSION,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "landing" / "index.html")


@app.get("/getting-started", include_in_schema=False)
def getting_started() -> FileResponse:
    return FileResponse(STATIC_DIR / "app" / "getting-started.html")


@app.get("/demo", include_in_schema=False)
def demo_page() -> FileResponse:
    return FileResponse(DEMO_PAGE)


@app.get("/signup", include_in_schema=False)
@app.get("/login", include_in_schema=False)
def local_auth_redirect() -> RedirectResponse:
    """Keep marketing auth links useful in credential-free local mode.

    The demo server intentionally has no Google OAuth or account database. A
    landing-page CTA must still have a safe destination, though; returning a
    raw 404 makes the local experience look broken and encourages developers
    to infer that OAuth is configured when it is not.
    """
    return RedirectResponse("/demo", status_code=303)


@app.get("/health", summary="Check that the local demo is running")
def health() -> dict[str, str | bool]:
    return {"ok": True, "mode": "local-demo", "safe_to_send": False}


@app.get("/api/demo/state", summary="Read the seeded local queue")
def demo_state() -> dict[str, Any]:
    return state.snapshot()


@app.get("/api/plan", summary="Read local demo limits")
def demo_plan() -> dict[str, Any]:
    return {
        "name": "Local demo",
        "daily_send_limit": 25,
        "lead_searches_per_hour": 10,
        "reply_drafts_per_month": 10,
    }


@app.get("/api/lead-sheet-template.xlsx", include_in_schema=False)
def lead_sheet_template_xlsx() -> Response:
    """Serve the same safe, example-only workbook as the production guide."""
    return Response(
        content=sheet_template.build_xlsx(),
        media_type=sheet_template.MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{sheet_template.FILENAME}"'
        },
    )


@app.get("/api/lead-sheet-template.csv", include_in_schema=False)
def lead_sheet_template_csv() -> Response:
    """Serve the safe CSV starter without loading production configuration."""
    return Response(
        content=sheet_template.build_csv(),
        media_type=sheet_template.CSV_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{sheet_template.CSV_FILENAME}"'
        },
    )


@app.post("/api/demo/queue/{item_id}/review", summary="Mark a local queue item reviewed")
def review_item(item_id: str) -> dict[str, Any]:
    try:
        item = state.review(item_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Demo queue item not found") from exc
    return {"ok": True, "item": item, "state": state.snapshot()}


@app.post("/api/demo/reset", summary="Restore the seeded local queue")
def reset_demo() -> dict[str, Any]:
    state.reset()
    return {"ok": True, "state": state.snapshot()}


@app.post("/api/demo/send", summary="Prove that local demo mode cannot send")
def send_demo_email() -> None:
    raise HTTPException(
        status_code=409,
        detail=(
            "Local demo mode never sends email. Connect real Gmail through "
            "server.py in a configured environment before sending anything."
        ),
    )


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Start the safe, credential-free Sendkeep local demo."
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address")
    parser.add_argument("--port", type=int, default=8000, help="bind port")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {CLI_VERSION}",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_cli_parser().parse_args(argv)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
