"""Test suite for the outreach agent. All external boundaries (Gmail API,
OpenRouter, Supabase, Google Sheets) are faked -- nothing touches the network.

Run:  python tests/test_outreach_agent.py
"""
import base64
import contextlib
import os
import sys
import time
import traceback
from pathlib import Path

# Make config importable even without a .env (harmless if one exists).
for var, val in {
    "OPENROUTER_API_KEY": "test-key",
    "APP_SECRET_KEY": "test-secret",
    "SUPABASE_URL": "http://localhost",
    "SUPABASE_SECRET_KEY": "test-secret-key",
}.items():
    os.environ.setdefault(var, val)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "outreach-agent"))

import accounts_db
import agent
import auth
import gmail
import ratelimit
import reviews_db
import sheets
import send_outreach
import watch_replies


@contextlib.contextmanager
def patched(obj, name, value):
    original = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, original)


def _enc(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


def msg(from_addr, text):
    return {
        "payload": {
            "headers": [{"name": "From", "value": f"Someone <{from_addr}>"}],
            "body": {"data": _enc(text)},
        }
    }


def fake_gmail_service(messages):
    class Exec:
        def __init__(self, value):
            self.value = value

        def execute(self):
            return self.value

    class Threads:
        def get(self, **kw):
            return Exec({"messages": messages})

    class Users:
        def threads(self):
            return Threads()

    class Service:
        def users(self):
            return Users()

    return Service()


def fake_sheets_service(values):
    class Exec:
        def __init__(self, value):
            self.value = value

        def execute(self):
            return self.value

    class Values:
        def get(self, **kw):
            return Exec({"values": values})

    class Spreadsheets:
        def values(self):
            return Values()

    class Service:
        def spreadsheets(self):
            return Spreadsheets()

    return Service()


ACCOUNT = {
    "id": "acct-1",
    "sender_name": "Oanh",
    "meeting_purpose": "help teams automate invoicing",
    "calendar_booking_link": "https://cal.com/oanh/15min",
    "google_sheet_id": "sheet-1",
}

# ---------------------------------------------------------------- strip_quoted

def test_strip_quoted_gmail_style():
    text = "Sounds good, send details.\n\nOn Mon, Jul 14, 2026 at 3:02 PM Oanh <o@x.com> wrote:\n> Hi John,\n> quick question"
    assert gmail.strip_quoted(text) == "Sounds good, send details.", gmail.strip_quoted(text)


def test_strip_quoted_outlook_style():
    text = "Not interested, thanks.\n\n-----Original Message-----\nFrom: o@x.com\nSent: Monday"
    assert gmail.strip_quoted(text) == "Not interested, thanks."


def test_strip_quoted_from_header():
    text = "What does it cost?\n\nFrom: Oanh <o@x.com>\nSubject: intro"
    assert gmail.strip_quoted(text) == "What does it cost?"


def test_strip_quoted_all_quoted_falls_back():
    text = "> everything\n> is quoted"
    assert gmail.strip_quoted(text) == text


def test_strip_quoted_plain_text_untouched():
    text = "Just a normal reply.\nSecond line."
    assert gmail.strip_quoted(text) == text


# ---------------------------------------------------------------- _extract_body

def test_extract_body_top_level():
    assert gmail._extract_body({"body": {"data": _enc("hello")}}) == "hello"


def test_extract_body_prefers_text_plain():
    payload = {
        "body": {},
        "parts": [
            {"mimeType": "text/html", "body": {"data": _enc("<b>html</b>")}},
            {"mimeType": "text/plain", "body": {"data": _enc("plain")}},
        ],
    }
    assert gmail._extract_body(payload) == "plain"


def test_extract_body_nested_multipart():
    payload = {
        "body": {},
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "body": {},
                "parts": [{"mimeType": "text/plain", "body": {"data": _enc("nested")}}],
            }
        ],
    }
    assert gmail._extract_body(payload) == "nested"


def test_extract_body_html_only_returns_html():
    payload = {
        "body": {},
        "parts": [{"mimeType": "text/html", "body": {"data": _enc("<p>only html</p>")}}],
    }
    # Documents current behavior: raw HTML falls through to the LLM/UI.
    assert gmail._extract_body(payload) == "<p>only html</p>"


# ------------------------------------------- get_latest_reply_with_history

def test_history_happy_path():
    messages = [msg("me@me.com", "our outreach"), msg("john@x.com", "their reply")]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        reply, history = gmail.get_latest_reply_with_history(ACCOUNT, "t1", "john@x.com")
    assert reply == "their reply"
    assert history == [(False, "our outreach")]


def test_history_latest_is_ours_returns_none():
    messages = [msg("me@me.com", "outreach"), msg("john@x.com", "reply"), msg("me@me.com", "our answer")]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        reply, history = gmail.get_latest_reply_with_history(ACCOUNT, "t1", "john@x.com")
    assert reply is None and history == []


def test_history_single_message_returns_none():
    with patched(gmail, "_get_service", lambda account: fake_gmail_service([msg("me@me.com", "outreach")])):
        assert gmail.get_latest_reply_with_history(ACCOUNT, "t1", "j@x.com") == (None, [])


def test_history_contact_match_case_insensitive():
    messages = [msg("me@me.com", "outreach"), msg("John@X.com", "yes!")]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        reply, _ = gmail.get_latest_reply_with_history(ACCOUNT, "t1", " john@x.com ")
    assert reply == "yes!"


def test_history_multi_turn_labels():
    messages = [
        msg("me@me.com", "outreach"),
        msg("john@x.com", "question?"),
        msg("me@me.com", "answer"),
        msg("john@x.com", "follow-up"),
    ]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        reply, history = gmail.get_latest_reply_with_history(ACCOUNT, "t1", "john@x.com")
    assert reply == "follow-up"
    assert [h[0] for h in history] == [False, True, False]
    assert [h[1] for h in history] == ["outreach", "question?", "answer"]


def test_get_latest_reply_delegates():
    messages = [msg("me@me.com", "outreach"), msg("john@x.com", "their reply")]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        assert gmail.get_latest_reply(ACCOUNT, "t1", "john@x.com") == "their reply"


# ---------------------------------------------------------------- draft_reply

def test_draft_reply_prompt_contents():
    captured = {}

    def fake_chat(system, user_prompt):
        captured["system"] = system
        captured["prompt"] = user_prompt
        return "Sure -- here's the link.\n\nOanh"

    history = [(False, "our outreach email"), (True, "what does it cost?\n\nOn Mon Oanh <o@x.com> wrote:\n> pitch")]
    with patched(agent, "_chat", fake_chat):
        draft = agent.draft_reply(ACCOUNT, "John", "Acme", "sounds good, what's next?", history)

    p = captured["prompt"]
    assert "Oanh" in p and "https://cal.com/oanh/15min" in p
    assert "help teams automate invoicing" in p
    assert "John at Acme" in p
    assert "We wrote:\nour outreach email" in p
    assert "They wrote:\nwhat does it cost?" in p
    assert "On Mon Oanh" not in p, "quoted tail should be stripped from history"
    assert "sounds good, what's next?" in p
    assert draft.startswith("Sure")


def test_draft_reply_no_company_no_link():
    captured = {}
    account = dict(ACCOUNT, calendar_booking_link="")
    with patched(agent, "_chat", lambda s, u: captured.update(prompt=u) or "ok"):
        agent.draft_reply(account, "John", "", "hi", [])
    assert "Prospect: John\n" in captured["prompt"]
    assert " at " not in captured["prompt"].split("\n")[3]
    assert "(none set)" in captured["prompt"]


def test_draft_reply_empty_history_bodies_filtered():
    captured = {}
    with patched(agent, "_chat", lambda s, u: captured.update(prompt=u) or "ok"):
        agent.draft_reply(ACCOUNT, "J", "", "reply", [(False, "outreach"), (True, "   ")])
    assert captured["prompt"].count("They wrote:") == 0
    assert captured["prompt"].count("We wrote:") == 1


# ------------------------------------------------------- generate_outreach_email

def test_generate_outreach_wellformed():
    with patched(agent, "_chat", lambda s, u: "Subject: Quick intro\n\nHi John,\nbody here.\nOanh"):
        subject, body = agent.generate_outreach_email(ACCOUNT, "John", "Acme")
    assert subject == "Quick intro"
    assert body == "Hi John,\nbody here.\nOanh"


def test_generate_outreach_single_newline_output():
    # LLMs frequently emit "Subject: X\nBody..." without the blank line.
    with patched(agent, "_chat", lambda s, u: "Subject: Quick intro\nHi John,\nbody here."):
        subject, body = agent.generate_outreach_email(ACCOUNT, "John", "Acme")
    assert subject == "Quick intro"
    assert body == "Hi John,\nbody here."


def test_generate_outreach_requires_calendar_link():
    account = dict(ACCOUNT, calendar_booking_link="  ")
    try:
        agent.generate_outreach_email(account, "John", "Acme")
    except RuntimeError:
        return
    raise AssertionError("should raise without calendar link")


# ---------------------------------------------------------------------- sheets

def _row(status="", email="j@x.com", sent_at="", thread="", name="John", body="", company="Acme"):
    r = [""] * 9
    r[sheets.COL_NAME] = name
    r[sheets.COL_EMAIL] = email
    r[sheets.COL_COMPANY] = company
    r[sheets.COL_STATUS] = status
    r[sheets.COL_THREAD_ID] = thread
    r[sheets.COL_SENT_AT] = sent_at
    r[sheets.COL_EMAIL_BODY] = body
    return r


def test_header_validation_accepts_expected():
    values = [list(sheets.EXPECTED_HEADER), _row()]
    with patched(sheets, "_get_service", lambda account: fake_sheets_service(values)):
        rows = sheets.get_all_rows(ACCOUNT)
    assert len(rows) == 1 and rows[0][0] == 2


def test_header_validation_case_and_space_insensitive():
    header = [" name ", "EMAIL", "Company", "Status", "ThreadID", "SentAt", "EmailBody", "LeadReason", "EmailConfidence"]
    with patched(sheets, "_get_service", lambda account: fake_sheets_service([header])):
        assert sheets.get_all_rows(ACCOUNT) == []


def test_header_validation_rejects_reordered():
    bad = ["Email", "Name", "Company", "Status", "ThreadID", "SentAt", "EmailBody", "LeadReason", "EmailConfidence"]
    with patched(sheets, "_get_service", lambda account: fake_sheets_service([bad, _row()])):
        try:
            sheets.get_all_rows(ACCOUNT)
        except RuntimeError as e:
            assert "header" in str(e).lower()
            return
    raise AssertionError("reordered header must raise")


def test_header_validation_rejects_empty_sheet():
    with patched(sheets, "_get_service", lambda account: fake_sheets_service([])):
        try:
            sheets.get_all_rows(ACCOUNT)
        except RuntimeError as e:
            assert "empty" in str(e).lower()
            return
    raise AssertionError("empty sheet must raise")


def test_verify_row_index_match_moved_and_missing():
    rows = [(2, _row(email="a@x.com")), (3, _row(email="b@x.com"))]
    with patched(sheets, "get_all_rows", lambda account: rows):
        assert sheets._verify_row_index(ACCOUNT, 2, "a@x.com") == 2      # still there
        assert sheets._verify_row_index(ACCOUNT, 2, "B@x.com ") == 3     # moved -> followed
        try:
            sheets._verify_row_index(ACCOUNT, 2, "gone@x.com")
        except RuntimeError:
            return
    raise AssertionError("missing email must raise")


def test_update_row_uses_verified_index():
    written = {}

    class Exec:
        def execute(self):
            return {}

    class Values:
        def batchUpdate(self, spreadsheetId, body):
            written["data"] = body["data"]
            return Exec()

    class Spreadsheets:
        def values(self):
            return Values()

    class Service:
        def spreadsheets(self):
            return Spreadsheets()

    rows = [(5, _row(email="moved@x.com"))]
    with patched(sheets, "get_all_rows", lambda account: rows):
        with patched(sheets, "_get_service", lambda account: Service()):
            sheets.update_row(ACCOUNT, 2, status="Sent", expect_email="moved@x.com")
    assert written["data"][0]["range"] == "D5", written


def test_sent_in_last_24_hours():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    rows = [
        (2, _row(status="Sent", sent_at=now.isoformat().replace("+00:00", "Z"))),
        (3, _row(status="Sent", sent_at=(now - timedelta(hours=25)).isoformat())),
        (4, _row(status="Sent", sent_at=(now - timedelta(hours=1)).isoformat()[:19])),  # naive
        (5, _row(status="Sent", sent_at="14/07/2026 10:00")),  # manual junk
        (6, _row()),  # blank
    ]
    assert sheets.sent_in_last_24_hours(rows) == 2


def test_campaign_readiness_caps_at_daily_limit():
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = (
        [(i, _row()) for i in range(2, 12)]                       # 10 eligible
        + [(50 + i, _row(status="Sent", sent_at=now_iso)) for i in range(22)]  # 22 sent today
        + [(80, _row(email=""))]                                  # no email -> not eligible
        + [(81, _row(status="Discarded"))]                        # status -> not eligible
    )
    with patched(sheets, "get_all_rows", lambda account: rows):
        r = sheets.campaign_readiness(ACCOUNT)
    assert r["sent_today"] == 22
    assert r["daily_limit"] == sheets.DAILY_SEND_LIMIT
    assert r["remaining_today"] == max(0, sheets.DAILY_SEND_LIMIT - 22)
    assert len(r["eligible"]) == min(10, r["remaining_today"])
    assert r["eligible_total"] == 10
    assert r["capped"] == max(0, 10 - r["remaining_today"])


def test_row_update_cells_column_letters_match_col_constants():
    cells = sheets._row_update_cells(7, status="s", thread_id="t", sent_at="a", email_body="b", email_confidence="c")
    letters = {v: k[0] for k, v in cells}
    assert letters == {"s": chr(65 + sheets.COL_STATUS), "t": chr(65 + sheets.COL_THREAD_ID),
                       "a": chr(65 + sheets.COL_SENT_AT), "b": chr(65 + sheets.COL_EMAIL_BODY),
                       "c": chr(65 + sheets.COL_EMAIL_CONFIDENCE)}
    assert all(k[1:] == "7" for k, _ in cells)


def test_get_reply_check_rows_filters_status():
    rows = [(2, _row(status="Sent")), (3, _row(status="Replied")), (4, _row()), (5, _row(status="Discarded"))]
    with patched(sheets, "get_all_rows", lambda account: rows):
        got = sheets.get_reply_check_rows(ACCOUNT)
    assert [i for i, _ in got] == [2, 3]


# ------------------------------------------------------------- watch_replies

def _watch_env(calls, reply_map, existing_reviews=None):
    """Builds patches for a check_for_replies run. reply_map: thread_id -> (reply, history)."""
    existing = existing_reviews if existing_reviews is not None else {}

    def fake_add_review(account_id, row_index, name, email, thread_id, customer_reply, draft_reply):
        calls.setdefault("add_review", []).append((thread_id, customer_reply, draft_reply))
        key = (thread_id, customer_reply)
        if key in existing:
            return existing[key]
        existing[key] = len(existing) + 1
        return existing[key]

    def fake_get_reply(account, t, e):
        result = reply_map.get(t, (None, []))
        if isinstance(result, Exception):
            raise result
        return result

    return [
        (reviews_db, "find_review_id", lambda account_id, t, r: existing.get((t, r))),
        (sheets, "get_reply_check_rows", lambda account: calls["rows"]),
        (sheets, "update_row", lambda account, idx, **kw: calls.setdefault("update_row", []).append((idx, kw))),
        (gmail, "get_latest_reply_with_history", fake_get_reply),
        (agent, "draft_reply", lambda account, name, company, reply, history=():
            calls.setdefault("draft", []).append((name, company, reply, list(history))) or f"draft for {reply}"),
        (reviews_db, "add_review", fake_add_review),
        (reviews_db, "get_review", lambda account_id, rid: {"id": rid, "status": "pending"}),
    ]


def test_check_for_replies_happy_path():
    calls = {"rows": [
        (2, _row(status="Sent", thread="t1", name="John", email="john@x.com", body="orig")),
        (3, _row(status="Sent", thread="", name="NoThread")),
        (4, _row(status="Sent", thread="t2", name="Quiet", email="q@x.com")),
    ]}
    reply_map = {"t1": ("yes let's talk", [(False, "orig")])}
    with contextlib.ExitStack() as stack:
        for target in _watch_env(calls, reply_map):
            stack.enter_context(patched(*target))
        result = watch_replies.check_for_replies(ACCOUNT)
    assert len(result["reviews"]) == 1 and result["reviews"][0]["id"] == 1
    assert result["row_errors"] == []
    assert calls["draft"] == [("John", "Acme", "yes let's talk", [(False, "orig")])]
    assert calls["update_row"] == [(2, {"status": "Replied", "expect_email": "john@x.com"})]
    assert calls["add_review"][0][0] == "t1"


def test_check_for_replies_does_not_redraft_known_reply():
    """The auto-poll runs every 100s. Once a reply has a review (pending OR
    dismissed), polling again must not burn another LLM call on it."""
    rows = [(2, _row(status="Replied", thread="t1", name="John", email="john@x.com"))]
    reply_map = {"t1": ("yes let's talk", [(False, "orig")])}
    calls = {"rows": rows}
    existing = {}
    with contextlib.ExitStack() as stack:
        for target in _watch_env(calls, reply_map, existing):
            stack.enter_context(patched(*target))
        first = watch_replies.check_for_replies(ACCOUNT)   # creates the review
        second = watch_replies.check_for_replies(ACCOUNT)  # same reply again
    draft_calls = len(calls.get("draft", []))
    assert draft_calls == 1, f"BUG: LLM drafted {draft_calls} times for the same reply"
    assert len(second["reviews"]) == 0, f"BUG: already-reviewed reply returned as new again: {second}"


def test_check_for_replies_isolates_broken_rows():
    """One row with a dead Gmail thread must not block rows after it, and the
    skipped row must be reported, not silent."""
    calls = {"rows": [
        (2, _row(status="Sent", thread="dead", name="Broken", email="broken@x.com")),
        (3, _row(status="Sent", thread="t2", name="Jane", email="jane@x.com")),
    ]}
    reply_map = {
        "dead": RuntimeError("thread not found"),
        "t2": ("interested!", [(False, "orig")]),
    }
    with contextlib.ExitStack() as stack:
        for target in _watch_env(calls, reply_map):
            stack.enter_context(patched(*target))
        result = watch_replies.check_for_replies(ACCOUNT)
    assert len(result["reviews"]) == 1, "row after the broken one must still be checked"
    assert len(result["row_errors"]) == 1 and "Broken" in result["row_errors"][0]
    assert calls["draft"][0][0] == "Jane"


# --------------------------------------------------------------- send_outreach

def test_send_outreach_marks_each_row_immediately():
    calls = {"updates": []}
    rows = [(2, _row(name="John", email="j@x.com")), (3, _row(name="Fail", email="f@x.com", company=""))]

    def fake_generate(account, name, company):
        if name == "Fail":
            raise RuntimeError("LLM exploded")
        return "Subject line", "Body text"

    readiness = {"eligible": rows, "eligible_total": 2, "sent_today": 0,
                 "daily_limit": 25, "remaining_today": 25, "capped": 0}
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(sheets, "campaign_readiness", lambda account: readiness))
        stack.enter_context(patched(agent, "generate_outreach_email", fake_generate))
        stack.enter_context(patched(gmail, "send_email", lambda account, to, s, b: f"thread-{to}"))
        stack.enter_context(patched(sheets, "update_row", lambda account, idx, **kw: calls["updates"].append((idx, kw))))
        stack.enter_context(patched(send_outreach, "notify", lambda account, subj, msg_: calls.update(notify=(subj, msg_))))
        result = send_outreach.main(ACCOUNT)

    assert result["sent"] == 1 and result["total"] == 2
    assert result["failed"] == [{"email": "f@x.com", "error": "LLM exploded"}]
    # Per-row marking: exactly one update, at send time, with the guard email.
    assert len(calls["updates"]) == 1
    idx, kw = calls["updates"][0]
    assert idx == 2 and kw["status"] == "Sent" and kw["thread_id"] == "thread-j@x.com"
    assert kw["expect_email"] == "j@x.com"
    assert "Sent 1 of 2" in calls["notify"][1]
    assert result["remaining_today"] == 24


def test_send_outreach_sheet_write_failure_is_labeled():
    """If the email went out but the sheet write failed, the error must say
    the email was SENT -- not report it as a failed send."""
    rows = [(2, _row(name="John", email="j@x.com"))]
    readiness = {"eligible": rows, "eligible_total": 1, "sent_today": 0,
                 "daily_limit": 25, "remaining_today": 25, "capped": 0}

    def broken_update(account, idx, **kw):
        raise RuntimeError("sheet gone")

    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(sheets, "campaign_readiness", lambda account: readiness))
        stack.enter_context(patched(agent, "generate_outreach_email", lambda a, n, c: ("S", "B")))
        stack.enter_context(patched(gmail, "send_email", lambda account, to, s, b: "t-1"))
        stack.enter_context(patched(sheets, "update_row", broken_update))
        stack.enter_context(patched(send_outreach, "notify", lambda *a: None))
        result = send_outreach.main(ACCOUNT)

    assert result["sent"] == 0
    assert len(result["failed"]) == 1
    assert "was sent" in result["failed"][0]["error"].lower()


def test_send_outreach_nothing_pending():
    readiness = {"eligible": [], "eligible_total": 0, "sent_today": 0,
                 "daily_limit": 25, "remaining_today": 25, "capped": 0}
    with patched(sheets, "campaign_readiness", lambda account: readiness):
        result = send_outreach.main(ACCOUNT)
    assert result == {"sent": 0, "total": 0, "failed": [], "daily_limit": 25, "remaining_today": 25}


# ------------------------------------------------------------------------ auth

def test_session_token_roundtrip_and_tamper():
    token = auth.create_session_token("acct-42", ttl_seconds=60)
    assert auth.verify_session_token(token) == "acct-42"
    assert auth.verify_session_token(token[:-1] + ("0" if token[-1] != "0" else "1")) is None
    assert auth.verify_session_token(None) is None
    assert auth.verify_session_token("a.b") is None
    expired = auth.create_session_token("acct-42", ttl_seconds=-1)
    assert auth.verify_session_token(expired) is None


def test_password_hash_roundtrip():
    stored = auth.hash_password("hunter2")
    assert auth.verify_password("hunter2", stored)
    assert not auth.verify_password("wrong", stored)
    assert not auth.verify_password("hunter2", None)
    assert not auth.verify_password("hunter2", "garbage")


def test_oauth_state_roundtrip():
    state = auth.create_oauth_state()
    assert auth.verify_oauth_state(state)
    assert not auth.verify_oauth_state("123.deadbeef")
    assert not auth.verify_oauth_state(auth.create_session_token("x"))  # two dots


# ------------------------------------------------------------------- ratelimit

def test_ratelimit_blocks_over_limit():
    key = f"test:{time.time()}"
    assert all(ratelimit.check(key, limit=3, window_seconds=60) for _ in range(3))
    assert not ratelimit.check(key, limit=3, window_seconds=60)


def test_ratelimit_window_expiry():
    key = f"test2:{time.time()}"
    ratelimit._hits[key] = [time.time() - 400] * 3
    assert ratelimit.check(key, limit=3, window_seconds=300)


# ----------------------------------------------------------------- accounts_db

def test_normalize_sheet_id():
    f = accounts_db._normalize_sheet_id
    assert f("https://docs.google.com/spreadsheets/d/ABC-123_x/edit#gid=0") == "ABC-123_x"
    assert f("ABC-123_x") == "ABC-123_x"
    assert f("ABC-123_x?usp=sharing") == "ABC-123_x"
    assert f("ABC-123_x#gid=0") == "ABC-123_x"


def test_encrypt_decrypt_roundtrip():
    secret = '{"token": "ya29.xyz"}'
    assert accounts_db.decrypt_secret(accounts_db.encrypt_secret(secret)) == secret


# ---------------------------------------------------------------------- runner

def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    passed, failed = 0, []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"PASS  {name}")
        except Exception:
            failed.append(name)
            print(f"FAIL  {name}")
            traceback.print_exc()
    print(f"\n{passed}/{len(tests)} passed, {len(failed)} failed")
    if failed:
        print("Failed:", ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
