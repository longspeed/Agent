"""Test suite for the outreach agent. All external boundaries (Gmail API,
OpenRouter, Supabase, Google Sheets) are faked -- nothing touches the network.

Run:  python tests/test_outreach_agent.py
"""
import base64
import contextlib
import io
import os
import sys
import time
import traceback
import zipfile
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
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for `import server`

import accounts_db
import agent
import auth
import drafts_db
import gmail
import ratelimit
import reviews_db
import sheet_template
import sheets
import send_outreach
import suppressions_db
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
    "sender_company": "Ledgerline",
    "meeting_purpose": "help teams automate invoicing",
    "calendar_booking_link": "https://cal.com/oanh/15min",
    "google_sheet_id": "sheet-1",
}

# A generated email that passes _validate_outreach for ACCOUNT: contains the
# calendar link verbatim, is long enough, and trips none of the banned-phrase /
# placeholder / markdown / dash checks. Tests that only care about a DIFFERENT
# aspect (subject parsing, prepare queuing) reuse this so validation is never
# the incidental reason they fail.
GOOD_EMAIL_BODY = (
    "Hi John, you mentioned scaling accounts payable at Acme, which is the reason "
    "I'm writing. We help finance teams cut invoice matching from days to minutes. "
    "Worth 15 minutes to compare notes? Grab a time here: https://cal.com/oanh/15min\n\n"
    "Oanh\nLedgerline"
)
GOOD_EMAIL = f"Subject: Cutting invoice matching\n\n{GOOD_EMAIL_BODY}"

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


def test_strip_quoted_preserves_inline_reply():
    """Inline repliers weave answers between '>' lines -- nothing may be cut."""
    text = "Thanks!\n> got 15 min Tuesday?\nTuesday works, but pricing first:\nwhat does it cost?"
    assert gmail.strip_quoted(text) == text


def test_strip_quoted_preserves_midbody_from_line():
    text = "From: john@x.com — my colleague will attend\nAlso, can we do Friday?"
    assert gmail.strip_quoted(text) == text


def test_strip_quoted_outlook_with_body_below_marker():
    text = "Works for me.\n\n-----Original Message-----\nFrom: o@x.com\nSent: Monday\n\nthe entire original email body"
    assert gmail.strip_quoted(text) == "Works for me."


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
    with patched(agent, "_chat", lambda s, u: GOOD_EMAIL):
        subject, body = agent.generate_outreach_email(ACCOUNT, "John", "Acme")
    assert subject == "Cutting invoice matching"
    assert "https://cal.com/oanh/15min" in body
    assert body.startswith("Hi John")


def test_generate_outreach_single_newline_output():
    # LLMs frequently emit "Subject: X\nBody..." without the blank line.
    with patched(agent, "_chat", lambda s, u: GOOD_EMAIL.replace("\n\n", "\n", 1)):
        subject, body = agent.generate_outreach_email(ACCOUNT, "John", "Acme")
    assert subject == "Cutting invoice matching"
    assert body.startswith("Hi John")


def test_generate_outreach_without_link_asks_for_reply():
    """The CTA link is optional now: no link must NOT block generation, and the
    prompt should steer the model to ask for a reply instead."""
    account = dict(ACCOUNT, calendar_booking_link="  ")
    no_link_email = (
        "Subject: Cutting invoice matching\n\n"
        "Hi John, you mentioned scaling accounts payable at Acme, which is the reason I'm writing. "
        "We help finance teams cut invoice matching from days to minutes. "
        "If that's worth a look, just reply and I'll show how it maps to Acme.\n\nOanh\nLedgerline"
    )
    captured = {}
    with patched(agent, "_chat", lambda s, u: captured.update(prompt=u) or no_link_email):
        subject, body = agent.generate_outreach_email(account, "John", "Acme")
    assert subject == "Cutting invoice matching"
    assert "No call-to-action link is set" in captured["prompt"]
    # The validator must not demand a link that was never configured.
    assert agent._validate_outreach(subject, body, "") == []


def test_generate_outreach_still_requires_a_goal():
    account = dict(ACCOUNT, meeting_purpose="  ")  # empty goal, but link is set
    try:
        agent.generate_outreach_email(account, "John", "Acme")
    except RuntimeError:
        return
    raise AssertionError("an empty goal must still block generation")


def test_generate_outreach_blocks_default_meeting_purpose():
    from config import DEFAULT_MEETING_PURPOSE
    account = dict(ACCOUNT, meeting_purpose=DEFAULT_MEETING_PURPOSE)
    assert agent.account_send_blockers(account)  # preview would disable the button
    with patched(agent, "_chat", lambda s, u: GOOD_EMAIL):
        try:
            agent.generate_outreach_email(account, "John", "Acme")
        except RuntimeError as e:
            assert "Settings" in str(e)
            return
    raise AssertionError("default meeting purpose must block generation")


def test_generate_outreach_passes_lead_reason_to_prompt():
    captured = {}
    def fake_chat(system, user_prompt):
        captured["prompt"] = user_prompt
        return GOOD_EMAIL
    with patched(agent, "_chat", fake_chat):
        agent.generate_outreach_email(ACCOUNT, "John", "Acme", lead_reason="spoke at RevOps summit")
    assert "spoke at RevOps summit" in captured["prompt"]
    assert "Research note" in captured["prompt"]


def test_generate_outreach_omits_lead_reason_when_blank():
    captured = {}
    with patched(agent, "_chat", lambda s, u: captured.update(prompt=u) or GOOD_EMAIL):
        agent.generate_outreach_email(ACCOUNT, "John", "Acme", lead_reason="")
    assert "Research note" not in captured["prompt"]


def test_generate_outreach_embeds_unsubscribe_line():
    unsub = "https://app.example.com/unsubscribe?t=abc.def"
    with patched(agent, "_chat", lambda s, u: GOOD_EMAIL):
        _, body = agent.generate_outreach_email(ACCOUNT, "John", "Acme", unsubscribe_url=unsub)
    assert unsub in body


def test_custom_instructions_reach_the_outreach_prompt():
    captured = {}
    account = dict(ACCOUNT, custom_instructions="Casual founder-to-founder tone. Mention we're YC-backed.")
    with patched(agent, "_chat", lambda s, u: captured.update(prompt=u) or GOOD_EMAIL):
        agent.generate_outreach_email(account, "John", "Acme")
    assert "YC-backed" in captured["prompt"]
    assert "sender gave these instructions" in captured["prompt"].lower()


def test_custom_instructions_omitted_when_blank():
    captured = {}
    with patched(agent, "_chat", lambda s, u: captured.update(prompt=u) or GOOD_EMAIL):
        agent.generate_outreach_email(ACCOUNT, "John", "Acme")  # fixture has none
    assert "sender gave these instructions" not in captured["prompt"].lower()


def test_custom_instructions_are_length_capped():
    ctx = agent._sender_context(dict(ACCOUNT, custom_instructions="x" * 5000))
    assert len(ctx["custom_instructions"]) == 600, "must be bounded so it can't swamp the prompt"


def test_custom_instructions_reach_the_reply_prompt():
    captured = {}
    account = dict(ACCOUNT, custom_instructions="Keep replies to two short lines.")
    with patched(agent, "_chat", lambda s, u: captured.update(prompt=u) or "ok"):
        agent.draft_reply(account, "John", "Acme", "sounds good", [])
    assert "two short lines" in captured["prompt"]


def test_generate_outreach_retries_then_raises_on_bad_output():
    calls = {"n": 0}
    def bad_chat(system, user_prompt):
        calls["n"] += 1
        return "Subject: hi\n\nLet's hop on a call to discuss synergies."  # banned + no link + short
    with patched(agent, "_chat", bad_chat):
        try:
            agent.generate_outreach_email(ACCOUNT, "John", "Acme")
        except RuntimeError:
            assert calls["n"] == 2, "must retry exactly once before giving up"
            return
    raise AssertionError("invalid output must raise, not return")


def test_generate_outreach_second_attempt_can_succeed():
    outputs = iter(["Subject: hi\n\ntoo short", GOOD_EMAIL])
    with patched(agent, "_chat", lambda s, u: next(outputs)):
        subject, body = agent.generate_outreach_email(ACCOUNT, "John", "Acme")
    assert subject == "Cutting invoice matching"


# ------------------------------------------------------ subject parsing + validator

def test_split_subject_variants():
    for raw, want in [
        ("Subject: hello there\n\nbody", "hello there"),
        ("**Subject:** hello there\n\nbody", "hello there"),
        ("subject - hello there\nbody", "hello there"),
        ('Subject: "hello there"\n\nbody', "hello there"),
    ]:
        subject, _ = agent._split_subject(raw)
        assert subject == "hello there", (raw, subject)


def test_split_subject_missing_prefix_keeps_body_whole():
    # The critical regression: no "Subject:" line must NOT eat the first body
    # line into the subject header.
    raw = "Hi John, this is the whole email.\nSecond line."
    subject, body = agent._split_subject(raw)
    assert subject is None
    assert body == raw


def test_validator_flags_each_failure_mode():
    link = "https://cal.com/oanh/15min"
    good = GOOD_EMAIL_BODY
    assert agent._validate_outreach("Cutting invoice matching", good, link) == []
    assert agent._validate_outreach("", good, link)                      # empty subject
    assert agent._validate_outreach("s", good.replace(link, "my calendar"), link)  # link paraphrased
    assert agent._validate_outreach("s", good + " circle back soon", link)         # banned phrase
    assert agent._validate_outreach("s", good.replace("which is", "— which is"), link)  # em dash
    assert agent._validate_outreach("s", good.replace("Hi John", "Hi [First Name]"), link)   # placeholder
    assert agent._validate_outreach("s", good.replace("We help", "**We help**"), link)       # markdown
    assert agent._validate_outreach("s", "too short", link)              # truncated


def test_validator_requires_optout_when_expected():
    link = "https://cal.com/oanh/15min"
    unsub = "https://app.example.com/unsubscribe?t=x.y"
    assert agent._validate_outreach("s", GOOD_EMAIL_BODY, link, unsub)  # missing -> problem
    with_optout = GOOD_EMAIL_BODY + "\n\n" + agent._opt_out_line(unsub)
    assert agent._validate_outreach("s", with_optout, link, unsub) == []


def test_validator_rejects_foreign_script():
    # Georgian token injected mid-sentence, exactly like the free model produced.
    body = GOOD_EMAIL_BODY.replace("the reason", "the დღის reason")
    problems = agent._validate_outreach("Cutting invoice matching", body, "https://cal.com/oanh/15min")
    assert any("garbled" in p or "non-English" in p for p in problems)
    # And in the subject.
    assert agent._validate_outreach("Quick 你好 intro", GOOD_EMAIL_BODY, "https://cal.com/oanh/15min")


def test_validator_rejects_reaching_out_variant():
    # "reaching out" must be caught even though "reach out" is the base ban and
    # is NOT a substring of "reaching out".
    body = GOOD_EMAIL_BODY.replace("the reason I'm writing", "reaching out")
    problems = agent._validate_outreach("s", body, "https://cal.com/oanh/15min")
    assert any("reaching out" in p for p in problems)


def test_validator_rejects_empty_filler():
    body = ("Hi John, I wanted to set up a brief meeting to bring clarity to your upcoming "
            "decisions. Let me know if you want to slot something in. https://cal.com/oanh/15min")
    problems = agent._validate_outreach("A brief meeting", body, "https://cal.com/oanh/15min")
    assert any("filler" in p for p in problems)


def test_account_send_blockers_requires_real_sender_name():
    # Unset name defaults to "the team" and is blocked; a real name clears it.
    assert agent.account_send_blockers({"sender_name": "", "meeting_purpose": "sell CLIs to devs"})
    assert agent.account_send_blockers({"sender_name": "the team", "meeting_purpose": "sell CLIs to devs"})
    assert agent.account_send_blockers({"sender_name": "Long", "meeting_purpose": "sell CLIs to devs"}) == []


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


def test_header_validation_accepts_partial_prefix():
    """Real sheets (the founder's included) often only carry the first few
    header cells -- columns are positional, so a prefix must pass."""
    with patched(sheets, "_get_service", lambda account: fake_sheets_service([["Name", "Email"], _row()])):
        assert len(sheets.get_all_rows(ACCOUNT)) == 1


def test_header_validation_rejects_data_like_first_row():
    # A sheet with no header at all: row 1 is a data row, not Name/Email.
    with patched(sheets, "_get_service", lambda account: fake_sheets_service([["John", "john@x.com"]])):
        try:
            sheets.get_all_rows(ACCOUNT)
        except RuntimeError:
            return
    raise AssertionError("data row in place of header must raise")


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


def test_campaign_readiness_excludes_suppressed():
    rows = [(2, _row(email="keep@x.com")), (3, _row(email="Gone@x.com"))]
    with patched(sheets, "get_all_rows", lambda account: rows):
        r = sheets.campaign_readiness(ACCOUNT, suppressed_emails={"gone@x.com"})
    emails = [row[sheets.COL_EMAIL] for _, row in r["eligible"]]
    assert emails == ["keep@x.com"], "opted-out address (case-insensitive) must drop out"
    assert r["eligible_total"] == 1


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
        (sheets, "require_full_header", lambda account: calls.setdefault("header_gate", []).append(True)),
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
    assert calls.get("header_gate"), "reply run must gate on the full header before writing"


def test_check_for_replies_review_saved_before_sheet_write():
    """A failing status write must cost one row_error -- never a re-draft:
    the review (and thus the dedupe key) is recorded before the sheet write."""
    rows = [(2, _row(status="Sent", thread="t1", name="John", email="john@x.com"))]
    reply_map = {"t1": ("yes!", [(False, "orig")])}
    calls = {"rows": rows}
    existing = {}

    def broken_update(account, idx, **kw):
        raise RuntimeError("email cell edited")

    with contextlib.ExitStack() as stack:
        targets = _watch_env(calls, reply_map, existing)
        for obj, name, value in targets:
            if name == "update_row":
                value = broken_update
            stack.enter_context(patched(obj, name, value))
        first = watch_replies.check_for_replies(ACCOUNT)
        second = watch_replies.check_for_replies(ACCOUNT)

    assert len(first["reviews"]) == 1, "review must exist despite the failed sheet write"
    assert len(first["row_errors"]) == 1 and "queued for review" in first["row_errors"][0]
    assert len(calls["draft"]) == 1, f"BUG: re-drafted on the failure path ({len(calls['draft'])} drafts)"
    assert len(second["reviews"]) == 0


# --------------------------------------------- send_outreach: prepare (phase 1)

def _readiness(rows, remaining=25):
    return {"eligible": rows, "eligible_total": len(rows), "sent_today": 0,
            "daily_limit": 25, "remaining_today": remaining, "capped": 0}


def test_prepare_drafts_queues_without_sending_or_writing():
    calls = {"drafts": [], "sends": 0, "updates": 0}
    rows = [(2, _row(name="John", email="j@x.com", company="Acme")),
            (3, _row(name="Fail", email="f@x.com", company=""))]

    def fake_generate(account, name, company, lead_reason="", unsubscribe_url=""):
        if name == "Fail":
            raise RuntimeError("LLM exploded")
        return "Subject line", "Body text"

    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(sheets, "require_full_header", lambda account: calls.update(header_gate=True)))
        stack.enter_context(patched(suppressions_db, "list_suppressed_emails", lambda aid: set()))
        stack.enter_context(patched(sheets, "campaign_readiness", lambda account, suppressed_emails=None: _readiness(rows)))
        stack.enter_context(patched(drafts_db, "has_pending_for_row", lambda aid, idx: False))
        stack.enter_context(patched(agent, "generate_outreach_email", fake_generate))
        stack.enter_context(patched(drafts_db, "add_draft", lambda *a: calls["drafts"].append(a) or len(calls["drafts"])))
        stack.enter_context(patched(gmail, "send_email", lambda *a, **k: calls.__setitem__("sends", calls["sends"] + 1) or "t"))
        stack.enter_context(patched(sheets, "update_row", lambda *a, **k: calls.__setitem__("updates", calls["updates"] + 1)))
        stack.enter_context(patched(send_outreach, "notify", lambda account, subj, msg_: calls.update(notify=(subj, msg_))))
        result = send_outreach.prepare_drafts(ACCOUNT)

    assert calls.get("header_gate"), "prepare must gate on the full header"
    assert result["prepared"] == 1 and result["total"] == 2
    assert result["failed"] == [{"email": "f@x.com", "error": "LLM exploded"}]
    assert calls["sends"] == 0, "prepare must NOT send any email"
    assert calls["updates"] == 0, "prepare must NOT write the sheet"
    # add_draft(account_id, row_index, name, email, company, subject, body)
    assert len(calls["drafts"]) == 1
    a = calls["drafts"][0]
    assert a[1] == 2 and a[3] == "j@x.com" and a[5] == "Subject line"
    assert "Prepared 1 of 2" in calls["notify"][1]


def test_prepare_drafts_skips_rows_already_in_queue():
    rows = [(2, _row(email="j@x.com"))]
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(sheets, "require_full_header", lambda account: None))
        stack.enter_context(patched(suppressions_db, "list_suppressed_emails", lambda aid: set()))
        stack.enter_context(patched(sheets, "campaign_readiness", lambda account, suppressed_emails=None: _readiness(rows)))
        stack.enter_context(patched(drafts_db, "has_pending_for_row", lambda aid, idx: True))  # already queued
        stack.enter_context(patched(agent, "generate_outreach_email", lambda *a, **k: ("S", "B")))
        stack.enter_context(patched(send_outreach, "notify", lambda *a: None))
        result = send_outreach.prepare_drafts(ACCOUNT)
    assert result["prepared"] == 0 and result["total"] == 0


def test_prepare_drafts_nothing_eligible():
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(sheets, "require_full_header", lambda account: None))
        stack.enter_context(patched(suppressions_db, "list_suppressed_emails", lambda aid: set()))
        stack.enter_context(patched(sheets, "campaign_readiness", lambda account, suppressed_emails=None: _readiness([])))
        result = send_outreach.prepare_drafts(ACCOUNT)
    assert result == {"prepared": 0, "total": 0, "failed": [], "daily_limit": 25, "remaining_today": 25}


# ----------------------------------------------- send_outreach: send (phase 2)

def test_mark_row_sent_labels_persistent_failure():
    """If the email went out but the sheet write keeps failing, the error must
    say the email WAS sent, not report a failed send."""
    attempts = []
    def broken_update(account, idx, **kw):
        attempts.append(idx)
        raise RuntimeError("sheet gone")
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(send_outreach, "_MARK_RETRY_DELAY", 0))
        stack.enter_context(patched(sheets, "update_row", broken_update))
        try:
            send_outreach.mark_row_sent(ACCOUNT, 2, "j@x.com", "t-1", "body")
        except RuntimeError as e:
            assert "was sent" in str(e).lower()
            assert len(attempts) == send_outreach._MARK_RETRIES
            return
    raise AssertionError("persistent failure must raise after exhausting retries")


def test_mark_row_sent_recovers_from_transient_failure():
    attempts = []
    def flaky(account, idx, **kw):
        attempts.append(kw)
        if len(attempts) == 1:
            raise RuntimeError("429 transient")
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(send_outreach, "_MARK_RETRY_DELAY", 0))
        stack.enter_context(patched(sheets, "update_row", flaky))
        send_outreach.mark_row_sent(ACCOUNT, 2, "j@x.com", "t-1", "body")
    assert len(attempts) == 2 and attempts[1]["status"] == "Sent"


def test_send_prepared_draft_sends_marks_and_reappends_optout():
    calls = {}
    draft = {"row_index": 2, "email": "j@x.com", "subject": "Hi", "body": "Body without a link"}
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(auth, "unsubscribe_url", lambda aid, email: "https://u/unsub?t=x.y"))
        stack.enter_context(patched(gmail, "send_email",
            lambda account, to, s, b, unsubscribe_url="": calls.update(to=to, body=b, header=unsubscribe_url) or "t-9"))
        stack.enter_context(patched(sheets, "update_row", lambda account, idx, **kw: calls.update(marked=(idx, kw))))
        thread_id, sent_body = send_outreach.send_prepared_draft(ACCOUNT, draft)
    assert thread_id == "t-9"
    assert calls["header"] == "https://u/unsub?t=x.y", "List-Unsubscribe header must be set"
    assert "https://u/unsub?t=x.y" in sent_body, "opt-out line must be appended when missing"
    idx, kw = calls["marked"]
    assert idx == 2 and kw["status"] == "Sent" and kw["expect_email"] == "j@x.com"


def test_send_all_prepared_skips_suppressed_and_respects_cap():
    drafts = [
        {"id": 1, "row_index": 2, "email": "a@x.com", "subject": "S", "body": "b"},
        {"id": 2, "row_index": 3, "email": "b@x.com", "subject": "S", "body": "b"},
        {"id": 3, "row_index": 4, "email": "c@x.com", "subject": "S", "body": "b"},
    ]
    sent, discarded, marked = [], [], []
    readiness_calls = {"n": 0}
    def fake_readiness(account, **k):
        readiness_calls["n"] += 1
        return {"remaining_today": 1}  # only room for one send
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(send_outreach, "_SEND_ALL_MIN_GAP", 0))
        stack.enter_context(patched(send_outreach, "_SEND_ALL_MAX_GAP", 0))
        stack.enter_context(patched(drafts_db, "list_pending_drafts", lambda aid: drafts))
        stack.enter_context(patched(suppressions_db, "is_suppressed", lambda aid, email: email == "a@x.com"))
        stack.enter_context(patched(drafts_db, "discard", lambda aid, did: discarded.append(did)))
        stack.enter_context(patched(sheets, "campaign_readiness", fake_readiness))
        def fake_send(account, draft, subject=None, body=None):
            sent.append(draft["email"]); return "t", draft["body"]
        stack.enter_context(patched(send_outreach, "send_prepared_draft", fake_send))
        stack.enter_context(patched(drafts_db, "mark_sent", lambda aid, did, s, b: marked.append(did)))
        stack.enter_context(patched(send_outreach, "notify", lambda *a: None))
        result = send_outreach.send_all_prepared(ACCOUNT)
    assert sent == ["b@x.com"], "suppressed skipped, then one sent before the cap bit"
    assert discarded == [1], "the suppressed draft is discarded, not left pending"
    assert result["sent"] == 1
    reasons = {s["reason"] for s in result["skipped"]}
    assert "recipient unsubscribed" in reasons and "daily limit reached" in reasons
    # Efficiency guarantee: the cap is read once for the batch, not per draft.
    assert readiness_calls["n"] == 1, "campaign_readiness must be read once, not per draft"


# ------------------------------------------------------------ header write gate

def test_require_full_header_accepts_complete():
    with patched(sheets, "_get_service", lambda account: fake_sheets_service([list(sheets.EXPECTED_HEADER)])):
        sheets.require_full_header(ACCOUNT)  # must not raise


def test_require_full_header_rejects_partial():
    with patched(sheets, "_get_service", lambda account: fake_sheets_service([["Name", "Email"]])):
        try:
            sheets.require_full_header(ACCOUNT)
        except RuntimeError as e:
            assert "row 1" in str(e).lower()
            return
    raise AssertionError("partial header must block writes")


# ------------------------------------------------------------- server endpoints

def test_server_secure_cookies_env_parse():
    import server
    cases = [("", False), ("0", False), ("false", False), ("no", False),
             ("1", True), ("true", True), ("YES", True), (" 1 ", True)]
    saved = os.environ.get("SECURE_COOKIES")
    try:
        for val, expect in cases:
            if val == "":
                os.environ.pop("SECURE_COOKIES", None)
            else:
                os.environ["SECURE_COOKIES"] = val
            assert server._secure_cookies() == expect, (val, expect)
    finally:
        if saved is None:
            os.environ.pop("SECURE_COOKIES", None)
        else:
            os.environ["SECURE_COOKIES"] = saved


def test_server_prepare_lock_lifecycle():
    """REGRESSION (eng review): concurrent-prepare 409, release after run,
    release when the job thread fails to spawn."""
    import server
    from types import SimpleNamespace
    account = dict(ACCOUNT)
    req = SimpleNamespace(state=SimpleNamespace(account_id=account["id"]))
    body = server.PrepareCampaignBody(confirmed=True)
    captured = {}
    server._accounts_sending.discard(account["id"])
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: account))
        stack.enter_context(patched(server.ratelimit, "check", lambda *a, **k: True))
        stack.enter_context(patched(server, "_campaign_preview", lambda a: {"eligible": 1, "blockers": []}))
        stack.enter_context(patched(server, "start_job", lambda aid, fn: captured.update(fn=fn) or "job-1"))
        assert server.prepare_campaigns(req, body) == {"job_id": "job-1"}
        assert account["id"] in server._accounts_sending
        try:
            server.prepare_campaigns(req, body)
            raise AssertionError("expected 409 while a batch is in flight")
        except server.HTTPException as e:
            assert e.status_code == 409
        with patched(server.send_outreach, "prepare_drafts", lambda a: {"prepared": 0}):
            captured["fn"]()
        assert account["id"] not in server._accounts_sending, "lock must release after the job runs"

        def boom(aid, fn):
            raise RuntimeError("thread spawn failed")
        stack.enter_context(patched(server, "start_job", boom))
        try:
            server.prepare_campaigns(req, body)
            raise AssertionError("expected spawn failure to propagate")
        except RuntimeError:
            pass
        assert account["id"] not in server._accounts_sending, "lock must release when spawn fails"


def test_server_prepare_blocked_by_settings():
    """A generic meeting purpose / missing link surfaces as a 409, not a batch
    that fails every row at generation time."""
    import server
    from types import SimpleNamespace
    account = dict(ACCOUNT)
    req = SimpleNamespace(state=SimpleNamespace(account_id=account["id"]))
    body = server.PrepareCampaignBody(confirmed=True)
    server._accounts_sending.discard(account["id"])
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: account))
        stack.enter_context(patched(server.ratelimit, "check", lambda *a, **k: True))
        stack.enter_context(patched(server, "_campaign_preview", lambda a: {"eligible": 3, "blockers": ["Describe what your meeting is for in Settings."]}))
        try:
            server.prepare_campaigns(req, body)
            raise AssertionError("expected 409 when settings block the batch")
        except server.HTTPException as e:
            assert e.status_code == 409 and "Settings" in e.detail
        assert account["id"] not in server._accounts_sending, "no lock should be held on a rejected batch"


# --------------------------------------------------- server: draft send/discard routes

def _srv_req(account_id="acct-1"):
    from types import SimpleNamespace
    return SimpleNamespace(state=SimpleNamespace(account_id=account_id))


_PENDING_DRAFT = {"id": 7, "row_index": 2, "email": "j@x.com", "subject": "orig",
                  "body": "orig body", "status": "pending"}


def test_send_draft_404_when_missing_or_handled():
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: None))
        try:
            server.send_draft(_srv_req(), 7, server.SendDraftBody(subject="s", body="b"))
            raise AssertionError("expected 404")
        except server.HTTPException as e:
            assert e.status_code == 404


def test_send_draft_409_and_discards_when_suppressed():
    import server
    discarded = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: dict(_PENDING_DRAFT)))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: True))
        stack.enter_context(patched(server.drafts_db, "discard", lambda aid, did: discarded.append(did)))
        try:
            server.send_draft(_srv_req(), 7, server.SendDraftBody(subject="s", body="b"))
            raise AssertionError("expected 409")
        except server.HTTPException as e:
            assert e.status_code == 409
    assert discarded == [7], "a draft whose recipient opted out is discarded, not sent"


def test_send_draft_409_when_cap_reached():
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: dict(_PENDING_DRAFT)))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(server.suppressions_db, "list_suppressed_emails", lambda aid: set()))
        stack.enter_context(patched(server.sheets, "campaign_readiness", lambda account, **k: {"remaining_today": 0}))
        try:
            server.send_draft(_srv_req(), 7, server.SendDraftBody(subject="s", body="b"))
            raise AssertionError("expected 409")
        except server.HTTPException as e:
            assert e.status_code == 409


def test_send_draft_happy_path_marks_edited_copy():
    import server
    marked = {}
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: dict(_PENDING_DRAFT)))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(server.suppressions_db, "list_suppressed_emails", lambda aid: set()))
        stack.enter_context(patched(server.sheets, "campaign_readiness", lambda account, **k: {"remaining_today": 5}))
        stack.enter_context(patched(server.send_outreach, "send_prepared_draft",
            lambda a, d, subject=None, body=None: ("t-1", "sent body with optout")))
        stack.enter_context(patched(server.drafts_db, "mark_sent",
            lambda aid, did, s, b: marked.update(did=did, subject=s, body=b)))
        result = server.send_draft(_srv_req(), 7, server.SendDraftBody(subject="edited", body="edited body"))
    assert result == {"ok": True}
    # The operator's edited subject is recorded, and the body stored is the one
    # send_prepared_draft actually sent (opt-out line applied).
    assert marked == {"did": 7, "subject": "edited", "body": "sent body with optout"}


def test_discard_draft_happy_and_404():
    import server
    discarded = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "discard", lambda aid, did: discarded.append(did)))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: {"id": did, "status": "pending"}))
        assert server.discard_draft(_srv_req(), 5) == {"ok": True}
    assert discarded == [5]
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: None))
        try:
            server.discard_draft(_srv_req(), 5)
            raise AssertionError("expected 404")
        except server.HTTPException as e:
            assert e.status_code == 404


def test_send_all_drafts_409_when_none_pending():
    import server
    account = dict(ACCOUNT)
    server._accounts_sending.discard(account["id"])
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: account))
        stack.enter_context(patched(server.ratelimit, "check", lambda *a, **k: True))
        stack.enter_context(patched(server.drafts_db, "list_pending_drafts", lambda aid: []))
        try:
            server.send_all_drafts(_srv_req())
            raise AssertionError("expected 409 when nothing is pending")
        except server.HTTPException as e:
            assert e.status_code == 409
    assert account["id"] not in server._accounts_sending, "no lock held when there's nothing to send"


def test_send_all_drafts_starts_background_job():
    import server
    account = dict(ACCOUNT)
    server._accounts_sending.discard(account["id"])
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: account))
        stack.enter_context(patched(server.ratelimit, "check", lambda *a, **k: True))
        stack.enter_context(patched(server.drafts_db, "list_pending_drafts", lambda aid: [{"id": 1}]))
        stack.enter_context(patched(server, "start_job", lambda aid, fn: "job-9"))
        assert server.send_all_drafts(_srv_req()) == {"job_id": "job-9"}
    server._accounts_sending.discard(account["id"])  # cleanup: run()'s finally never fired (start_job mocked)


# --------------------------------------------------- server: public unsubscribe routes

def test_unsubscribe_confirm_invalid_token_400():
    import server
    with patched(server.auth, "verify_unsubscribe_token", lambda t: None):
        resp = server.unsubscribe_confirm("garbage")
    assert resp.status_code == 400
    assert b"expired or invalid" in resp.body.lower()


def test_unsubscribe_confirm_valid_shows_confirm_page():
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server.auth, "verify_unsubscribe_token", lambda t: ("acct-1", "p@x.com")))
        stack.enter_context(patched(server.accounts_db, "get_account", lambda aid: dict(ACCOUNT)))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: False))
        resp = server.unsubscribe_confirm("t")
    assert resp.status_code == 200
    assert b"Confirm unsubscribe" in resp.body and b"p@x.com" in resp.body


# A request whose body/query mirror what Starlette hands the endpoint, WITHOUT
# faking request.form() -- the old fakes did, which hid that request.form()
# needs python-multipart and 500s in production (found by /qa 2026-07-25).
class _FakeUnsubReq:
    def __init__(self, body=b"", query=None):
        self._body = body
        self.query_params = query or {}
    async def body(self):
        return self._body


def test_unsubscribe_apply_reads_token_from_form_body():
    """Confirm-page POST: token arrives as a urlencoded body field. Must parse
    without python-multipart."""
    import server
    import asyncio
    added, marked = [], []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server.auth, "verify_unsubscribe_token", lambda t: ("acct-1", "p@x.com")))
        stack.enter_context(patched(server.accounts_db, "get_account", lambda aid: dict(ACCOUNT)))
        stack.enter_context(patched(server.suppressions_db, "add",
            lambda aid, email, source="unsubscribe_link": added.append((aid, email))))
        stack.enter_context(patched(server.sheets, "mark_unsubscribed", lambda account, email: marked.append(email)))
        resp = asyncio.run(server.unsubscribe_apply(_FakeUnsubReq(body=b"t=sometoken")))
    assert resp.status_code == 200
    assert b"unsubscribed" in resp.body.lower()
    assert added == [("acct-1", "p@x.com")] and marked == ["p@x.com"]


def test_unsubscribe_apply_reads_token_from_query_one_click():
    """Gmail RFC 8058 one-click: token in the query string, body is
    'List-Unsubscribe=One-Click'. Must still suppress."""
    import server
    import asyncio
    added = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server.auth, "verify_unsubscribe_token", lambda t: ("acct-1", "p@x.com")))
        stack.enter_context(patched(server.accounts_db, "get_account", lambda aid: dict(ACCOUNT)))
        stack.enter_context(patched(server.suppressions_db, "add",
            lambda aid, email, source="unsubscribe_link": added.append(email)))
        stack.enter_context(patched(server.sheets, "mark_unsubscribed", lambda a, e: None))
        req = _FakeUnsubReq(body=b"List-Unsubscribe=One-Click", query={"t": "sometoken"})
        resp = asyncio.run(server.unsubscribe_apply(req))
    assert resp.status_code == 200 and added == ["p@x.com"]


def test_unsubscribe_apply_still_succeeds_when_sheet_write_fails():
    """The opt-out (suppression) is authoritative; a Sheets hiccup on the
    best-effort row mark must not turn into an error page for the recipient."""
    import server
    import asyncio
    added = []

    def boom(account, email):
        raise RuntimeError("sheet unreachable")

    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server.auth, "verify_unsubscribe_token", lambda t: ("acct-1", "p@x.com")))
        stack.enter_context(patched(server.accounts_db, "get_account", lambda aid: dict(ACCOUNT)))
        stack.enter_context(patched(server.suppressions_db, "add",
            lambda aid, email, source="unsubscribe_link": added.append(email)))
        stack.enter_context(patched(server.sheets, "mark_unsubscribed", boom))
        resp = asyncio.run(server.unsubscribe_apply(_FakeUnsubReq(body=b"t=sometoken")))
    assert resp.status_code == 200 and added == ["p@x.com"]


# ------------------------------------------------------- unsubscribe tokens

def test_unsubscribe_token_roundtrip():
    token = auth.create_unsubscribe_token("acct-9", "Person@Example.com")
    result = auth.verify_unsubscribe_token(token)
    assert result == ("acct-9", "person@example.com"), "email is normalised to lower-case"


def test_unsubscribe_token_tamper_and_garbage():
    token = auth.create_unsubscribe_token("acct-9", "p@x.com")
    body, _, sig = token.partition(".")
    assert auth.verify_unsubscribe_token(f"{body}.deadbeef") is None  # bad signature
    assert auth.verify_unsubscribe_token("not-a-token") is None       # no dot
    assert auth.verify_unsubscribe_token("") is None
    assert auth.verify_unsubscribe_token(None) is None


def test_unsubscribe_token_not_interchangeable_with_session():
    """An unsubscribe token must never authenticate as a session, and vice
    versa -- they share the signing key but not the payload namespace."""
    unsub = auth.create_unsubscribe_token("acct-9", "p@x.com")
    session = auth.create_session_token("acct-9", ttl_seconds=60)
    assert auth.verify_session_token(unsub) is None
    assert auth.verify_unsubscribe_token(session) is None


def test_server_plan_copy_is_accurate():
    import server
    from types import SimpleNamespace
    req = SimpleNamespace(state=SimpleNamespace(account_id="a"))
    with patched(server, "_account", lambda r: dict(ACCOUNT)):
        plan = server.get_plan(req)
    assert "verified-email" not in plan["note"]
    assert "Human-approved" in plan["note"]


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


# ------------------------------------------------------- security fixes (2026-07-22)

def test_client_ip_prefers_cf_connecting_ip():
    """REGRESSION: behind the Cloudflare tunnel every request arrives from
    127.0.0.1, so keying rate limits on request.client.host collapsed all
    visitors into one bucket. CF-Connecting-IP is the authoritative client IP."""
    import server
    from types import SimpleNamespace
    # Header present (behind the tunnel) -> the real client IP wins.
    req = SimpleNamespace(headers={"cf-connecting-ip": "203.0.113.9"},
                          client=SimpleNamespace(host="127.0.0.1"))
    assert server._client_ip(req) == "203.0.113.9"
    # No header (local/dev) -> socket peer, matching the old behavior.
    req2 = SimpleNamespace(headers={}, client=SimpleNamespace(host="10.0.0.5"))
    assert server._client_ip(req2) == "10.0.0.5"
    # Whitespace in the header is stripped.
    req3 = SimpleNamespace(headers={"cf-connecting-ip": "  198.51.100.7 "},
                           client=SimpleNamespace(host="127.0.0.1"))
    assert server._client_ip(req3) == "198.51.100.7"
    # No client at all -> "unknown", never an AttributeError.
    req4 = SimpleNamespace(headers={}, client=None)
    assert server._client_ip(req4) == "unknown"


def test_link_google_blocks_password_account_takeover():
    """REGRESSION: a "Sign in with Google" identity must NOT silently merge
    into a pre-registered password account (unverified email) -- that's an
    account-takeover primitive. See accounts_db.AccountLinkBlocked."""
    # Case A: email already belongs to a PASSWORD account -> refuse to merge.
    with patched(accounts_db, "get_account_by_google_id", lambda gid: None), \
         patched(accounts_db, "get_account_by_email",
                 lambda e: {"id": "prereg", "email": e, "password_hash": "pbkdf2$260000$x$y"}):
        try:
            accounts_db.link_or_create_google_account("victim@corp.com", "google-123")
            raise AssertionError("expected AccountLinkBlocked for a password account")
        except accounts_db.AccountLinkBlocked:
            pass

    # Case B: email belongs to a password-LESS account (itself created via
    # Google) -> nothing to hijack, safe to attach the google_id.
    linked = {"id": "acct-9", "email": "u@corp.com", "google_id": "google-xyz"}

    class _Resp:
        data = [linked]

    class _Query:
        def update(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def execute(self):
            return _Resp()

    class _Client:
        def table(self, *a, **k):
            return _Query()

    with patched(accounts_db, "get_account_by_google_id", lambda gid: None), \
         patched(accounts_db, "get_account_by_email",
                 lambda e: {"id": "acct-9", "email": e, "password_hash": None}), \
         patched(accounts_db, "_get_client", lambda: _Client()):
        assert accounts_db.link_or_create_google_account("u@corp.com", "google-xyz") == linked

    # Case C: this google_id is already linked -> reuse, no email lookup at all.
    existing = {"id": "acct-1", "google_id": "google-known"}
    with patched(accounts_db, "get_account_by_google_id", lambda gid: existing):
        assert accounts_db.link_or_create_google_account("x@y.com", "google-known") is existing


def test_run_job_sanitizes_unexpected_errors():
    """RuntimeError is the app's user-safe message convention -> surfaced.
    Any other exception must not leak its internals to the client."""
    import server
    server._run_job("job-rt", "acct-1",
                    lambda: (_ for _ in ()).throw(RuntimeError("Set a calendar link in Settings")))
    assert server.JOBS["job-rt"]["error"] == "Set a calendar link in Settings"

    server._run_job("job-int", "acct-1",
                    lambda: (_ for _ in ()).throw(KeyError("secret_internal_field")))
    assert "secret_internal_field" not in server.JOBS["job-int"]["error"]
    assert server.JOBS["job-int"]["error"] == "Something went wrong on our end. Please try again."

    server.JOBS.pop("job-rt", None)
    server.JOBS.pop("job-int", None)


def test_ratelimit_evicts_stale_keys():
    """A key whose newest hit predates the largest window is swept, so one-off
    keys (e.g. a single signup IP) don't accumulate in _hits forever."""
    stale_key = f"stale:{time.time()}"
    fresh_key = f"fresh:{time.time()}"
    ratelimit._hits[stale_key] = [time.time() - 10_000]
    ratelimit._last_sweep = 0.0       # force the next check() to run a sweep
    ratelimit._max_window = 3600
    ratelimit.check(fresh_key, limit=5, window_seconds=3600)
    assert stale_key not in ratelimit._hits, "stale key should have been evicted"
    ratelimit._hits.pop(fresh_key, None)


def test_tavily_search_resilient_and_requires_key():
    """Missing key stays a loud config error; a failed request returns [] (so
    one flaky query doesn't abort a multi-query search) and isn't metered."""
    import search
    import requests as _requests

    with patched(search, "TAVILY_API_KEY", ""):
        try:
            search.tavily_search("q")
            raise AssertionError("expected RuntimeError when the key is unset")
        except RuntimeError:
            pass

    def boom_post(*a, **k):
        raise _requests.RequestException("network down")

    metered = []
    with patched(search, "TAVILY_API_KEY", "key"), \
         patched(search.requests, "post", boom_post), \
         patched(search.usage, "record", lambda *a, **k: metered.append(a)):
        assert search.tavily_search("q") == []
    assert metered == [], "a failed search must not be metered"

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"title": "T", "url": "http://u", "content": "C", "extra": 1}]}

    metered_ok = []
    with patched(search, "TAVILY_API_KEY", "key"), \
         patched(search.requests, "post", lambda *a, **k: _Resp()), \
         patched(search.usage, "record", lambda *a, **k: metered_ok.append(a)):
        rows = search.tavily_search("q", max_results=3)
    assert rows == [{"title": "T", "url": "http://u", "content": "C"}]
    assert len(metered_ok) == 1, "a successful search meters exactly once"


# -------------------------------------------------------------- sheet template

def _template_rows():
    """Read the generated workbook back the way a spreadsheet app would, so the
    assertions below test the shipped bytes rather than the source constants."""
    import xml.etree.ElementTree as ET

    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    archive = zipfile.ZipFile(io.BytesIO(sheet_template.build_xlsx()))
    sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    rows = []
    for row in sheet.find("m:sheetData", ns).findall("m:row", ns):
        cells = {}
        for cell in row.findall("m:c", ns):
            column = "".join(ch for ch in cell.get("r") if ch.isalpha())
            text = cell.find("m:is/m:t", ns)
            cells[column] = text.text if text is not None else ""
        rows.append([cells.get(chr(ord("A") + i), "") for i in range(9)])
    return rows


def test_sheet_template_header_row_matches_expected_header():
    """The entire point of the download is a row 1 the write gate accepts."""
    assert _template_rows()[0] == sheets.EXPECTED_HEADER


def test_sheet_template_survives_the_write_gate():
    """require_full_header() is what rejects a hand-typed sheet. The file we
    hand people has to clear it, byte for byte."""
    header = _template_rows()[0]
    with patched(sheets, "_get_service", lambda account: fake_sheets_service([header])):
        sheets.require_full_header(ACCOUNT)  # raises if the header is wrong


def test_sheet_template_rows_are_never_sendable():
    """Someone will connect this file as their real sheet without deleting the
    examples. campaign_readiness() queues any row with a blank Status and an
    email, so one blank Status here emails a stranger on their first run. Run
    the shipped rows through the real eligibility rule, not a copy of it."""
    rows = [(i, row) for i, row in enumerate(_template_rows()[1:], start=2)]
    with patched(sheets, "get_all_rows", lambda account: rows):
        readiness = sheets.campaign_readiness(ACCOUNT)
    assert readiness["eligible_total"] == 0, (
        f"a demo row is queued to send: {readiness['eligible']!r}"
    )


def test_sheet_template_uses_only_reserved_example_addresses():
    """Second line of defence behind the Status guard: RFC 2606 reserves
    example.com precisely so it can never reach a real mailbox."""
    for row in _template_rows()[1:]:
        email = row[sheets.COL_EMAIL]
        assert email.endswith("@example.com"), f"non-reserved address: {email}"


def _template_csv_rows():
    """Parse the shipped CSV the way Sheets' importer would."""
    import csv as csv_mod
    text = sheet_template.build_csv().decode("utf-8-sig")
    return list(csv_mod.reader(io.StringIO(text)))


def test_sheet_template_csv_matches_the_workbook():
    """Two formats, one grid. If they drift, whichever one a user picks stops
    being a faithful description of what the app expects."""
    assert _template_csv_rows() == _template_rows()


def test_sheet_template_csv_rows_are_never_sendable():
    """Same guarantee the workbook carries: a CSV is the likelier thing someone
    imports wholesale without deleting the examples first."""
    rows = [(i, row) for i, row in enumerate(_template_csv_rows()[1:], start=2)]
    with patched(sheets, "get_all_rows", lambda account: rows):
        readiness = sheets.campaign_readiness(ACCOUNT)
    assert readiness["eligible_total"] == 0, (
        f"a demo row in the CSV is queued to send: {readiness['eligible']!r}"
    )


def test_sheet_template_csv_starts_with_a_bom():
    """Without the BOM Excel guesses the local codepage and mangles the file."""
    assert sheet_template.build_csv().startswith(b"\xef\xbb\xbf")


def test_sheet_template_is_valid_xlsx_with_leads_tab_first():
    """The zip and XML are hand-rolled, so check the parts parse -- and that the
    leads grid is the FIRST tab, because SHEET_RANGE ("A:I") is unqualified and
    Sheets resolves it against the leftmost tab."""
    import xml.etree.ElementTree as ET

    archive = zipfile.ZipFile(io.BytesIO(sheet_template.build_xlsx()))
    assert archive.testzip() is None
    for name in archive.namelist():
        ET.fromstring(archive.read(name))  # raises on malformed XML
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    tabs = [el.get("name") for el in workbook.iter() if el.tag.endswith("}sheet")]
    assert tabs[0] == sheet_template.DATA_SHEET
    assert sheet_template.NOTES_SHEET in tabs


# --------------------------------------------------------- public asset gating

def test_public_asset_allows_landing_and_app_bundles():
    import server
    assert server._is_public_asset("/static/landing/index.html")
    assert server._is_public_asset("/static/app/assets/settings-abc123.js")


def test_public_asset_refuses_traversal_out_of_a_public_prefix():
    """The public prefixes grew to cover the guide's React bundle, so the
    normpath guard has to hold on both. Without it StaticFiles resolves the
    '..' back into an auth-gated page template and serves it unauthenticated."""
    import server
    for path in (
        "/static/landing/../outreach.html",
        "/static/app/assets/../settings.html",
        "/static/app/assets/../../outreach.html",
        "/static/app/assets/../../../server.py",
    ):
        assert not server._is_public_asset(path), path


def test_public_asset_does_not_expose_app_page_shells():
    """Only assets/ is public. The page templates themselves stay gated, so
    widening the prefix cannot hand out a rendered app page."""
    import server
    assert not server._is_public_asset("/static/app/settings.html")
    assert not server._is_public_asset("/static/outreach.html")


def test_public_paths_cover_every_link_the_marketing_site_exposes():
    """Each of these is linked from the landing footer or nav, so a logged-out
    prospect has to reach it. /privacy additionally gates Google's OAuth
    verification, which gates billing."""
    import server
    for path in ("/privacy", "/terms", "/acceptable-use", "/dpa", "/security",
                 "/getting-started", "/static/legal.css"):
        assert path in server.PUBLIC_PATHS, path


def test_api_docs_are_closed_by_default():
    """Left on, /docs and /openapi.json hand every tenant a map of the whole
    API. They are opt-in via ENABLE_API_DOCS."""
    import server
    if not server._API_DOCS:
        assert server.app.openapi_url is None
        assert server.app.docs_url is None
        assert server.app.redoc_url is None


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
