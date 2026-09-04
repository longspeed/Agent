"""Regression contracts for issues found by /qa on 2026-08-17.

Report: qa-reports/qa-report-daytonaproxy01-net-2026-08-17.md
"""
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
for name, value in {
    "OPENROUTER_API_KEY": "test-key",
    "APP_SECRET_KEY": "test-secret",
    "SUPABASE_URL": "http://localhost",
    "SUPABASE_SECRET_KEY": "test-secret-key",
}.items():
    os.environ.setdefault(name, value)

sys.path.insert(0, str(ROOT / "outreach-agent"))
sys.path.insert(0, str(ROOT))

import auth  # noqa: E402
import server  # noqa: E402


def test_logged_out_app_page_redirects_to_login(monkeypatch):
    """Regression: ISSUE-005 — /settings rendered a blank page after logout."""
    monkeypatch.setattr(auth, "verify_session_token", lambda token: None)
    response = TestClient(server.app).get("/settings", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/settings"
    assert response.headers["cache-control"] == "private, no-store"


def test_logged_out_tab_link_preserves_the_query_for_post_login_routing(monkeypatch):
    """A tab deep link must survive the auth redirect and OAuth round trip."""
    monkeypatch.setattr(auth, "verify_session_token", lambda token: None)
    response = TestClient(server.app).get(
        "/outreach?tab=threads", follow_redirects=False
    )

    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == "/login"
    assert parse_qs(location.query)["next"] == ["/outreach?tab=threads"]


def test_public_sheet_templates_do_not_require_a_session(monkeypatch):
    monkeypatch.setattr(auth, "verify_session_token", lambda token: None)
    client = TestClient(server.app)

    xlsx = client.get("/api/lead-sheet-template.xlsx")
    assert xlsx.status_code == 200
    assert xlsx.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "attachment" in xlsx.headers["content-disposition"]
    assert xlsx.content[:2] == b"PK"

    csv = client.get("/api/lead-sheet-template.csv")
    assert csv.status_code == 200
    assert csv.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv.headers["content-disposition"]
    assert "Name,Email,Company" in csv.text


def test_safe_next_keeps_tabs_but_rejects_external_targets():
    assert server._safe_next("/outreach?tab=threads") == "/outreach?tab=threads"
    for unsafe in ("//evil.example", "https://evil.example", r"\evil.example", ""):
        assert server._safe_next(unsafe) is None


def test_login_page_is_styled_without_external_tailwind_or_font_requests():
    html = (ROOT / "static" / "login.html").read_text(encoding="utf-8")
    assert "cdn.tailwindcss.com" not in html
    assert "fonts.googleapis.com" not in html
    assert "URLSearchParams" in html
    assert "encodeURIComponent(next)" in html


def test_landing_promise_cta_and_demo_are_honest_and_responsive():
    pricing = (ROOT / "site" / "src" / "components" / "pricing.tsx").read_text(
        encoding="utf-8"
    )
    mockup = (ROOT / "site" / "src" / "components" / "mockup.tsx").read_text(
        encoding="utf-8"
    )
    css = (ROOT / "site" / "src" / "index.css").read_text(encoding="utf-8")

    inbox = pricing.split("name: 'Inbox'", 1)[1].split("name: 'Agency pilot'", 1)[0]
    assert "cta: 'See your promises'" in inbox
    assert "href: '/outreach?tab=promises'" in inbox
    sections = (ROOT / "site" / "src" / "components" / "sections.tsx").read_text(
        encoding="utf-8"
    )
    assert "'/outreach?tab=inbox'" not in sections
    assert "'/outreach?tab=promises'" in sections
    assert "<button" not in mockup
    assert "Example only" in mockup
    assert "do not change Gmail" in mockup
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css
    assert "@media (max-width: 640px)" in css


def test_logged_out_unknown_page_reaches_the_404_handler(monkeypatch):
    """A typo should not look like an authentication failure."""
    monkeypatch.setattr(auth, "verify_session_token", lambda token: None)
    response = TestClient(server.app).get(
        "/definitely-missing",
        headers={"accept": "text/html"},
        follow_redirects=False,
    )

    assert response.status_code == 404
    assert "Page not found" in response.text


def test_root_auth_variants_are_not_cacheable(monkeypatch):
    """Regression: ISSUE-001 — cached marketing HTML survived Google sign-in."""
    client = TestClient(server.app)
    monkeypatch.setattr(auth, "verify_session_token", lambda token: None)
    public = client.get("/")
    assert public.headers["cache-control"] == "private, no-store"
    assert public.headers["vary"] == "Cookie"

    monkeypatch.setattr(auth, "verify_session_token", lambda token: "acct-1")
    private = client.get("/")
    assert private.headers["cache-control"] == "private, no-store"
    assert private.headers["vary"] == "Cookie"
    assert private.content != public.content


def test_follow_up_migration_contains_every_runtime_column():
    """Regression: ISSUE-002/003 — production missed part of the grouped schema."""
    sql = (ROOT / "supabase" / "migrations" /
           "20260827000000_sendkeep_baseline.sql").read_text(encoding="utf-8").lower()
    for column in (
        "outreach_send_mode", "follow_up_delay_days", "thread_id",
        "follow_up_due_at", "follow_up_status", "follow_up_cancel_reason",
        "follow_up_sent_at", "follow_up_replied_at", "kind", "source_draft_id",
    ):
        assert f"{column}" in sql
    assert "notify pgrst, 'reload schema'" in sql


def test_durable_tables_have_client_boundary_and_event_dedupe():
    sql = (ROOT / "supabase" / "migrations" /
           "20260827000000_sendkeep_baseline.sql").read_text(encoding="utf-8").lower()
    for table in ("commitments", "tracked_threads", "worker_runs", "worker_events"):
        assert f"public.{table}" in sql
    assert "alter table public.%i enable row level security" in sql
    assert "revoke all on table public.%i from anon, authenticated" in sql
    assert "dedupe_key" in sql
    assert "public.reviews" in sql
    assert "sent_at timestamptz" in sql
    assert "worker_events_account_dedupe_unique_idx" in sql
    assert "worker_events_run_dedupe_unique_idx" in sql
    for index in (
        "worker_events_account_dedupe_unique_idx",
        "worker_events_run_dedupe_unique_idx",
    ):
        definition = sql.split(f"create unique index if not exists {index}", 1)[1].split(";", 1)[0]
        assert " where " not in definition, "upsert conflict targets cannot use partial indexes"


def test_reply_dedupe_index_repair_is_unique_partial_and_data_preserving():
    """An older non-unique index with the canonical name must be repaired."""
    sql = (ROOT / "supabase" / "migrations" /
           "20260831020000_repair_reviews_thread_message_index.sql").read_text(
               encoding="utf-8"
           ).lower()
    assert "having count(*) > 1" in sql
    assert "drop index if exists public.reviews_thread_message_idx" in sql
    assert "create unique index reviews_thread_message_idx" in sql
    assert "where gmail_message_id is not null" in sql
    assert "delete from" not in sql
    assert "notify pgrst, 'reload schema'" in sql


def test_supabase_migration_versions_are_unique():
    migrations = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
    versions = [path.name.split("_", 1)[0] for path in migrations]
    duplicates = sorted({version for version in versions if versions.count(version) > 1})
    assert duplicates == [], f"duplicate migration prefixes: {duplicates}"


def test_atomic_send_contract_migration_fails_closed_without_rewriting_customer_data():
    sql = (ROOT / "supabase" / "migrations" /
           "20260901000000_atomic_send_contract.sql").read_text(
               encoding="utf-8"
           ).lower()
    assert "having count(*) > 1" in sql
    assert "raise exception" in sql
    assert "outreach_drafts_first_touch_email_unique_idx" in sql
    assert "status in ('pending', 'sending', 'send_uncertain', 'sent')" in sql
    assert "delete from" not in sql
    assert "update public.outreach_drafts" not in sql
    assert "revoke all on function public.atomic_send_contract()" in sql
    assert "notify pgrst, 'reload schema'" in sql


def test_public_copy_matches_auto_send_and_has_no_launch_placeholders():
    """Regression: ISSUE-004/006/007 — public copy contradicted the product."""
    pages = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "static/security.html", "static/privacy.html", "static/terms.html",
            "static/acceptable-use.html", "site/src/components/sections.tsx",
        )
    ).lower()
    for banned in (
        "nothing sends without an explicit confirmation",
        "draft pending legal review",
        "do not rely on this as a binding contract",
        "your registered business address goes here",
    ):
        assert banned not in pages
    assert "auto mode" not in pages
    assert "capped on purpose" in pages


def test_public_funnel_is_gmail_first_and_has_recoverable_follow_up_state():
    """The public bundle should match the focused Gmail product promise."""
    landing = (ROOT / "static" / "landing" / "assets").glob("index-*.js")
    landing_text = "\n".join(path.read_text(encoding="utf-8") for path in landing).lower()
    app_home = (ROOT / "static" / "index.html").read_text(encoding="utf-8").lower()
    outreach = (ROOT / "static" / "outreach.html").read_text(encoding="utf-8").lower()

    assert "start free" not in landing_text
    assert "linkedin" not in landing_text
    assert "show the work after the send" in landing_text
    assert "promises detected" in landing_text
    assert "follow-ups scheduled" in landing_text
    assert "gmail or gemini remains your reply surface" in landing_text
    assert "another agent" not in app_home
    assert "unavailable" in outreach
    assert "gmail sent" in outreach
    assert "optional reply help" in outreach
    assert "the core is read-only against gmail sent" in landing_text
    assert "domain auth" in outreach
    assert "approval contract" in outreach
    assert "Nothing is emailed and the sheet is untouched" not in outreach
    assert "conversation pipeline" in app_home
    assert "meetings confirmed" in app_home
    assert "deals won" in app_home
    assert "confirmedmeetings > 0" in app_home
    assert "confirmeddeals > 0" in app_home


def test_commitment_queue_copy_matches_confirmed_due_lifecycle():
    outreach = (ROOT / "static" / "outreach.html").read_text(encoding="utf-8")
    assert "Needs confirmation" in outreach
    assert "Due to check" in outreach
    assert "No promises need attention yet." in outreach
    assert "\n  loadFollowUpStatus();\n" not in outreach
