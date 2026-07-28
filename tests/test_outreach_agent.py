"""Test suite for the outreach agent. All external boundaries (Gmail API,
OpenRouter, Supabase, Google Sheets) are faked -- nothing touches the network.

Run:  python tests/test_outreach_agent.py
"""
import base64
import contextlib
import hashlib
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
import bounces
import config
import dns_check
import drafts_db
import gmail
import providers
import ratelimit
import reviews_db
import sheet_template
import sheets
import send_outreach
import suppressions_db
import watch_replies

import plans
import usage

# Plan quotas are read before nearly every operation (prepare_drafts,
# find_leads) and metered after nearly every success, so without a default these
# two Supabase calls would need faking in ~20 individual tests -- and a single
# omission is a real network call, which the docstring above forbids. Defaulted
# to "nothing used yet, record nothing"; the quota tests below override them
# with counts that actually bite. Safe to stub globally because no other test
# exercises these two functions: the ones that cover accounts_db patch
# _get_client instead.
accounts_db.usage_quantity_since = lambda account_id, kind, since_iso=None: 0
accounts_db.record_usage = lambda *a, **k: None


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


def msg(from_addr, text, msg_id=None):
    """A thread message. Every real Gmail message carries an id and the reply
    dedupe key is built on it, so the default derives one deterministically from
    the content rather than leaving it absent -- a fixture without an id would
    silently exercise the legacy body-matching fallback instead of the real
    path. Pass msg_id explicitly to model two distinct messages with identical
    text, which is exactly the collision the body key could not survive."""
    digest = hashlib.md5(f"{from_addr}|{text}".encode()).hexdigest()[:12]
    return {
        "id": msg_id or f"m-{digest}",
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
        reply, history, message_id = gmail.get_latest_reply_with_history(
            ACCOUNT, "t1", "john@x.com")
    assert reply == "their reply"
    assert history == [(False, "our outreach")]
    assert message_id == messages[-1]["id"], (
        "the reply's own Gmail message id is the dedupe key -- returning None "
        "here silently drops the review queue back to matching on body text"
    )


def test_history_latest_is_ours_returns_none():
    messages = [msg("me@me.com", "outreach"), msg("john@x.com", "reply"), msg("me@me.com", "our answer")]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        reply, history, message_id = gmail.get_latest_reply_with_history(
            ACCOUNT, "t1", "john@x.com")
    assert reply is None and history == [] and message_id is None


def test_history_single_message_returns_none():
    with patched(gmail, "_get_service", lambda account: fake_gmail_service([msg("me@me.com", "outreach")])):
        assert gmail.get_latest_reply_with_history(ACCOUNT, "t1", "j@x.com") == (None, [], None)


def test_history_contact_match_case_insensitive():
    messages = [msg("me@me.com", "outreach"), msg("John@X.com", "yes!")]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        reply, _, _ = gmail.get_latest_reply_with_history(ACCOUNT, "t1", " john@x.com ")
    assert reply == "yes!"


def test_history_multi_turn_labels():
    messages = [
        msg("me@me.com", "outreach"),
        msg("john@x.com", "question?"),
        msg("me@me.com", "answer"),
        msg("john@x.com", "follow-up"),
    ]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        reply, history, _ = gmail.get_latest_reply_with_history(ACCOUNT, "t1", "john@x.com")
    assert reply == "follow-up"
    assert [h[0] for h in history] == [False, True, False]
    assert [h[1] for h in history] == ["outreach", "question?", "answer"]


def test_get_latest_reply_delegates():
    messages = [msg("me@me.com", "outreach"), msg("john@x.com", "their reply")]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        assert gmail.get_latest_reply(ACCOUNT, "t1", "john@x.com") == "their reply"


# ------------------------------------------------- reply dedupe key

class _ReviewRows:
    """PostgREST-shaped fake: records the .eq()/.is_() filters applied and
    returns the rows satisfying all of them. Filters reset per execute() because
    find_review_id issues two queries -- the id lookup, then the legacy body
    fallback -- against the same client."""

    def __init__(self, rows):
        self.rows = rows
        self._eq = []
        self._null = []

    def table(self, name): return self
    def select(self, cols): return self
    def limit(self, n): return self

    def eq(self, col, val):
        self._eq.append((col, val))
        return self

    def is_(self, col, val):
        self._null.append(col)
        return self

    def execute(self):
        matched = [
            r for r in self.rows
            if all(r.get(c) == v for c, v in self._eq)
            and all(r.get(c) is None for c in self._null)
        ]
        self._eq, self._null = [], []
        return type("R", (), {"data": matched})()


def test_reply_dedupe_keys_on_the_message_id_not_the_body():
    """Two identical short replies on one thread are two different Gmail
    messages and both must surface. Under the old body key the second collided
    with the first and was silently classified as already-reviewed -- a warm
    reply the operator never saw. Walking every message on a thread instead of
    only the newest turns that collision from theoretical into routine."""
    fake = _ReviewRows([
        {"id": 7, "account_id": "a1", "thread_id": "t1",
         "gmail_message_id": "m-first", "customer_reply": "ok"},
    ])
    with patched(reviews_db, "_get_client", lambda: fake):
        assert reviews_db.find_review_id("a1", "t1", "m-first", "ok") == 7
        assert reviews_db.find_review_id("a1", "t1", "m-second", "ok") is None, (
            "same body, different message -- must not be swallowed as a duplicate"
        )


def test_reply_dedupe_still_matches_rows_written_before_the_id_column():
    """Rows created before gmail_message_id existed hold NULL there and can
    never match an id lookup, so without the body fallback every thread already
    carrying a review would emit one duplicate on the first cycle after the
    migration. The fallback is scoped to NULL-id rows only, so it cannot
    resurrect the collision the id key was introduced to fix."""
    fake = _ReviewRows([
        {"id": 3, "account_id": "a1", "thread_id": "t1",
         "gmail_message_id": None, "customer_reply": "sounds good"},
    ])
    with patched(reviews_db, "_get_client", lambda: fake):
        assert reviews_db.find_review_id("a1", "t1", "m-new", "sounds good") == 3
        assert reviews_db.find_review_id("a1", "t1", "m-new") is None, (
            "with no body supplied there is nothing to fall back to -- the "
            "legacy query must not run and match on account+thread alone"
        )


# ------------------------------------------------- edit-pair capture

class _Capture:
    """Captures the row handed to .insert()/.update() so a test can assert on
    what was actually written, not merely that a write happened."""

    def __init__(self):
        self.inserted = None
        self.updated = None

    def table(self, name): return self
    def eq(self, col, val): return self
    def insert(self, row): self.inserted = row; return self
    def update(self, row): self.updated = row; return self
    def execute(self): return type("R", (), {"data": [{"id": 1}]})()


def test_the_model_draft_survives_the_operator_edit():
    """mark_sent overwriting subject/body is correct -- they are the record of
    what was actually sent. The bug was that they were the ONLY record, so every
    edit destroyed the pair (what the model wrote, what this person actually
    says) instead of capturing it. That pair is the entire training signal and
    it cannot be backfilled, which is why capture ships before anything that
    reads it."""
    cap = _Capture()
    with patched(drafts_db, "_get_client", lambda: cap), \
         patched(drafts_db, "has_pending_for_row", lambda a, r: False):
        drafts_db.add_draft("a1", 2, "John", "j@x.com", "Acme",
                            "Model subject", "Model body")
    assert cap.inserted["original_subject"] == "Model subject"
    assert cap.inserted["original_body"] == "Model body"

    with patched(drafts_db, "_get_client", lambda: cap):
        drafts_db.mark_sent("a1", 1, "Operator subject", "Operator body")
    assert cap.updated["subject"] == "Operator subject", "the sent copy is recorded"
    assert "original_subject" not in cap.updated, (
        "mark_sent must never touch the frozen original -- overwriting both "
        "columns is exactly the data loss this capture exists to stop"
    )
    assert "original_body" not in cap.updated


def test_the_model_reply_survives_the_operator_edit():
    """Same loss on the reply path. reviews.mark_sent overwrites draft_reply
    with whatever the operator sent, so without original_draft_reply the model's
    proposal is gone the moment a reply goes out."""
    cap = _Capture()
    with patched(reviews_db, "find_review_id", lambda *a, **k: None), \
         patched(reviews_db, "_get_client", lambda: cap):
        reviews_db.add_review("a1", 2, "John", "j@x.com", "t1",
                              "their reply", "Model draft",
                              gmail_message_id="m1")
    assert cap.inserted["original_draft_reply"] == "Model draft"

    with patched(reviews_db, "_get_client", lambda: cap):
        reviews_db.mark_sent("a1", 1, "What the operator actually sent")
    assert cap.updated["draft_reply"] == "What the operator actually sent"
    assert "original_draft_reply" not in cap.updated


# ---------------------------------------------------------------- draft_reply

def test_draft_reply_prompt_contents():
    captured = {}

    def fake_chat(system, user_prompt, purpose):
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
    with patched(agent, "_chat", lambda s, u, p: captured.update(prompt=u) or "ok"):
        agent.draft_reply(account, "John", "", "hi", [])
    assert "Prospect: John\n" in captured["prompt"]
    assert " at " not in captured["prompt"].split("\n")[3]
    assert "(none set)" in captured["prompt"]


def test_draft_reply_empty_history_bodies_filtered():
    captured = {}
    with patched(agent, "_chat", lambda s, u, p: captured.update(prompt=u) or "ok"):
        agent.draft_reply(ACCOUNT, "J", "", "reply", [(False, "outreach"), (True, "   ")])
    assert captured["prompt"].count("They wrote:") == 0
    assert captured["prompt"].count("We wrote:") == 1


# ------------------------------------------------------- generate_outreach_email

def test_generate_outreach_wellformed():
    with patched(agent, "_chat", lambda s, u, p: GOOD_EMAIL):
        subject, body = agent.generate_outreach_email(ACCOUNT, "John", "Acme")
    assert subject == "Cutting invoice matching"
    assert "https://cal.com/oanh/15min" in body
    assert body.startswith("Hi John")


def test_generate_outreach_single_newline_output():
    # LLMs frequently emit "Subject: X\nBody..." without the blank line.
    with patched(agent, "_chat", lambda s, u, p: GOOD_EMAIL.replace("\n\n", "\n", 1)):
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
    with patched(agent, "_chat", lambda s, u, p: captured.update(prompt=u) or no_link_email):
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
    with patched(agent, "_chat", lambda s, u, p: GOOD_EMAIL):
        try:
            agent.generate_outreach_email(account, "John", "Acme")
        except RuntimeError as e:
            assert "Settings" in str(e)
            return
    raise AssertionError("default meeting purpose must block generation")


def test_generate_outreach_passes_lead_reason_to_prompt():
    captured = {}
    def fake_chat(system, user_prompt, purpose):
        captured["prompt"] = user_prompt
        return GOOD_EMAIL
    with patched(agent, "_chat", fake_chat):
        agent.generate_outreach_email(ACCOUNT, "John", "Acme", lead_reason="spoke at RevOps summit")
    assert "spoke at RevOps summit" in captured["prompt"]
    assert "Research note" in captured["prompt"]


def test_generate_outreach_omits_lead_reason_when_blank():
    captured = {}
    with patched(agent, "_chat", lambda s, u, p: captured.update(prompt=u) or GOOD_EMAIL):
        agent.generate_outreach_email(ACCOUNT, "John", "Acme", lead_reason="")
    assert "Research note" not in captured["prompt"]


def test_generate_outreach_embeds_unsubscribe_line():
    unsub = "https://app.example.com/unsubscribe?t=abc.def"
    with patched(agent, "_chat", lambda s, u, p: GOOD_EMAIL):
        _, body = agent.generate_outreach_email(ACCOUNT, "John", "Acme", unsubscribe_url=unsub)
    assert unsub in body


def test_custom_instructions_reach_the_outreach_prompt():
    captured = {}
    account = dict(ACCOUNT, custom_instructions="Casual founder-to-founder tone. Mention we're YC-backed.")
    with patched(agent, "_chat", lambda s, u, p: captured.update(prompt=u) or GOOD_EMAIL):
        agent.generate_outreach_email(account, "John", "Acme")
    assert "YC-backed" in captured["prompt"]
    assert "sender gave these instructions" in captured["prompt"].lower()


def test_custom_instructions_omitted_when_blank():
    captured = {}
    with patched(agent, "_chat", lambda s, u, p: captured.update(prompt=u) or GOOD_EMAIL):
        agent.generate_outreach_email(ACCOUNT, "John", "Acme")  # fixture has none
    assert "sender gave these instructions" not in captured["prompt"].lower()


def test_custom_instructions_are_length_capped():
    ctx = agent._sender_context(dict(ACCOUNT, custom_instructions="x" * 5000))
    assert len(ctx["custom_instructions"]) == 600, "must be bounded so it can't swamp the prompt"


def test_custom_instructions_reach_the_reply_prompt():
    captured = {}
    account = dict(ACCOUNT, custom_instructions="Keep replies to two short lines.")
    with patched(agent, "_chat", lambda s, u, p: captured.update(prompt=u) or "ok"):
        agent.draft_reply(account, "John", "Acme", "sounds good", [])
    assert "two short lines" in captured["prompt"]


def test_generate_outreach_retries_then_raises_on_bad_output():
    calls = {"n": 0}
    def bad_chat(system, user_prompt, purpose):
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
    with patched(agent, "_chat", lambda s, u, p: next(outputs)):
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
        r = sheets.campaign_readiness(ACCOUNT, sent_today=22)
    assert r["sent_today"] == 22
    assert r["sheet_sent_today"] == 22, "reported for display; both agree here"
    assert r["daily_limit"] == sheets.DAILY_SEND_LIMIT
    assert r["remaining_today"] == max(0, sheets.DAILY_SEND_LIMIT - 22)
    assert len(r["eligible"]) == min(10, r["remaining_today"])
    assert r["eligible_total"] == 10
    assert r["capped"] == max(0, 10 - r["remaining_today"])


def test_campaign_readiness_has_no_default_for_the_send_count():
    """Guards this whole class against regressing. The same bug appeared four
    times -- bounce watermark, bounce numerator, and twice around the cap -- and
    every instance was a gating value that fell back to sheet-derived data. A
    default here would restore it silently, so sent_today must stay required and
    every caller must keep stating where its number came from."""
    import inspect
    sig = inspect.signature(sheets.campaign_readiness)
    param = sig.parameters["sent_today"]
    assert param.default is inspect.Parameter.empty, (
        "sent_today must have no default; a fallback to the sheet-derived count "
        "is the bug this parameter exists to remove"
    )
    try:
        sheets.campaign_readiness(ACCOUNT)
        assert False, "calling without sent_today must fail loudly"
    except TypeError:
        pass


def test_the_allowance_ignores_what_the_sheet_says_about_sends():
    """The fourth instance of the same class, and the one that was live. The cap
    was DAILY_SEND_LIMIT minus a count of sheet SentAt values, so deleting the
    Sent rows, clearing that column, or merely changing its date format handed
    back a full day's allowance -- on a domain-reputation control. The count now
    comes from the send log, which the operator cannot edit."""
    scrubbed = [(i, _row(status="Sent", sent_at="")) for i in range(2, 27)]     # 25 sent, no dates
    reformatted = [(i, _row(status="Sent", sent_at="07/26/2026")) for i in range(2, 27)]
    deleted = []                                                                # rows removed
    for label, rows in (("cleared", scrubbed), ("reformatted", reformatted), ("deleted", deleted)):
        with patched(sheets, "get_all_rows", lambda account: rows):
            r = sheets.campaign_readiness(ACCOUNT, sent_today=25)
        assert r["sheet_sent_today"] == 0, f"{label}: the sheet genuinely reports none"
        assert r["remaining_today"] == 0, f"{label}: the allowance must still be spent"
        assert r["eligible"] == [], f"{label}: nothing may be queued"


def test_campaign_readiness_excludes_suppressed():
    rows = [(2, _row(email="keep@x.com")), (3, _row(email="Gone@x.com"))]
    with patched(sheets, "get_all_rows", lambda account: rows):
        r = sheets.campaign_readiness(ACCOUNT, sent_today=0, suppressed_emails={"gone@x.com"})
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

def _watch_env(calls, reply_map, existing_reviews=None, bounce_map=None):
    """Builds patches for a check_for_replies run. reply_map: thread_id -> (reply, history).
    bounce_map: thread_id -> the dict gmail.find_bounce would return."""
    existing = existing_reviews if existing_reviews is not None else {}
    bounces_by_thread = bounce_map or {}

    def fake_add_review(account_id, row_index, name, email, thread_id, customer_reply,
                        draft_reply, gmail_message_id=None):
        calls.setdefault("add_review", []).append((thread_id, customer_reply, draft_reply))
        key = (thread_id, gmail_message_id)
        if key in existing:
            return existing[key]
        existing[key] = len(existing) + 1
        return existing[key]

    def fake_get_reply(account, t, e, thread=None):
        result = reply_map.get(t, (None, [], None))
        if isinstance(result, Exception):
            raise result
        # reply_map entries are written as (reply, history) at the call sites --
        # synthesize the message id here so adding the dedupe key did not force
        # an edit to all seven tests that share this fixture. One stable id per
        # thread is the right model: the same reply re-read on a later poll is
        # the same Gmail message.
        if len(result) == 2:
            reply, history = result
            return reply, history, (f"msg-{t}" if reply else None)
        return result

    def fake_get_thread(account, t):
        # Counted so a regression back to two full fetches per row is visible.
        calls.setdefault("thread_reads", []).append(t)
        return {"messages": [], "id": t}

    return [
        (reviews_db, "find_review_id",
            lambda account_id, t, mid, reply=None: existing.get((t, mid))),
        (sheets, "require_full_header", lambda account: calls.setdefault("header_gate", []).append(True)),
        (sheets, "get_reply_check_rows", lambda account: calls["rows"]),
        (sheets, "update_row", lambda account, idx, **kw: calls.setdefault("update_row", []).append((idx, kw))),
        (gmail, "get_thread", fake_get_thread),
        (gmail, "get_latest_reply_with_history", fake_get_reply),
        (gmail, "find_bounce", lambda account, t, e, thread=None: bounces_by_thread.get(t)),
        (suppressions_db, "add", lambda account_id, email, source="unsubscribe_link":
            calls.setdefault("suppressed", []).append((email, source))),
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
        stack.enter_context(patched(suppressions_db, "list_source_timestamps", lambda aid, src: []))
        stack.enter_context(patched(drafts_db, "count_sent_last_24_hours", lambda aid: 0))
        stack.enter_context(patched(drafts_db, "count_sent_since", lambda aid, since: 0))
        # Quota faked at the USAGE layer, not by stubbing plans.headroom: a
        # fixture that says "quota never blocks" would make this test assert
        # behaviour under infinite quota, and the enforcement branch would be
        # exercised by nothing. Real plan rules run; there is simply room left.
        stack.enter_context(_quota_used(**{usage.UNIT_DRAFT_EMAIL: 0}))
        stack.enter_context(patched(sheets, "require_full_header", lambda account: calls.update(header_gate=True)))
        stack.enter_context(patched(suppressions_db, "list_suppressed_emails", lambda aid: set()))
        stack.enter_context(patched(sheets, "campaign_readiness", lambda account, sent_today=0, suppressed_emails=None: _readiness(rows)))
        stack.enter_context(patched(sheets, "get_all_rows", lambda account: []))  # no send history yet -> bounce guard is a no-op
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
        stack.enter_context(patched(suppressions_db, "list_source_timestamps", lambda aid, src: []))
        stack.enter_context(patched(drafts_db, "count_sent_last_24_hours", lambda aid: 0))
        stack.enter_context(patched(drafts_db, "count_sent_since", lambda aid, since: 0))
        stack.enter_context(patched(sheets, "require_full_header", lambda account: None))
        stack.enter_context(patched(suppressions_db, "list_suppressed_emails", lambda aid: set()))
        stack.enter_context(patched(sheets, "campaign_readiness", lambda account, sent_today=0, suppressed_emails=None: _readiness(rows)))
        stack.enter_context(patched(sheets, "get_all_rows", lambda account: []))  # no send history yet -> bounce guard is a no-op
        stack.enter_context(patched(drafts_db, "has_pending_for_row", lambda aid, idx: True))  # already queued
        stack.enter_context(patched(agent, "generate_outreach_email", lambda *a, **k: ("S", "B")))
        stack.enter_context(patched(send_outreach, "notify", lambda *a: None))
        result = send_outreach.prepare_drafts(ACCOUNT)
    assert result["prepared"] == 0 and result["total"] == 0


def test_prepare_drafts_nothing_eligible():
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(suppressions_db, "list_source_timestamps", lambda aid, src: []))
        stack.enter_context(patched(drafts_db, "count_sent_last_24_hours", lambda aid: 0))
        stack.enter_context(patched(drafts_db, "count_sent_since", lambda aid, since: 0))
        stack.enter_context(patched(sheets, "require_full_header", lambda account: None))
        stack.enter_context(patched(suppressions_db, "list_suppressed_emails", lambda aid: set()))
        stack.enter_context(patched(sheets, "campaign_readiness", lambda account, sent_today=0, suppressed_emails=None: _readiness([])))
        stack.enter_context(patched(sheets, "get_all_rows", lambda account: []))  # no send history yet -> bounce guard is a no-op
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


_DRAFT_TO_SEND = {"id": 5, "row_index": 2, "email": "j@x.com", "subject": "Hi",
                  "body": "Body without a link"}


def _send_env(stack, order, sheet_raises=None, thread_id="t-9"):
    """Patches the three writes a send performs, recording their order. order
    receives ("gmail"|"queue"|"sheet", detail) tuples."""
    stack.enter_context(patched(send_outreach, "_MARK_RETRY_DELAY", 0))
    stack.enter_context(patched(drafts_db, "require_send_log", lambda: None))
    stack.enter_context(patched(auth, "unsubscribe_url", lambda aid, e: "https://u/unsub?t=x.y"))

    def fake_send(account, to, s, b, unsubscribe_url=""):
        order.append(("gmail", {"to": to, "body": b, "header": unsubscribe_url}))
        return thread_id

    def fake_mark_sent(aid, did, subject, body):
        order.append(("queue", {"draft_id": did, "subject": subject, "body": body}))

    def fake_update_row(account, idx, **kw):
        order.append(("sheet", {"row": idx, **kw}))
        if sheet_raises:
            raise sheet_raises

    stack.enter_context(patched(gmail, "send_email", fake_send))
    stack.enter_context(patched(drafts_db, "mark_sent", fake_mark_sent))
    stack.enter_context(patched(sheets, "update_row", fake_update_row))


def test_send_prepared_draft_sends_marks_and_reappends_optout():
    order = []
    with contextlib.ExitStack() as stack:
        _send_env(stack, order)
        result = send_outreach.send_prepared_draft(ACCOUNT, dict(_DRAFT_TO_SEND))
    assert result["thread_id"] == "t-9" and result["sheet_error"] is None
    gmail_call = dict(order)["gmail"]
    assert gmail_call["header"] == "https://u/unsub?t=x.y", "List-Unsubscribe header must be set"
    assert "https://u/unsub?t=x.y" in result["body"], "opt-out line must be appended when missing"
    sheet_call = dict(order)["sheet"]
    assert sheet_call["row"] == 2 and sheet_call["status"] == "Sent"
    assert sheet_call["expect_email"] == "j@x.com"


def test_a_send_refuses_to_start_when_it_could_not_be_recorded():
    """The migration this needs could itself have reintroduced the bug. mark_sent
    writes sent_at, and mark_sent is what stops a re-send -- so on an unmigrated
    database the recording write fails AFTER Gmail accepted the message. Checked
    before the irreversible act, and deliberately not degraded into a fallback
    that drops sent_at: that would trade duplicate sends for an uncounted daily
    cap, which is the reputation control."""
    sent = []

    def unmigrated():
        raise RuntimeError("column outreach_drafts.sent_at does not exist")

    # The real require_send_log, against a database that answers like an
    # un-migrated one. Not routed through _send_env, which stubs it out.
    with patched(drafts_db, "_send_log_verified", False), \
         patched(drafts_db, "_get_client", unmigrated), \
         patched(gmail, "send_email", lambda *a, **k: sent.append(a) or "t"):
        try:
            send_outreach.send_prepared_draft(ACCOUNT, dict(_DRAFT_TO_SEND))
            assert False, "must refuse before sending"
        except RuntimeError as e:
            assert "sent_at is missing" in str(e)
            assert "alter table public.outreach_drafts" in str(e)
    assert sent == [], "no email may leave when the send could not be recorded"


def test_the_send_log_check_is_cached_after_it_passes():
    """One query per process, not per send -- it cannot become false mid-run."""
    calls = []

    class _Ok:
        def table(self, n): return self
        def select(self, c): return self
        def limit(self, n): return self
        def execute(self): calls.append(1); return type("R", (), {"data": []})()

    with patched(drafts_db, "_send_log_verified", False), \
         patched(drafts_db, "_get_client", lambda: _Ok()):
        drafts_db.require_send_log()
        drafts_db.require_send_log()
        drafts_db.require_send_log()
    assert len(calls) == 1, calls


def test_the_send_is_recorded_in_the_queue_before_the_sheet_is_touched():
    """The order is the invariant. The Gmail send is irreversible, so the record
    that prevents a second one has to be the very next write -- and it has to be
    the reliable store, not the spreadsheet the operator edits underneath us."""
    order = []
    with contextlib.ExitStack() as stack:
        _send_env(stack, order)
        send_outreach.send_prepared_draft(ACCOUNT, dict(_DRAFT_TO_SEND))
    assert [step for step, _ in order] == ["gmail", "queue", "sheet"]


def test_a_failed_sheet_write_cannot_leave_the_draft_pending():
    """The bug: sheets.update_row re-verifies the row by address and raises
    permanently when the operator has deleted it, so all three retries fail
    identically. With the sheet written first, the email had gone out, the queue
    update never ran, the draft stayed pending, and the next batch sent it again.
    Now the queue update is already done and the sheet failure is a repair task."""
    order = []
    deleted_row = RuntimeError(
        "Couldn't find j@x.com in the sheet anymore — it may have been edited or the row deleted."
    )
    with contextlib.ExitStack() as stack:
        _send_env(stack, order, sheet_raises=deleted_row)
        result = send_outreach.send_prepared_draft(ACCOUNT, dict(_DRAFT_TO_SEND))

    steps = [step for step, _ in order]
    assert steps.count("gmail") == 1, "exactly one email"
    assert "queue" in steps, "the draft MUST be retired even though the sheet write failed"
    assert steps.index("queue") < steps.index("sheet")
    assert result["sheet_error"] is not None
    assert "was emailed successfully" in result["sheet_error"]
    assert "not be emailed again" in result["sheet_error"], "tell the operator not to re-send"


def test_send_all_prepared_skips_suppressed_and_respects_cap():
    drafts = [
        {"id": 1, "row_index": 2, "email": "a@x.com", "subject": "S", "body": "b"},
        {"id": 2, "row_index": 3, "email": "b@x.com", "subject": "S", "body": "b"},
        {"id": 3, "row_index": 4, "email": "c@x.com", "subject": "S", "body": "b"},
    ]
    sent, discarded, marked = [], [], []
    readiness_calls = {"n": 0}
    def fake_readiness(account, sent_today=0, **k):
        readiness_calls["n"] += 1
        return {"remaining_today": 1}  # only room for one send
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(suppressions_db, "list_source_timestamps", lambda aid, src: []))
        stack.enter_context(patched(drafts_db, "count_sent_last_24_hours", lambda aid: 0))
        stack.enter_context(patched(drafts_db, "count_sent_since", lambda aid, since: 0))
        stack.enter_context(patched(send_outreach, "_SEND_ALL_MIN_GAP", 0))
        stack.enter_context(patched(send_outreach, "_SEND_ALL_MAX_GAP", 0))
        stack.enter_context(patched(drafts_db, "list_pending_drafts", lambda aid: drafts))
        stack.enter_context(patched(suppressions_db, "is_suppressed", lambda aid, email: email == "a@x.com"))
        stack.enter_context(patched(drafts_db, "discard", lambda aid, did: discarded.append(did)))
        stack.enter_context(patched(sheets, "campaign_readiness", fake_readiness))
        stack.enter_context(patched(sheets, "get_all_rows", lambda account: []))  # no send history yet -> bounce guard is a no-op
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


def _sendall_env(stack, drafts, rows, outcomes, sent_today=0):
    """send_all_prepared with everything external faked. outcomes maps an email
    to the exception send_prepared_draft should raise for it, or to a
    sheet_error string; anything absent sends cleanly."""
    log = {"sent": [], "discarded": []}
    stack.enter_context(patched(send_outreach, "_SEND_ALL_MIN_GAP", 0))
    stack.enter_context(patched(send_outreach, "_SEND_ALL_MAX_GAP", 0))
    stack.enter_context(patched(send_outreach, "notify", lambda *a: None))
    stack.enter_context(patched(drafts_db, "list_pending_drafts", lambda aid: drafts))
    stack.enter_context(patched(drafts_db, "discard",
                                lambda aid, did: log["discarded"].append(did)))
    stack.enter_context(patched(suppressions_db, "is_suppressed", lambda aid, e: False))
    stack.enter_context(patched(suppressions_db, "list_source_timestamps", lambda aid, s: []))
    stack.enter_context(patched(drafts_db, "count_sent_last_24_hours", lambda aid: sent_today))
    stack.enter_context(patched(drafts_db, "count_sent_since", lambda aid, since: 0))
    stack.enter_context(patched(sheets, "get_all_rows", lambda account: rows))

    def fake_send(account, draft, subject=None, body=None):
        outcome = outcomes.get(draft["email"])
        if isinstance(outcome, Exception):
            raise outcome
        log["sent"].append(draft["email"])
        return {"thread_id": "t", "body": draft["body"], "sheet_error": outcome}

    stack.enter_context(patched(send_outreach, "send_prepared_draft", fake_send))
    return log


def test_the_daily_cap_counts_emails_sent_not_sheet_writes():
    """The cap is a domain-reputation control, so it has to count what left the
    building. Decrementing only after a successful sheet write let a run of sheet
    failures under-count and send past the limit."""
    # Row indexes kept clear of the history rows below, or the already-emailed
    # guard skips these before the cap is ever consulted.
    drafts = [{"id": i, "row_index": 100 + i, "email": f"p{i}@x.com", "subject": "S", "body": "b"}
              for i in range(1, 4)]
    # Every send succeeds; every sheet write fails.
    outcomes = {d["email"]: "sent, but the sheet row was not updated" for d in drafts}
    # 24 sends already in the send log today; cap 25 -> room for exactly one.
    rows = [(i, _row(status="Sent", email=f"h{i}@x.com", sent_at=_days_ago(0)))
            for i in range(1, 25)]
    with contextlib.ExitStack() as stack:
        log = _sendall_env(stack, drafts, rows, outcomes, sent_today=24)
        result = send_outreach.send_all_prepared(ACCOUNT)
    assert len(log["sent"]) == 1, f"cap must stop at one; sent {log['sent']}"
    assert result["sent"] == 1
    assert result["failed"] == [], "a sheet write failure is not a failed send"
    assert len(result["sheet_warnings"]) == 1
    assert any(s["reason"] == "daily limit reached" for s in result["skipped"])


def test_send_all_skips_a_draft_whose_row_already_says_sent():
    """Second, independent layer. If the queue says pending but the sheet says
    Sent, the only way to get there is a send whose queue update failed -- so the
    email is out and this draft must be retired, not sent again. Uses the sheet
    read the batch already does."""
    drafts = [
        {"id": 1, "row_index": 2, "email": "already@x.com", "subject": "S", "body": "b"},
        {"id": 2, "row_index": 3, "email": "fresh@x.com", "subject": "S", "body": "b"},
    ]
    rows = [(2, _row(status="Sent", email="already@x.com", sent_at=_days_ago(0))),
            (3, _row(status="", email="fresh@x.com"))]
    with contextlib.ExitStack() as stack:
        log = _sendall_env(stack, drafts, rows, {})
        result = send_outreach.send_all_prepared(ACCOUNT)
    assert log["sent"] == ["fresh@x.com"]
    assert log["discarded"] == [1], "the already-sent draft is retired, not re-sent"
    assert any(s["reason"] == "already marked Sent in the sheet" for s in result["skipped"])


def test_a_deleted_sheet_row_does_not_start_a_resend_loop():
    """End to end, the failure the ordering fix exists to prevent. The operator
    deletes a row after preparing drafts -- which the old bounce-pause message
    literally told them to do. Before: the email went out, the sheet write raised
    permanently, the draft stayed pending, and EVERY subsequent batch sent the
    same email to the same prospect. Two batches, one email."""
    draft = {"id": 1, "row_index": 2, "email": "j@x.com", "subject": "S", "body": "b"}
    order = []
    queue_status = {"status": "pending"}

    def pending_drafts(account_id):
        return [dict(draft)] if queue_status["status"] == "pending" else []

    with contextlib.ExitStack() as stack:
        _send_env(stack, order, sheet_raises=RuntimeError("row deleted"))
        stack.enter_context(patched(send_outreach, "_SEND_ALL_MIN_GAP", 0))
        stack.enter_context(patched(send_outreach, "_SEND_ALL_MAX_GAP", 0))
        stack.enter_context(patched(send_outreach, "notify", lambda *a: None))
        stack.enter_context(patched(drafts_db, "list_pending_drafts", pending_drafts))
        stack.enter_context(patched(suppressions_db, "is_suppressed", lambda aid, e: False))
        stack.enter_context(patched(suppressions_db, "list_source_timestamps", lambda aid, s: []))
        stack.enter_context(patched(drafts_db, "count_sent_last_24_hours", lambda aid: 0))
        stack.enter_context(patched(drafts_db, "count_sent_since", lambda aid, since: 0))
        stack.enter_context(patched(sheets, "get_all_rows", lambda account: []))
        # drafts_db.mark_sent takes the draft out of the pending set; reflect that
        # so the second batch sees what production would. Wraps _send_env's
        # recorder rather than replacing it, so the write still shows up in order.
        recorded = drafts_db.mark_sent

        def marking(aid, did, s, b):
            queue_status["status"] = "sent"
            return recorded(aid, did, s, b)

        stack.enter_context(patched(drafts_db, "mark_sent", marking))

        first = send_outreach.send_all_prepared(ACCOUNT)
        second = send_outreach.send_all_prepared(ACCOUNT)

    assert [s for s, _ in order].count("gmail") == 1, (
        "the prospect must be emailed exactly once across both batches"
    )
    assert first["sent"] == 1 and len(first["sheet_warnings"]) == 1
    assert second["sent"] == 0, "nothing left to send"
    assert first["failed"] == [], "delivered mail must not be reported as failed"


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
        stack.enter_context(patched(server.drafts_db, "count_sent_last_24_hours", lambda aid: 0))
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
        stack.enter_context(patched(server.drafts_db, "count_sent_last_24_hours", lambda aid: 0))
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
        stack.enter_context(patched(server.drafts_db, "count_sent_last_24_hours", lambda aid: 0))
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
        stack.enter_context(patched(server.drafts_db, "count_sent_last_24_hours", lambda aid: 0))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: dict(_PENDING_DRAFT)))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(server.suppressions_db, "list_suppressed_emails", lambda aid: set()))
        stack.enter_context(patched(server.sheets, "campaign_readiness", lambda account, **k: {"remaining_today": 5}))

        def fake_send(a, d, subject=None, body=None):
            marked.update(draft_id=d["id"], subject=subject, body=body)
            return {"thread_id": "t-1", "body": "sent body with optout", "sheet_error": None}

        stack.enter_context(patched(server.send_outreach, "send_prepared_draft", fake_send))

        def must_not_be_called(aid, did, s, b):
            raise AssertionError(
                "the endpoint must not record the send itself -- doing it out here "
                "is what let a failed sheet write leave the draft pending"
            )

        stack.enter_context(patched(server.drafts_db, "mark_sent", must_not_be_called))
        result = server.send_draft(_srv_req(), 7, server.SendDraftBody(subject="edited", body="edited body"))
    assert result == {"ok": True, "sheet_warning": None}
    # The operator's edited copy is what gets sent and recorded, and recording it
    # now happens inside send_prepared_draft, immediately after Gmail accepts it.
    assert marked == {"draft_id": 7, "subject": "edited", "body": "edited body"}


def test_send_draft_returns_200_with_a_warning_when_only_the_sheet_failed():
    """The email was delivered. Returning that as an error is what prompted an
    operator to click Send a second time."""
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "count_sent_last_24_hours", lambda aid: 0))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: dict(_PENDING_DRAFT)))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, e: False))
        stack.enter_context(patched(server.suppressions_db, "list_suppressed_emails", lambda aid: set()))
        stack.enter_context(patched(server.sheets, "campaign_readiness", lambda account, **k: {"remaining_today": 5}))
        stack.enter_context(patched(server.send_outreach, "send_prepared_draft",
            lambda a, d, subject=None, body=None: {
                "thread_id": "t-1", "body": "b",
                "sheet_error": "j@x.com was emailed successfully, but its sheet row was not updated",
            }))
        result = server.send_draft(_srv_req(), 7, server.SendDraftBody(subject="s", body="b"))
    assert result["ok"] is True
    assert "emailed successfully" in result["sheet_warning"]


def test_discard_draft_happy_and_404():
    import server
    discarded = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "discard", lambda aid, did: discarded.append(did)))
        stack.enter_context(patched(server.drafts_db, "count_sent_last_24_hours", lambda aid: 0))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: {"id": did, "status": "pending"}))
        assert server.discard_draft(_srv_req(), 5) == {"ok": True}
    assert discarded == [5]
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "count_sent_last_24_hours", lambda aid: 0))
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
    kinds = [a[0] for a in metered_ok]
    assert kinds.count("search") == 1, "a successful search meters exactly once"
    # Tests configure no Tavily price, so its cost lands in the blind-spot
    # bucket rather than being silently recorded as free.
    assert "cost:unpriced" in kinds


# ------------------------------------------------------------------- dns_check

def _dns(mapping=None):
    """Fakes _txt_records: maps a queried name to its TXT strings. Anything not
    listed resolves to [] -- looked up fine, nothing published."""
    mapping = mapping or {}
    return lambda name: mapping.get(name, [])


def test_dns_check_skips_domains_google_manages():
    """A free @gmail.com sender controls no DNS. Warning them about records
    they cannot publish would be pure noise."""
    result = dns_check.check_domain("gmail.com")
    assert result["managed"] is True and result["findings"] == []
    assert "nothing for you to configure" in result["summary"]


def test_dns_check_reports_missing_records():
    with patched(dns_check, "_txt_records", _dns()):
        result = dns_check.check_domain("nowhere.test")
    statuses = {f["name"]: f["status"] for f in result["findings"]}
    assert statuses == {"SPF": "missing", "DKIM": "warning", "DMARC": "missing"}


def test_dns_check_flags_spf_that_does_not_authorise_google():
    mapping = {"acme.test": ["v=spf1 include:mailgun.org -all"]}
    with patched(dns_check, "_txt_records", _dns(mapping)):
        result = dns_check.check_domain("acme.test")
    spf = next(f for f in result["findings"] if f["name"] == "SPF")
    assert spf["status"] == "warning" and "_spf.google.com" in spf["detail"]


def test_dns_check_treats_a_revoked_dkim_key_as_broken():
    """RFC 6376: an empty p= is a REVOKED key. Calling that healthy would be
    worse than reporting nothing -- receivers actively distrust the selector."""
    mapping = {"google._domainkey.acme.test": ["v=DKIM1; p="]}
    with patched(dns_check, "_txt_records", _dns(mapping)):
        result = dns_check.check_domain("acme.test")
    dkim = next(f for f in result["findings"] if f["name"] == "DKIM")
    assert dkim["status"] == "missing" and "revoked" in dkim["detail"]


def test_dns_check_accepts_a_real_dkim_key():
    mapping = {"google._domainkey.acme.test": ["v=DKIM1; k=rsa; p=MIIBIjANBgkq"]}
    with patched(dns_check, "_txt_records", _dns(mapping)):
        result = dns_check.check_domain("acme.test")
    assert next(f for f in result["findings"] if f["name"] == "DKIM")["status"] == "ok"


def test_dns_check_reports_the_dmarc_policy():
    mapping = {"_dmarc.acme.test": ["v=DMARC1; p=reject; rua=mailto:x@acme.test"]}
    with patched(dns_check, "_txt_records", _dns(mapping)):
        result = dns_check.check_domain("acme.test")
    dmarc = next(f for f in result["findings"] if f["name"] == "DMARC")
    assert dmarc["status"] == "ok" and "reject" in dmarc["detail"]


def test_dns_check_does_not_report_missing_when_the_lookup_failed():
    """A network blip must never render as "your domain has no SPF" -- that is
    a false alarm about a domain that may be configured perfectly."""
    with patched(dns_check, "_txt_records", lambda name: None):
        result = dns_check.check_domain("acme.test")
    assert {f["status"] for f in result["findings"]} == {"unknown"}
    assert "Could not complete" in result["summary"]


def test_dns_check_all_clear():
    mapping = {
        "acme.test": ["v=spf1 include:_spf.google.com ~all"],
        "google._domainkey.acme.test": ["v=DKIM1; k=rsa; p=MIIBIjANBgkq"],
        "_dmarc.acme.test": ["v=DMARC1; p=none"],
    }
    with patched(dns_check, "_txt_records", _dns(mapping)):
        result = dns_check.check_domain("acme.test")
    assert {f["status"] for f in result["findings"]} == {"ok"}
    assert "all look correct" in result["summary"]


def test_txt_records_joins_split_chunks():
    """Long TXT values arrive as several quoted strings -- SPF and DKIM records
    routinely exceed the 255-byte per-string limit and must be concatenated."""
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"Answer": [{"data": '"v=DKIM1; p=AAAA" "BBBB"'}]}

    with patched(dns_check.requests, "get", lambda *a, **k: _Resp()):
        assert dns_check._txt_records("x.test") == ["v=DKIM1; p=AAAABBBB"]


def test_txt_records_returns_none_when_the_request_fails():
    def boom(*a, **k):
        raise dns_check.requests.RequestException("network down")

    with patched(dns_check.requests, "get", boom):
        assert dns_check._txt_records("x.test") is None


# --------------------------------------------------------------------- bounces

def bounce_msg(status_code, failed_recipient="john@x.com", sender="mailer-daemon@googlemail.com"):
    """A delivery-status notification shaped the way Gmail sends one: a
    human-readable text/plain part plus a machine-readable
    message/delivery-status sibling that _extract_body() would skip over."""
    return {
        "payload": {
            "headers": [
                {"name": "From", "value": f"Mail Delivery Subsystem <{sender}>"},
                {"name": "Content-Type",
                 "value": 'multipart/report; report-type=delivery-status; boundary="xyz"'},
            ],
            "parts": [
                {"mimeType": "text/plain",
                 "body": {"data": _enc(f"Delivery to {failed_recipient} failed.")}},
                {"mimeType": "message/delivery-status",
                 "body": {"data": _enc(
                     f"Final-Recipient: rfc822; {failed_recipient}\n"
                     f"Action: failed\nStatus: {status_code}\n")}},
            ],
        }
    }


def test_find_bounce_classifies_permanent_and_transient():
    """RFC 3463: 5.x.x is permanent, 4.x.x is a temporary failure."""
    for code, permanent in (("5.1.1", True), ("4.2.2", False)):
        messages = [msg("me@me.com", "outreach"), bounce_msg(code)]
        with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
            found = gmail.find_bounce(ACCOUNT, "t1", "john@x.com")
        assert found is not None, f"{code} should have been detected"
        assert found["permanent"] is permanent and found["code"] == code


def test_find_bounce_ignores_another_recipients_failure():
    """A daemon message naming somebody else must never suppress our contact."""
    messages = [msg("me@me.com", "outreach"), bounce_msg("5.1.1", failed_recipient="other@z.com")]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        assert gmail.find_bounce(ACCOUNT, "t1", "john@x.com") is None


def test_find_bounce_ignores_a_normal_reply():
    """A human reply that happens to contain a version-like string is not a
    bounce -- the sender and the report content-type are what qualify it."""
    messages = [msg("me@me.com", "outreach"), msg("john@x.com", "sounds good, we run 5.1.1 here")]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        assert gmail.find_bounce(ACCOUNT, "t1", "john@x.com") is None


def _bounce_with_original(status_code, received_ip, failed_recipient="john@x.com"):
    """A DSN that quotes our original message back, the way every real one does.

    The quoted copy carries the Received: chain the message picked up in transit,
    which is a list of IP addresses."""
    original = (
        f"Received: from mx.example.com ([{received_ip}])\n"
        f"        by mx.google.com with ESMTPS id abc123;\n"
        f"        Tue, 21 Jul 2026 04:11:09 -0700 (PDT)\n"
        f"To: {failed_recipient}\n"
        "Subject: Invoice reconciliation for finance teams\n"
        "\n"
        "Body of the message we sent.\n"
    )
    message = bounce_msg(status_code, failed_recipient=failed_recipient)
    message["payload"]["parts"].append(
        {"mimeType": "message/rfc822", "body": {"data": _enc(original)}}
    )
    return message


def test_a_routing_ip_in_the_quoted_original_cannot_forge_a_status_code():
    """The bug this test exists for: a status code was found by regexing the
    whole concatenated MIME tree, which includes the verbatim copy of our own
    message that every bounce quotes back. "5.10.20.30" in a Received: header
    contains "5.10.20", a well-formed permanent-failure code -- so a transient
    delay through RIPE space reported permanent: True and suppressed a live
    prospect forever. The status must come from the delivery-status part."""
    messages = [msg("me@me.com", "outreach"),
                _bounce_with_original("4.2.2", received_ip="5.10.20.30")]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        found = gmail.find_bounce(ACCOUNT, "t1", "john@x.com")
    assert found is not None, "the real 4.2.2 should still be detected"
    assert found["code"] == "4.2.2"
    assert found["permanent"] is False, "an IP address must never make a bounce permanent"


def test_a_full_mailbox_does_not_suppress_a_live_prospect():
    """The realistic form of the bug, in Postfix's own wording. Postfix names the
    relaying host and its IP *before* the SMTP code, so regexing the message for
    the first dotted triple found "5.10.20" inside 5.10.20.30 and reported a
    permanent failure -- suppressing forever a prospect whose mailbox was merely
    full. This is the most common non-Gmail bounce layout there is."""
    postfix = {
        "payload": {
            "headers": [
                {"name": "From", "value": "MAILER-DAEMON@mail.sender.com"},
                {"name": "Content-Type",
                 "value": "multipart/report; report-type=delivery-status"},
            ],
            "mimeType": "multipart/report",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _enc(
                    "This is the mail system at host mail.sender.com.\n\n"
                    "I'm sorry to have to inform you that your message could not\n"
                    "be delivered to one or more recipients.\n\n"
                    "<john@x.com>: host mx.x.com[5.10.20.30] said: 452 4.2.2 Mailbox full\n"
                    "    (in reply to RCPT TO command)\n"
                )}},
                {"mimeType": "message/delivery-status", "body": {"data": _enc(
                    "Final-Recipient: rfc822; john@x.com\nAction: failed\nStatus: 4.2.2\n"
                    "Diagnostic-Code: smtp; 452 4.2.2 Mailbox full\n"
                )}},
            ],
        }
    }
    with patched(gmail, "_get_service", lambda account: fake_gmail_service([
        msg("me@me.com", "outreach"), postfix
    ])):
        found = gmail.find_bounce(ACCOUNT, "t1", "john@x.com")
    assert found["code"] == "4.2.2"
    assert found["permanent"] is False, "a full mailbox must never suppress the address"


def test_an_ip_inside_a_diagnostic_code_cannot_forge_a_status():
    """Second pass at the same bug. Anchoring on the field name at line start
    constrains which line is read, not where in it the code may sit -- and a
    lazy scan across the line finds the IP first:

        Diagnostic-Code: smtp; host mx.x.com[5.10.20.30] said: 452 4.2.2 ...

    Proximity to the SMTP reply is the load-bearing part, so the reply-anchored
    pattern has to be tried against the field value before any bare code is."""
    assert gmail._code_in_field_value(
        " smtp; host mx.x.com[5.10.20.30] said: 452 4.2.2 Mailbox full"
    ) == "4.2.2"
    assert gmail._code_in_field_value(" smtp; 550-5.1.1 The account does not exist") == "5.1.1"
    # A code at the start of the value, past the RFC 3464 type label.
    assert gmail._code_in_field_value(" 5.1.1") == "5.1.1"
    assert gmail._code_in_field_value(" smtp; 5.7.1 blocked") == "5.7.1"
    # Neither anchor satisfied: an IP and nothing else. Better to skip.
    assert gmail._code_in_field_value(" x-postfix; host mx[5.10.20.30] refused to talk") is None


def test_status_field_tolerates_trailing_commentary():
    """Some MTAs append text to Status:. Requiring the whole value to be the code
    dropped those into the fallback path unnecessarily."""
    message = bounce_msg("5.1.1")
    message["payload"]["parts"][1]["body"] = {"data": _enc(
        "Final-Recipient: rfc822; john@x.com\nAction: failed\n"
        "Status: 5.1.1 (bad destination mailbox address)\n"
    )}
    with patched(gmail, "_get_service", lambda account: fake_gmail_service([
        msg("me@me.com", "outreach"), message
    ])):
        found = gmail.find_bounce(ACCOUNT, "t1", "john@x.com")
    assert found["code"] == "5.1.1" and found["permanent"] is True


def test_a_full_mailbox_survives_a_dsn_with_no_status_field():
    """The fallback path, end to end, on the shape that exposed the hole: a
    delivery-status part carrying only a Diagnostic-Code, whose quoted server
    response names the relaying host's IP before the code."""
    message = bounce_msg("4.2.2")
    message["payload"]["parts"][1]["body"] = {"data": _enc(
        "Final-Recipient: rfc822; john@x.com\nAction: failed\n"
        "Diagnostic-Code: smtp; host mx.x.com[5.10.20.30] said: 452 4.2.2 Mailbox full\n"
    )}
    message["payload"]["parts"][0]["body"] = {"data": _enc("Delivery to john@x.com deferred.")}
    with patched(gmail, "_get_service", lambda account: fake_gmail_service([
        msg("me@me.com", "outreach"), message
    ])):
        found = gmail.find_bounce(ACCOUNT, "t1", "john@x.com")
    assert found["code"] == "4.2.2"
    assert found["permanent"] is False, "a full mailbox must never suppress the address"


def test_address_matching_is_whole_address_not_substring():
    """This comparison decides whose address gets suppressed, so it cannot be a
    substring test: john@x.com sits inside bjohn@x.com, john@x.com.au and
    x-john@x.com, all of which are different, real people."""
    for text in ("<john@x.com>: host mx said 550", "john@x.com, other@z.com",
                 "To: John@X.com", "mailto:john@x.com>"):
        assert gmail._mentions_address(text, "john@x.com") is True, text
    for text in ("<bjohn@x.com>: host mx said 550", "john@x.com.au failed",
                 "x-john@x.com", "_john@x.com", "john@x.commercial.net"):
        assert gmail._mentions_address(text, "john@x.com") is False, text
        assert "john@x.com" in text.lower(), (
            f"{text!r} must contain the contact as a substring or it proves nothing"
        )
    assert gmail._mentions_address("anything", "") is False


def test_a_similarly_named_recipients_bounce_is_not_ours():
    """The address gate, end to end. A permanent failure for bjohn@x.com must
    not suppress john@x.com -- the same attribution bug as the multi-recipient
    one, one size smaller."""
    messages = [msg("me@me.com", "outreach"), bounce_msg("5.1.1", failed_recipient="bjohn@x.com")]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        assert gmail.find_bounce(ACCOUNT, "t1", "john@x.com") is None
        assert gmail.find_bounce(ACCOUNT, "t1", "bjohn@x.com")["code"] == "5.1.1"


def test_a_similarly_named_recipient_does_not_steal_our_line():
    """Same thing inside the per-line fallback: the bjohn line must not be read
    as ours just because it contains our address as a substring."""
    postfix = {
        "payload": {
            "headers": [
                {"name": "From", "value": "MAILER-DAEMON@mail.sender.com"},
                {"name": "Content-Type", "value": "text/plain"},
            ],
            "mimeType": "text/plain",
            "body": {"data": _enc(
                "<bjohn@x.com>: host mx.x.com[203.0.113.9] said: 550 5.1.1 User unknown\n"
                "<john@x.com>: host mx.x.com[5.10.20.30] said: 452 4.2.2 Mailbox full\n"
            )},
        }
    }
    with patched(gmail, "_get_service", lambda account: fake_gmail_service([
        msg("me@me.com", "outreach"), postfix
    ])):
        found = gmail.find_bounce(ACCOUNT, "t1", "john@x.com")
    assert found["code"] == "4.2.2" and found["permanent"] is False


def test_x_failed_recipients_matches_one_address_out_of_a_list():
    """The header can list several addresses. Bounding the match must not break
    the comma-separated case it is most often used for."""
    message = bounce_msg("5.1.1", failed_recipient="john@x.com")
    message["payload"]["headers"].append(
        {"name": "X-Failed-Recipients", "value": "bjohn@x.com, john@x.com, other@z.com"}
    )
    with patched(gmail, "_get_service", lambda account: fake_gmail_service([
        msg("me@me.com", "outreach"), message
    ])):
        assert gmail.find_bounce(ACCOUNT, "t1", "john@x.com")["code"] == "5.1.1"
        assert gmail.find_bounce(ACCOUNT, "t1", "nobody@x.com") is None


def test_a_multi_recipient_plain_text_bounce_reads_our_own_line():
    """Postfix writes one line per recipient. On a bounce covering several
    addresses with no delivery-status part, scanning the whole message for the
    first code pins the wrong person's failure on our prospect -- here, another
    address's permanent 5.1.1 onto a contact whose mailbox is merely full."""
    postfix = {
        "payload": {
            "headers": [
                {"name": "From", "value": "MAILER-DAEMON@mail.sender.com"},
                {"name": "Content-Type", "value": "text/plain"},
            ],
            "mimeType": "text/plain",
            "body": {"data": _enc(
                "This is the mail system at host mail.sender.com.\n\n"
                "<other@z.com>: host mx.z.com[203.0.113.9] said: 550 5.1.1 User unknown\n"
                "<john@x.com>: host mx.x.com[5.10.20.30] said: 452 4.2.2 Mailbox full\n"
            )},
        }
    }
    with patched(gmail, "_get_service", lambda account: fake_gmail_service([
        msg("me@me.com", "outreach"), postfix
    ])) :
        for email, code, permanent in (("john@x.com", "4.2.2", False),
                                       ("other@z.com", "5.1.1", True)):
            found = gmail.find_bounce(ACCOUNT, "t1", email)
            assert found["code"] == code, (email, found)
            assert found["permanent"] is permanent, (email, found)


def test_a_dsn_that_names_only_other_recipients_is_not_our_bounce():
    """When the report enumerates recipients and ours is not one of them, the
    shared human-readable text must not be consulted -- it describes somebody
    else's failure. Our contact appears here only in the quoted original, which
    is enough to pass find_bounce's address gate."""
    message = bounce_msg("5.1.1", failed_recipient="other@z.com")
    message["payload"]["parts"][0]["body"] = {"data": _enc(
        "Delivery failed permanently: 550 5.1.1 User unknown"
    )}
    message["payload"]["parts"].append({"mimeType": "message/rfc822", "body": {"data": _enc(
        "To: other@z.com, john@x.com\nSubject: outreach\n\nbody\n"
    )}})
    with patched(gmail, "_get_service", lambda account: fake_gmail_service([
        msg("me@me.com", "outreach"), message
    ])):
        assert gmail.find_bounce(ACCOUNT, "t1", "john@x.com") is None
        assert gmail.find_bounce(ACCOUNT, "t1", "other@z.com")["code"] == "5.1.1"


def test_a_routing_ip_cannot_downgrade_a_real_permanent_failure():
    """The same flaw in the other direction: the old code took the *first* match
    anywhere in the tree, so a 4.x.x-looking IP ahead of a genuine 5.1.1 read as
    transient and the dead address was never suppressed."""
    messages = [msg("me@me.com", "outreach"),
                _bounce_with_original("5.1.1", received_ip="4.31.198.44")]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        found = gmail.find_bounce(ACCOUNT, "t1", "john@x.com")
    assert found["code"] == "5.1.1" and found["permanent"] is True


def test_find_bounce_reads_the_status_of_our_own_recipient():
    """A DSN can carry one per-recipient block per address. Reading whichever
    status appears first would classify our contact by someone else's result."""
    message = bounce_msg("5.1.1", failed_recipient="other@z.com")
    message["payload"]["parts"][1]["body"] = {"data": _enc(
        "Final-Recipient: rfc822; other@z.com\nAction: failed\nStatus: 5.1.1\n"
        "\n"
        "Final-Recipient: rfc822; john@x.com\nAction: failed\nStatus: 4.2.2\n"
    )}
    message["payload"]["parts"][0]["body"] = {"data": _enc(
        "Delivery to other@z.com, john@x.com failed."
    )}
    messages = [msg("me@me.com", "outreach"), message]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        found = gmail.find_bounce(ACCOUNT, "t1", "john@x.com")
        assert found["code"] == "4.2.2" and found["permanent"] is False
        assert gmail.find_bounce(ACCOUNT, "t1", "other@z.com")["permanent"] is True


def test_a_delay_notice_is_not_a_bounce():
    """Action: delayed means the MTA is still retrying. The mail commonly lands
    on a later attempt, so the row must be left alone -- marking it stops the
    reply checks on a prospect who is about to receive the email."""
    message = bounce_msg("4.4.7")
    message["payload"]["parts"][1]["body"] = {"data": _enc(
        "Final-Recipient: rfc822; john@x.com\nAction: delayed\nStatus: 4.4.7\n"
    )}
    message["payload"]["parts"][0]["body"] = {"data": _enc(
        "Your message has not been delivered yet. Delivery will be retried."
    )}
    with patched(gmail, "_get_service", lambda account: fake_gmail_service([
        msg("me@me.com", "outreach"), message
    ])):
        assert gmail.find_bounce(ACCOUNT, "t1", "john@x.com") is None


def test_find_bounce_falls_back_to_an_anchored_code_in_plain_text():
    """Not every MTA sends a delivery-status part. A code is trusted in the
    human-readable text only where a server would have put one: right after the
    SMTP reply it qualifies, or on a Status:/Diagnostic-Code: line."""
    plain = {
        "payload": {
            "headers": [
                {"name": "From", "value": "MAILER-DAEMON@mx.example.com"},
                {"name": "Content-Type", "value": "text/plain"},
            ],
            "mimeType": "text/plain",
            "body": {"data": _enc(
                "Delivery to john@x.com failed permanently.\n"
                "The server said: 550 5.1.1 unknown recipient\n"
            )},
        }
    }
    with patched(gmail, "_get_service", lambda account: fake_gmail_service([
        msg("me@me.com", "outreach"), plain
    ])):
        found = gmail.find_bounce(ACCOUNT, "t1", "john@x.com")
    assert found["code"] == "5.1.1" and found["permanent"] is True


def test_an_unanchored_code_in_plain_text_is_not_a_bounce():
    """A bare dotted triple is far more likely to be an IP or a version string
    than a delivery status. Skipping costs one stale row; guessing suppresses a
    real prospect permanently."""
    plain = {
        "payload": {
            "headers": [
                {"name": "From", "value": "MAILER-DAEMON@mx.example.com"},
                {"name": "Content-Type", "value": "text/plain"},
            ],
            "mimeType": "text/plain",
            "body": {"data": _enc(
                "Delivery to john@x.com was relayed via host 5.10.20.30 running Postfix 4.2.2.\n"
            )},
        }
    }
    with patched(gmail, "_get_service", lambda account: fake_gmail_service([
        msg("me@me.com", "outreach"), plain
    ])):
        assert gmail.find_bounce(ACCOUNT, "t1", "john@x.com") is None


def test_find_bounce_accepts_a_prefetched_thread_without_calling_gmail():
    def explode(account):
        raise AssertionError("find_bounce must not fetch when handed a thread")

    thread = {"messages": [msg("me@me.com", "outreach"), bounce_msg("5.1.1")]}
    with patched(gmail, "_get_service", explode):
        found = gmail.find_bounce(ACCOUNT, "t1", "john@x.com", thread=thread)
        reply, _, _ = gmail.get_latest_reply_with_history(
            ACCOUNT, "t1", "john@x.com", thread=thread
        )
    assert found["code"] == "5.1.1"
    assert reply is None, "the newest message is the daemon's, not the contact's"


def test_check_for_replies_hard_bounce_suppresses_and_marks_row():
    calls = {"rows": [(2, _row(status="Sent", thread="t1", name="John", email="john@x.com"))]}
    bounce = {"permanent": True, "code": "5.1.1", "detail": "no such user"}
    with contextlib.ExitStack() as stack:
        for target in _watch_env(calls, {}, bounce_map={"t1": bounce}):
            stack.enter_context(patched(*target))
        result = watch_replies.check_for_replies(ACCOUNT)
    assert result["bounces"] == [
        {"row": 2, "email": "john@x.com", "code": "5.1.1", "permanent": True}
    ]
    assert calls["update_row"] == [(2, {"status": "Bounced", "expect_email": "john@x.com"})]
    assert calls["suppressed"] == [("john@x.com", "hard_bounce")]
    assert result["reviews"] == [] and result["row_errors"] == []


def test_check_for_replies_soft_bounce_does_not_suppress():
    """A 4.x.x is a full mailbox or a greylist. Marking the row is right;
    permanently blocking a prospect we could still reach is not.

    The row gets "Delayed", not "Bounced". Writing "Bounced" for both kinds threw
    the distinction away one function call after making it: it dragged transient
    failures into the pause threshold and dropped the row out of reply-check
    scope, so a prospect whose mail landed on a retry wrote back to nobody."""
    calls = {"rows": [(2, _row(status="Sent", thread="t1", name="John", email="john@x.com"))]}
    bounce = {"permanent": False, "code": "4.2.2", "detail": "mailbox full"}
    with contextlib.ExitStack() as stack:
        for target in _watch_env(calls, {}, bounce_map={"t1": bounce}):
            stack.enter_context(patched(*target))
        result = watch_replies.check_for_replies(ACCOUNT)
    assert result["bounces"][0]["permanent"] is False
    assert calls["update_row"] == [(2, {"status": "Delayed", "expect_email": "john@x.com"})]
    assert "suppressed" not in calls, "a temporary failure must not suppress the address"


def test_check_for_replies_reads_each_thread_once():
    """find_bounce and get_latest_reply_with_history ask different questions of
    the same messages. Two format=full fetches per row is double Gmail quota for
    data already in memory."""
    calls = {"rows": [
        (2, _row(status="Sent", thread="t1", name="John", email="john@x.com")),
        (3, _row(status="Sent", thread="t2", name="Jane", email="jane@x.com")),
    ]}
    with contextlib.ExitStack() as stack:
        for target in _watch_env(calls, {}):
            stack.enter_context(patched(*target))
        watch_replies.check_for_replies(ACCOUNT)
    assert calls["thread_reads"] == ["t1", "t2"], calls["thread_reads"]


def test_a_delayed_row_stays_in_reply_check_scope():
    """The transient-failure row must keep being polled: that mail commonly
    lands on a retry, and the prospect who then answers has to reach someone."""
    rows = [
        (2, _row(status="Delayed", email="d@x.com")),
        (3, _row(status="Bounced", email="b@x.com")),
        (4, _row(status="Sent", email="s@x.com")),
        (5, _row(status="Replied", email="r@x.com")),
        (6, _row(status="", email="q@x.com")),
    ]
    with patched(sheets, "get_all_rows", lambda account: rows):
        in_scope = {row[sheets.COL_EMAIL] for _, row in sheets.get_reply_check_rows(ACCOUNT)}
    assert in_scope == {"d@x.com", "s@x.com", "r@x.com"}
    assert "b@x.com" not in in_scope, "a permanent failure is done; stop polling it"


def _days_ago(days):
    """A SentAt value in the format send_outreach writes, `days` in the past.
    None means no timestamp at all -- a hand-filled or pre-SentAt sheet."""
    if days is None:
        return ""
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _hard_bounce_stamps(count=0, older=0):
    """`count` hard-bounce suppression timestamps inside the rate window, plus
    `older` outside it."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return ([now - timedelta(days=bounces.WINDOW_DAYS + 10)] * older
            + [now - timedelta(days=1)] * count)


@contextlib.contextmanager
def _bounce_inputs(sent=0, bounces=0, older_bounces=0):
    """The only two inputs to the bounce verdict, and neither is the sheet.

    sends come from the send log (outreach_drafts, one row per email Gmail
    accepted); hard bounces from the suppression list, which also supplies the
    acknowledgement watermark. Both append-only. The sheet moved this ratio in
    both directions -- deleting Bounced rows zeroed the numerator, and pasting
    rows carrying a status and a date inflated the denominator."""
    stamps = _hard_bounce_stamps(bounces, older_bounces)
    with patched(suppressions_db, "list_source_timestamps", lambda aid, source: stamps), \
         patched(drafts_db, "count_sent_since", lambda aid, since: sent):
        yield


def _bounce_rows(sent, bounced, days_ago=None, status="Sent"):
    """sent rows total, of which `bounced` are hard bounces; the rest carry
    `status`."""
    rows = [(i, _row(status="Bounced", email=f"b{i}@x.com", sent_at=_days_ago(days_ago)))
            for i in range(bounced)]
    rows += [(i, _row(status=status, email=f"s{i}@x.com", sent_at=_days_ago(days_ago)))
             for i in range(bounced, sent)]
    return rows


def test_bounce_rate_pauses_sending_only_with_enough_volume():
    """One bounce out of three is 33%, and means nothing. Pausing a brand-new
    account on it would be a worse bug than the one the guard prevents."""
    with _bounce_inputs(sent=3, bounces=1):
        small = bounces.stats(ACCOUNT, _bounce_rows(3, 1, days_ago=1))
        assert small["rate"] > bounces.PAUSE_THRESHOLD
        assert small["paused"] is False
        assert bounces.pause_reason(ACCOUNT, _bounce_rows(3, 1, days_ago=1)) == ""

    unsafe = _bounce_rows(40, 4, days_ago=1)
    with _bounce_inputs(sent=40, bounces=4):
        assert bounces.stats(ACCOUNT, unsafe)["paused"] is True
        assert "paused" in bounces.pause_reason(ACCOUNT, unsafe)

    healthy = _bounce_rows(40, 1, days_ago=1)
    with _bounce_inputs(sent=40, bounces=1):
        assert bounces.stats(ACCOUNT, healthy)["paused"] is False
        assert bounces.pause_reason(ACCOUNT, healthy) == ""


def test_stats_always_reads_the_suppression_list():
    """This replaces a test that asserted the opposite, and the reason it flipped
    is the point. Skipping the lookup when the sheet-derived rate looked safe was
    only sound while the numerator came off the sheet -- which is exactly what let
    deleting the Bounced rows read as 0% and never consult anything. The numerator
    now comes from here, so it is not optional."""
    reads = []
    with patched(suppressions_db, "list_source_timestamps",
                 lambda aid, source: reads.append("suppressions") or []), \
         patched(drafts_db, "count_sent_since",
                 lambda aid, since: reads.append("send log") or 0):
        healthy = bounces.stats(ACCOUNT, _bounce_rows(40, 0, days_ago=1))
    assert sorted(reads) == ["send log", "suppressions"], (
        "both terms of the ratio are read every time; neither is optional and "
        "neither comes from the sheet rows passed in"
    )
    assert healthy["paused"] is False and healthy["unsafe"] is False
    assert healthy["hard_bounces_recorded"] == 0
    assert healthy["sent"] == 0, "40 sheet rows saying Sent contribute nothing"
    assert healthy["sheet_sent"] == 40, "they are still reported, for display"


def test_deleting_every_bounced_row_does_not_zero_the_rate():
    """The hole the live walkthrough found. Fixing the watermark was not enough:
    with the numerator read off the sheet, deleting the Bounced rows dropped the
    rate to 0%, so `unsafe` went false and the watermark was never even reached.
    Both sides of the verdict have to come from data the operator cannot edit."""
    # 20 sends still on the sheet, every Bounced row deleted, 3 hard bounces on
    # the suppression list.
    rows = _bounce_rows(20, 0, days_ago=1)
    with _bounce_inputs(sent=20, bounces=3):
        current = bounces.stats(ACCOUNT, rows)
    assert current["sheet_bounced"] == 0, "the sheet genuinely shows none"
    assert current["bounced"] == 3, "the suppression list is what counts"
    assert current["rate"] == 3 / 20
    assert current["paused"] is True

    reason = bounces.pause_reason(ACCOUNT, rows, current=current)
    assert "your sheet shows 0" in reason.lower(), (
        "explain the discrepancy, or the guard looks like it fired at random and "
        "the obvious next guess is to delete more rows"
    )
    assert "will not lift this" in reason


def test_a_trimmed_sheet_cannot_report_a_rate_above_one():
    """Trim the sheet far enough and there are more recorded bounces than
    remaining sends. Capping keeps the number sane; it must never lower it."""
    with _bounce_inputs(sent=20, bounces=50):
        current = bounces.stats(ACCOUNT, _bounce_rows(20, 0, days_ago=1))
    assert current["bounced"] == 20 and current["sent"] == 20
    assert current["rate"] == 1.0 and current["paused"] is True
    assert current["hard_bounces_recorded"] == 50, "the real total is still reported"


def test_a_delayed_row_counts_as_sent_but_never_as_bounced():
    """The whole point of the separate status. 20 greylisted mailboxes are 20
    perfectly reachable prospects; letting them march the account toward the
    pause threshold locks the operator out of their own product on behalf of
    nobody."""
    rows = _bounce_rows(40, 0, days_ago=1)
    rows += [(100 + i, _row(status="Delayed", email=f"d{i}@x.com", sent_at=_days_ago(1)))
             for i in range(20)]
    with _bounce_inputs(sent=60, bounces=0):
        current = bounces.stats(ACCOUNT, rows)
    assert current["sent"] == 60, current
    assert current["bounced"] == 0
    assert current["rate"] == 0.0
    assert current["paused"] is False


def test_bounce_rate_uses_a_recent_window_not_a_lifetime_average():
    """A lifetime denominator hides exactly the failure this module exists to
    catch. 5,000 clean historical sends plus 200 consecutive bounces today is
    3.8% -- under the threshold, while the domain burns."""
    rows = _bounce_rows(5000, 0, days_ago=120) + _bounce_rows(200, 200, days_ago=0)
    with _bounce_inputs(sent=200, bounces=200):
        current = bounces.stats(ACCOUNT, rows)
    assert current["sheet_sent"] == 5200, "the whole history is still on the sheet"
    assert 200 / current["sheet_sent"] < bounces.PAUSE_THRESHOLD, (
        "a lifetime rate would be under the threshold, or this test proves nothing"
    )
    assert current["sent"] == 200 and current["bounced"] == 200
    assert current["rate"] == 1.0
    assert current["paused"] is True
    assert current["window"] == f"last {bounces.WINDOW_DAYS} days"


def test_bounces_outside_the_window_stop_holding_an_account_back():
    """The other direction: a bad import months ago must not pause an account
    that has been sending cleanly since. The window applies to the suppression
    timestamps too, not just to the sends."""
    rows = _bounce_rows(100, 50, days_ago=90) + _bounce_rows(40, 0, days_ago=2)
    with _bounce_inputs(sent=40, bounces=0, older_bounces=50):
        current = bounces.stats(ACCOUNT, rows)
    assert current["sent"] == 40 and current["bounced"] == 0
    assert current["paused"] is False
    assert current["hard_bounces_recorded"] == 50, (
        "the history is still recorded and still reported, just not enforced"
    )


def test_the_guard_no_longer_depends_on_sheet_dates_at_all():
    """Replaces a test for an "all time" fallback that has been deleted.

    That branch existed because a hand-filled sheet has no parseable SentAt, and
    it was where dilution was easiest: a blank status is what makes a row
    eligible, so marking rows "Sent" is the natural way to exclude leads from a
    campaign, and on an undated sheet 200 excluded rows collapsed the rate to
    nothing. The send log always carries a timestamp, so there is no undated case
    left to fall back for."""
    undated = _bounce_rows(40, 4)                      # no SentAt anywhere
    excluded = undated + [(500 + i, _row(status="Sent", email=f"skip{i}@x.com"))
                          for i in range(200)]         # manually excluded leads
    with _bounce_inputs(sent=40, bounces=4):
        plain = bounces.stats(ACCOUNT, undated)
        diluted = bounces.stats(ACCOUNT, excluded)
    assert plain["window"] == f"last {bounces.WINDOW_DAYS} days", "no 'all time' branch"
    assert plain["sent"] == 40 and plain["bounced"] == 4 and plain["paused"] is True
    assert diluted["rate"] == plain["rate"], (
        "200 rows marked Sent in the sheet must not move the rate by even a fraction"
    )
    assert diluted["paused"] is True


def test_acknowledging_lifts_the_pause_without_deleting_anything():
    """The old remediation -- "remove the bad addresses from your sheet" -- was
    the only lever that lowered the rate, on a product that sells a full audit
    trail. A compliance guard whose fix is editing the record is not a guard."""
    rows = _bounce_rows(40, 4, days_ago=1)
    with _bounce_inputs(sent=40, bounces=4):
        assert bounces.stats(ACCOUNT, rows)["paused"] is True

        saved = {}
        with patched(bounces.accounts_db, "set_bounce_ack",
                     lambda account_id, count: saved.update(id=account_id, count=count)), \
             patched(sheets, "get_all_rows", lambda account: rows):
            result = bounces.acknowledge(ACCOUNT)
        assert saved == {"id": ACCOUNT["id"], "count": 4}
        assert result["paused"] is False

        acknowledged = dict(ACCOUNT, bounce_ack_count=4)
        assert bounces.stats(acknowledged, rows)["paused"] is False
        assert bounces.pause_reason(acknowledged, rows) == ""
        # Still reported as unsafe -- acknowledged is not the same as healthy.
        assert bounces.stats(acknowledged, rows)["unsafe"] is True


def test_a_deteriorating_list_pauses_again_after_an_acknowledgement():
    """Acknowledging accepts the bounces seen so far, not every future one."""
    acknowledged = dict(ACCOUNT, bounce_ack_count=4)
    worse = _bounce_rows(60, 9, days_ago=1)
    with _bounce_inputs(sent=60, bounces=9):
        assert bounces.stats(acknowledged, worse)["paused"] is True


def test_deleting_bounced_rows_after_acknowledging_cannot_disable_the_guard():
    """The watermark has to be compared against a number the operator does not
    control. Measured against "Bounced" sheet rows it was: acknowledge at 200,
    delete half of them, and the sheet count falls below the watermark while the
    rate stays catastrophic -- so the guard could not re-arm until 101 further
    bounces, quietly and permanently, through exactly the deletion this design
    exists to make unnecessary. Suppression rows survive the delete."""
    acknowledged = dict(ACCOUNT, bounce_ack_count=200)
    # 200 bounced + 50 clean, then the operator deletes 100 of the bounced rows.
    # The sheet now shows only 100; the suppression list still holds 200.
    rows = _bounce_rows(150, 100, days_ago=1)
    with _bounce_inputs(sent=150, bounces=200):
        from_the_sheet = bounces.stats(acknowledged, rows)
        assert from_the_sheet["unsafe"] is True, "the rate is still far over the line"
        assert from_the_sheet["sheet_bounced"] == 100 < 200, "sheet count is under the watermark"
        assert from_the_sheet["bounced"] == 150, "numerator comes from suppressions, capped at sends"
        # Nothing new has bounced yet, so acknowledged still means acknowledged.
        assert from_the_sheet["paused"] is False

    # One new hard bounce lands. The suppression list is append-only, so it
    # counts 201 regardless of what the sheet says, and the guard fires.
    with _bounce_inputs(sent=150, bounces=201):
        resumed = bounces.stats(acknowledged, rows)
        assert resumed["paused"] is True
        assert resumed["hard_bounces_recorded"] == 201


def test_a_watermark_above_the_recorded_count_cannot_hold_the_guard_down():
    """A watermark higher than the recorded count is impossible in normal
    operation -- suppressions pruned, or an acknowledgement saved back when sheet
    rows were the counter. It no longer describes the account, so it must fail
    toward the guard firing rather than silently disabling it until the count
    catches up. One acknowledgement repairs it."""
    stale = dict(ACCOUNT, bounce_ack_count=500)
    rows = _bounce_rows(40, 4, days_ago=1)
    with _bounce_inputs(sent=40, bounces=200):
        assert bounces.stats(stale, rows)["paused"] is True
    # Repaired: the watermark now matches what is recorded, and behaves normally.
    repaired = dict(ACCOUNT, bounce_ack_count=200)
    with _bounce_inputs(sent=40, bounces=200):
        assert bounces.stats(repaired, rows)["paused"] is False
    with _bounce_inputs(sent=40, bounces=201):
        assert bounces.stats(repaired, rows)["paused"] is True


def test_a_hard_bounce_is_suppressed_before_the_sheet_is_written():
    """The suppression row both protects the address and is the count the pause
    threshold is measured against, so a failing sheet write must not lose it."""
    order = []
    with patched(suppressions_db, "add",
                 lambda aid, e, source="unsubscribe_link": order.append(("suppress", source))), \
         patched(sheets, "update_row", lambda *a, **k: order.append(("sheet", k["status"]))):
        bounces.record(ACCOUNT, 2, "j@x.com", {"permanent": True, "code": "5.1.1"})
    assert order == [("suppress", "hard_bounce"), ("sheet", "Bounced")]

    def sheet_down(*a, **k):
        raise RuntimeError("sheet unreachable")

    suppressed = []
    with patched(suppressions_db, "add",
                 lambda aid, e, source="unsubscribe_link": suppressed.append(e)), \
         patched(sheets, "update_row", sheet_down):
        try:
            bounces.record(ACCOUNT, 2, "j@x.com", {"permanent": True, "code": "5.1.1"})
        except RuntimeError:
            pass
    assert suppressed == ["j@x.com"], "the address must stay suppressed even so"


def test_the_pause_message_does_not_tell_anyone_to_delete_their_data():
    with _bounce_inputs(sent=40, bounces=4):
        reason = bounces.pause_reason(ACCOUNT, _bounce_rows(40, 4, days_ago=1))
    assert reason, "this fixture must be paused for the assertions below to mean anything"
    lowered = reason.lower()
    assert "remove the bad addresses" not in lowered
    assert "nothing has been deleted" in lowered
    assert "acknowledge" in lowered
    assert "already suppressed" in lowered, "say why continuing is safe"


def test_an_unmigrated_database_keeps_the_old_pause_behaviour():
    """bounce_ack_count is a documented migration. Until it runs, account rows
    have no such key -- that must read as "nothing acknowledged", not crash."""
    rows = _bounce_rows(40, 4, days_ago=1)
    assert "bounce_ack_count" not in ACCOUNT, "fixture must model a pre-migration row"
    with _bounce_inputs(sent=40, bounces=4):
        assert bounces.stats(ACCOUNT, rows)["paused"] is True
        # A column that exists but is NULL, and a garbage value, both read as
        # zero rather than throwing on the send path.
        assert bounces.stats(dict(ACCOUNT, bounce_ack_count=None), rows)["paused"] is True
        assert bounces.stats(dict(ACCOUNT, bounce_ack_count="oops"), rows)["paused"] is True


class _FakeTable:
    """Minimal PostgREST chain. missing is the set of column names that raise on
    select, the way an un-migrated database answers."""

    def __init__(self, missing=(), unreachable=False):
        self.missing = set(missing)
        self.unreachable = unreachable
        self._column = None

    def table(self, name):
        return self

    def select(self, column, **kwargs):
        self._column = column
        return self

    def limit(self, n):
        return self

    def execute(self):
        if self.unreachable:
            raise RuntimeError("connection refused")
        if self._column in self.missing:
            raise RuntimeError('column accounts.%s does not exist' % self._column)
        return type("R", (), {"data": [], "count": 0})()


def test_schema_check_names_the_missing_column_and_its_consequence():
    """The window this closes: bounce_ack_count is only needed by an account
    already paused for a bad bounce rate, so without a boot-time warning the
    first person to discover it is a customer having a bad day whose only exit is
    deleting sheet rows -- the remediation the pause message stopped giving."""
    with patched(accounts_db, "_get_client",
                 lambda: _FakeTable(missing={"bounce_ack_count"})):
        warnings = accounts_db.check_schema()
    assert len(warnings) == 1, warnings
    assert "bounce_ack_count" in warnings[0]
    assert "alter table public.accounts" in warnings[0], "give the statement, not just the name"
    assert "deleting sheet rows" in warnings[0], "say what breaks, not just what is absent"


def test_schema_check_is_silent_on_a_migrated_database():
    with patched(accounts_db, "_get_client", lambda: _FakeTable()):
        assert accounts_db.check_schema() == []


def test_schema_check_does_not_blame_a_migration_for_an_unreachable_database():
    """A network failure would otherwise report every column as missing and send
    someone off to run SQL they do not need."""
    with patched(accounts_db, "_get_client", lambda: _FakeTable(unreachable=True)):
        warnings = accounts_db.check_schema()
    assert len(warnings) == 1
    assert "Could not verify" in warnings[0]
    assert "alter table" not in warnings[0]


def test_a_degraded_control_warns_but_never_blocks_boot():
    """Each degrades to something that still runs, so refusing to start would be
    the larger outage. And one probe throwing must not hide the others."""
    import server
    out = io.StringIO()

    def boom():
        raise RuntimeError("supabase down")

    with patched(server.accounts_db, "check_schema", boom), \
         patched(server.ratelimit, "enforcement_warnings",
                 lambda: ["rate limiting is in-memory and per-process"]), \
         contextlib.redirect_stdout(out):
        server.report_degraded_controls()   # must not raise
    printed = out.getvalue()
    assert "SCHEMA: check skipped" in printed
    assert "RATELIMIT" in printed, "a failing probe must not suppress the next one"

    out = io.StringIO()
    with patched(server.accounts_db, "check_schema",
                 lambda: ["accounts.bounce_ack_count is missing"]), \
         patched(server.ratelimit, "enforcement_warnings", lambda: []), \
         contextlib.redirect_stdout(out):
        server.report_degraded_controls()
    assert "bounce_ack_count" in out.getvalue()


def test_the_ratelimiter_reports_when_workers_make_it_unenforceable():
    """Behind N workers each process keeps its own counters, so the effective
    limit is somewhere between limit and N x limit depending on which worker
    answers -- and it looks exactly like a working limiter from outside."""
    for env, argv, expect_warning in (
        ({}, ["uvicorn", "server:app"], False),
        ({"WEB_CONCURRENCY": "1"}, ["uvicorn", "server:app"], False),
        ({"WEB_CONCURRENCY": "4"}, ["uvicorn", "server:app"], True),
        ({}, ["uvicorn", "server:app", "--workers", "3"], True),
        ({}, ["uvicorn", "server:app", "--workers=2"], True),
        ({}, ["uvicorn", "server:app", "--workers", "notanumber"], False),
    ):
        with patched(ratelimit.os, "environ", env), patched(ratelimit.sys, "argv", argv):
            warnings = ratelimit.enforcement_warnings()
        assert bool(warnings) is expect_warning, (env, argv, warnings)
        if expect_warning:
            assert "login" in warnings[0], "name the limit that matters most"


def test_the_ratelimiter_does_not_cry_wolf_about_restart_amnesia():
    """Windows emptying on restart is inherent to the design, not a
    misconfiguration. A warning on every boot is one an operator learns to scroll
    past, which would cost more than it buys -- it is documented in the module
    instead."""
    with patched(ratelimit.os, "environ", {}), patched(ratelimit.sys, "argv", ["uvicorn"]):
        assert ratelimit.enforcement_warnings() == []
    assert "Restart amnesia" in ratelimit.__doc__


def test_ratelimit_state_is_process_local_and_resets():
    """Naming the amnesia so it is a known property rather than a surprise: a
    fresh process starts every window empty. This is what makes the login limit a
    speed bump rather than the account-security control."""
    key = f"restart:{time.time()}"
    assert ratelimit.check(key, limit=1, window_seconds=3600) is True
    assert ratelimit.check(key, limit=1, window_seconds=3600) is False
    with patched(ratelimit, "_hits", {}):          # what a restart looks like
        assert ratelimit.check(key, limit=1, window_seconds=3600) is True


def test_the_sweep_never_drops_a_key_still_inside_its_window():
    """The eviction horizon is the largest window ever asked about, and _max_window
    only ever grows -- that monotonicity is what makes dropping safe. If it could
    shrink, a long-window key would be evicted early and its limit would silently
    reset. Nothing about that is visible from outside."""
    now = time.time()
    with patched(ratelimit, "_hits", {}), patched(ratelimit, "_max_window", 0.0), \
         patched(ratelimit, "_last_sweep", now):
        # A long-window key recorded first, then a short-window call forces a sweep.
        assert ratelimit.check("signup:1.2.3.4", limit=5, window_seconds=3600) is True
        ratelimit._last_sweep = 0.0                # make the next call sweep
        assert ratelimit.check("login:9.9.9.9", limit=10, window_seconds=300) is True
        assert "signup:1.2.3.4" in ratelimit._hits, (
            "a 3600s key must survive a sweep triggered by a 300s call"
        )
        # Genuinely stale keys still go, or the dict leaks for the process lifetime.
        ratelimit._hits["ancient"] = [now - 99999]
        ratelimit._last_sweep = 0.0
        ratelimit.check("login:9.9.9.9", limit=10, window_seconds=300)
        assert "ancient" not in ratelimit._hits


def test_acknowledge_endpoint_reports_the_migration_when_the_column_is_missing():
    import server

    def missing_column(account_id, count):
        raise RuntimeError("Could not save the bounce acknowledgement. ... "
                           "alter table public.accounts add column bounce_ack_count ...")

    with _bounce_inputs(sent=40, bounces=4), \
         patched(server, "_account", lambda r: dict(ACCOUNT)), \
         patched(bounces.accounts_db, "set_bounce_ack", missing_column), \
         patched(sheets, "get_all_rows", lambda account: _bounce_rows(40, 4, days_ago=1)):
        try:
            server.acknowledge_bounces(_srv_req())
            assert False, "should surface the failure"
        except RuntimeError as e:
            # server.py turns RuntimeError into a 409 with this text (see the
            # runtime_error_handler), so the migration statement reaches the user.
            assert "bounce_ack_count" in str(e)


def test_prepare_drafts_refuses_when_bounce_rate_is_unsafe():
    """The guard has to fire before the batch spends any LLM calls."""
    drafted = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(_bounce_inputs(sent=40, bounces=4))
        stack.enter_context(patched(sheets, "require_full_header", lambda account: None))
        stack.enter_context(patched(sheets, "get_all_rows", lambda account: _bounce_rows(40, 4)))
        stack.enter_context(patched(agent, "generate_outreach_email",
                                    lambda *a, **k: drafted.append(a) or ("s", "b")))
        try:
            send_outreach.prepare_drafts(ACCOUNT)
            raise AssertionError("expected SendingPaused")
        except bounces.SendingPaused as e:
            assert "paused" in str(e)
    assert drafted == [], "must refuse before spending an LLM call"


def test_send_all_prepared_refuses_when_bounce_rate_is_unsafe():
    """Drafts queued before the rate crossed the line must not go out either."""
    sent = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(_bounce_inputs(sent=40, bounces=4))
        stack.enter_context(patched(sheets, "get_all_rows", lambda account: _bounce_rows(40, 4)))
        stack.enter_context(patched(gmail, "send_email", lambda *a, **k: sent.append(a) or "t"))
        try:
            send_outreach.send_all_prepared(ACCOUNT)
            raise AssertionError("expected SendingPaused")
        except bounces.SendingPaused:
            pass
    assert sent == [], "no email may leave while sending is paused"


def test_bounce_stats_counts_bounced_rows_as_sent():
    """A bounced row was still an email that left the building -- excluding it
    from the denominator would overstate the rate. The denominator is the one
    side that does come off the sheet; the numerator is the suppression list."""
    with _bounce_inputs(sent=10, bounces=2):
        stats = bounces.stats(ACCOUNT, _bounce_rows(10, 2, days_ago=1))
    assert stats["sent"] == 10 and stats["bounced"] == 2
    assert stats["rate"] == 0.2


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
        readiness = sheets.campaign_readiness(ACCOUNT, sent_today=0)
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
        readiness = sheets.campaign_readiness(ACCOUNT, sent_today=0)
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


# ------------------------------------------------- LLM provider chain + routing

def _prow(name, free_tier, model=None, api_key="test-key", url="https://x.test/v1",
          enabled_when="api_key"):
    """One row in the shape config._build_providers() emits."""
    return {
        "name": name, "label": name.title(), "url": url, "api_key": api_key,
        "model": model or f"{name}-model", "free_tier": free_tier,
        "enabled_when": enabled_when,
    }


class _LLMResp:
    def __init__(self, status=200, content=GOOD_EMAIL, headers=None, call_usage=None,
                 finish_reason="stop"):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.headers = headers or {}
        self.text = "provider error body"
        self._content = content
        self._usage = call_usage or {}
        self._finish = finish_reason

    def json(self):
        return {
            "choices": [{"message": {"content": self._content}, "finish_reason": self._finish}],
            "usage": self._usage,
        }


def test_first_touch_drafts_may_use_every_configured_provider_in_order():
    rows = [_prow("groq", True), _prow("gemini", True), _prow("openrouter", True)]
    with patched(providers, "LLM_PROVIDERS", rows):
        chain = providers.chain_for(agent.usage.DRAFT_EMAIL)
    assert [p.name for p in chain] == ["groq", "gemini", "openrouter"]


def test_reply_drafts_never_touch_a_free_tier_endpoint_when_a_paid_one_exists():
    """A reply prompt carries the prospect's own words, and a free tier generally
    reserves the right to train on what it is sent. The paid endpoint is used
    even though it is later in the preference order, and the free ones are not
    kept as a fallback -- a failed draft is recoverable, a leak is not."""
    rows = [_prow("groq", True), _prow("gemini", False), _prow("openrouter", True)]
    with patched(providers, "LLM_PROVIDERS", rows):
        chain = providers.chain_for(agent.usage.DRAFT_REPLY)
    assert [p.name for p in chain] == ["gemini"]


def test_reply_drafts_fall_back_to_free_tier_only_with_a_loud_warning():
    """The one configuration a pre-revenue install has is free-tier-only, so this
    must keep working -- but never silently."""
    rows = [_prow("groq", True)]
    out = io.StringIO()
    with patched(providers, "LLM_PROVIDERS", rows), \
         patched(providers, "_warned_free_tier_replies", False), \
         contextlib.redirect_stdout(out):
        chain = providers.chain_for(agent.usage.DRAFT_REPLY)
    assert [p.name for p in chain] == ["groq"]
    printed = out.getvalue()
    assert "WARNING" in printed and "training" in printed
    assert "privacy.html" in printed, "the warning must name the disclosure that has to match"


def test_reply_drafts_can_be_hard_blocked_off_free_tier_endpoints():
    rows = [_prow("groq", True)]
    with patched(providers, "LLM_PROVIDERS", rows), \
         patched(providers, "REPLY_DRAFTS_REQUIRE_PRIVATE_ENDPOINT", True):
        try:
            providers.chain_for(agent.usage.DRAFT_REPLY)
            assert False, "should refuse rather than use a free-tier endpoint"
        except RuntimeError as e:
            assert "free tier" in str(e)
    # First-touch drafting is unaffected: its prompt has nothing private in it.
    with patched(providers, "LLM_PROVIDERS", rows), \
         patched(providers, "REPLY_DRAFTS_REQUIRE_PRIVATE_ENDPOINT", True):
        assert len(providers.chain_for(agent.usage.DRAFT_EMAIL)) == 1


def test_no_configured_provider_raises_with_the_fix_in_the_message():
    with patched(providers, "LLM_PROVIDERS", []):
        try:
            providers.chain_for(agent.usage.DRAFT_EMAIL)
            assert False, "should raise"
        except RuntimeError as e:
            assert "GROQ_API_KEY" in str(e)


def test_provider_is_enabled_by_the_field_that_activates_it():
    """The hosted providers need a key. The local proxy needs a base URL and may
    have no key at all, since it need not require auth."""
    rows = [
        _prow("groq", True, api_key=""),
        _prow("cliproxy", False, api_key="", url="http://localhost:8317/v1",
              enabled_when="url"),
        _prow("gemini", True, api_key="", url="", enabled_when="url"),
    ]
    with patched(providers, "LLM_PROVIDERS", rows):
        assert [p.name for p in providers.available()] == ["cliproxy"]


def test_openrouter_free_suffix_decides_the_tier_with_no_env_var():
    assert config._provider_tier("openrouter", "auto", "openai/gpt-oss-20b:free") == "free"
    assert config._provider_tier("openrouter", "auto", "anthropic/claude-sonnet-5") == "paid"


def test_an_unreadable_tier_resolves_to_free_not_paid():
    """Fail toward the endpoint being untrusted. Calling a paid endpoint free
    only costs a routing option; calling a free one paid sends a prospect's words
    somewhere they may be trained on."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert config._provider_tier("groq", "nonsense", "m") == "free"
    assert "treating this endpoint as free-tier" in out.getvalue()


def test_free_tier_models_are_priced_at_zero_not_left_unpriced():
    """A free-tier call genuinely costs $0, so it is a known price. Derived from
    the tier, so the same model on a paid tier stops claiming to be free."""
    free_models = [p["model"] for p in config.LLM_PROVIDERS if p["free_tier"]]
    for model in free_models:
        assert config.MODEL_PRICES.get(model) == (0.0, 0.0), model


def test_describe_reports_which_endpoints_may_draft_replies():
    rows = [_prow("groq", True), _prow("gemini", False)]
    with patched(providers, "LLM_PROVIDERS", rows):
        described = {d["name"]: d for d in providers.describe()}
    assert described["groq"]["drafts_replies"] is False
    assert described["gemini"]["drafts_replies"] is True
    assert described["groq"]["tier"] == "free"


# ------------------------------------------------------------ _chat fallthrough

def test_chat_moves_to_the_next_provider_when_one_is_unavailable():
    rows = [_prow("groq", True), _prow("gemini", True)]
    tried = []

    def fake_post(provider, system, prompt, purpose, may_wait):
        tried.append(provider.name)
        if provider.name == "groq":
            raise agent.ProviderUnavailable("rate-limited (Retry-After: 3600s)")
        return "drafted"

    out = io.StringIO()
    with patched(providers, "LLM_PROVIDERS", rows), patched(agent, "_post", fake_post), \
         contextlib.redirect_stdout(out):
        assert agent._chat("sys", "user", agent.usage.DRAFT_EMAIL) == "drafted"
    assert tried == ["groq", "gemini"]


def test_chat_only_waits_out_a_rate_limit_on_the_last_provider():
    """Sleeping through a Retry-After while another provider sits idle wastes the
    exact resource the fallback chain exists to protect."""
    rows = [_prow("groq", True), _prow("gemini", True)]
    waits = []

    def fake_post(provider, system, prompt, purpose, may_wait):
        waits.append((provider.name, may_wait))
        raise agent.ProviderUnavailable("nope")

    out = io.StringIO()
    with patched(providers, "LLM_PROVIDERS", rows), patched(agent, "_post", fake_post), \
         contextlib.redirect_stdout(out):
        try:
            agent._chat("sys", "user", agent.usage.DRAFT_EMAIL)
            assert False, "should raise once every provider has failed"
        except RuntimeError as e:
            assert "Groq" in str(e) and "Gemini" in str(e), "name every endpoint tried"
    assert waits == [("groq", False), ("gemini", True)]


def test_post_does_not_park_a_request_on_a_blown_daily_quota():
    """Free tiers answer an exhausted daily allowance with a Retry-After in the
    thousands of seconds. Sleeping on that holds a web request open for an hour."""
    provider = providers.Provider("groq", "Groq", "https://x.test/v1", "k", "m", True)
    slept = []
    resp = _LLMResp(status=429, headers={"Retry-After": "3600"})
    with patched(agent.requests, "post", lambda *a, **k: resp), \
         patched(agent.time, "sleep", lambda s: slept.append(s)):
        try:
            agent._post(provider, "sys", "user", agent.usage.DRAFT_EMAIL, may_wait=True)
            assert False, "should report the provider unavailable"
        except agent.ProviderUnavailable as e:
            assert "3600" in str(e)
    assert slept == [], "must not sleep past MAX_RETRY_WAIT"


def test_post_waits_out_a_short_rate_limit_then_succeeds():
    provider = providers.Provider("groq", "Groq", "https://x.test/v1", "k", "m", True)
    responses = iter([_LLMResp(status=429, headers={"Retry-After": "2"}), _LLMResp()])
    slept = []
    out = io.StringIO()
    with patched(agent.requests, "post", lambda *a, **k: next(responses)), \
         patched(agent.time, "sleep", lambda s: slept.append(s)), \
         contextlib.redirect_stdout(out):
        assert agent._post(provider, "sys", "user", agent.usage.DRAFT_EMAIL,
                           may_wait=True).startswith("Subject:")
    assert slept == [2]


def test_post_treats_a_bad_key_or_retired_model_as_unavailable():
    """A 401 or a 404 on the model id is someone else's endpoint being unusable,
    not a reason to fail the draft while another provider is configured."""
    provider = providers.Provider("groq", "Groq", "https://x.test/v1", "k", "m", True)
    for status in (401, 404, 500):
        with patched(agent.requests, "post", lambda *a, **k: _LLMResp(status=status)):
            try:
                agent._post(provider, "sys", "user", agent.usage.DRAFT_EMAIL, may_wait=True)
                assert False, f"{status} should be ProviderUnavailable"
            except agent.ProviderUnavailable as e:
                assert str(status) in str(e)


def test_post_treats_a_connection_failure_as_unavailable():
    provider = providers.Provider("groq", "Groq", "https://x.test/v1", "k", "m", True)

    def boom(*a, **k):
        raise agent.requests.RequestException("connection reset")

    with patched(agent.requests, "post", boom):
        try:
            agent._post(provider, "sys", "user", agent.usage.DRAFT_EMAIL, may_wait=True)
            assert False, "should raise ProviderUnavailable"
        except agent.ProviderUnavailable as e:
            assert "connection reset" in str(e)


def test_usage_is_metered_against_the_model_that_actually_served_the_call():
    """Metering is what the pricing decision is built on, so a fallthrough to a
    different provider must not be recorded under the primary's model."""
    recorded = []
    provider = providers.Provider("gemini", "Gemini", "https://x.test/v1", "k",
                                  "gemini-2.5-flash", True)
    resp = _LLMResp(call_usage={"prompt_tokens": 400, "completion_tokens": 120,
                                "total_tokens": 520})
    with patched(agent.requests, "post", lambda *a, **k: resp), \
         patched(agent.usage, "record_llm", lambda *a, **k: recorded.append((a, k))):
        agent._post(provider, "sys", "user", agent.usage.DRAFT_EMAIL, may_wait=True)
    (purpose, model, prompt_tokens, completion_tokens), kwargs = recorded[0]
    assert model == "gemini-2.5-flash"
    assert (prompt_tokens, completion_tokens) == (400, 120)
    assert kwargs["total_tokens"] == 520


def test_post_survives_a_malformed_provider_response():
    provider = providers.Provider("groq", "Groq", "https://x.test/v1", "k", "m", True)

    class _Empty(_LLMResp):
        def json(self):
            return {"usage": {}}

    with patched(agent.requests, "post", lambda *a, **k: _Empty()):
        try:
            agent._post(provider, "sys", "user", agent.usage.DRAFT_EMAIL, may_wait=True)
            assert False, "should raise ProviderUnavailable, not KeyError"
        except agent.ProviderUnavailable as e:
            assert "response shape" in str(e)


def test_provider_endpoint_appends_the_completions_path_once():
    provider = providers.Provider("openrouter", "OpenRouter", "https://openrouter.ai/api/v1",
                                  "k", "m", True)
    assert provider.endpoint == "https://openrouter.ai/api/v1/chat/completions"


def test_provider_display_names_the_tier():
    """Logs have to answer "was that endpoint allowed to see this" as well as
    "which model wrote it"."""
    free = providers.Provider("groq", "Groq", "u", "k", "llama-3.3-70b-versatile", True)
    assert "free tier" in free.display and "llama-3.3-70b-versatile" in free.display
    paid = providers.Provider("gemini", "Gemini", "u", "k", "gemini-2.5-flash", False)
    assert "paid tier" in paid.display


def test_printing_a_garbled_generation_cannot_kill_a_batch():
    """A weak model injects a non-Latin token. The validator quotes the offending
    characters back in its complaint, and send_outreach prints that complaint from
    inside its per-contact `except` handler. On a cp1252 console that print raised
    UnicodeEncodeError out of the handler, so one bad row killed the whole batch
    instead of failing itself."""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    problems = agent._validate_outreach("A real subject", "sentence about work. " * 8 + "дгӘ", "", "")
    complaint = next(p for p in problems if "garbled" in p)

    plain = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    try:
        print(f"Failed to draft for p@x.com: {complaint}", file=plain)
        plain.flush()
        assert False, "cp1252 should not be able to encode this -- test is not proving anything"
    except UnicodeEncodeError:
        pass

    config._make_output_utf8_safe(stream)
    print(f"Failed to draft for p@x.com: {complaint}", file=stream)
    stream.flush()
    assert b"Failed to draft for p@x.com" in raw.getvalue()


def test_legal_pages_name_every_llm_provider_that_can_receive_lead_data():
    """privacy.html and dpa.html both promise a complete sub-processor list, and
    the DPA says its list is derived from the code. Adding a provider row without
    updating both pages turns that into a false statement to a customer's legal
    reviewer, so it fails here instead."""
    root = Path(__file__).resolve().parent.parent
    pages = {
        name: (root / "static" / name).read_text(encoding="utf-8")
        for name in ("privacy.html", "dpa.html")
    }
    disclosed = [r[6] for r in config._PROVIDER_ROWS if r[6]]
    assert disclosed, "the provider table should have at least one customer-facing endpoint"
    for name in disclosed:
        for page, text in pages.items():
            assert name in text, f"{page} does not disclose {name} as a sub-processor"


def test_legal_pages_disclose_free_tier_training_exposure():
    """The free tiers this runs on may train on what they are sent. Claiming "we
    never share lead data" over that, with no qualification, is the finding this
    disclosure exists to close."""
    root = Path(__file__).resolve().parent.parent
    for name in ("privacy.html", "dpa.html"):
        text = (root / "static" / name).read_text(encoding="utf-8").lower()
        assert "free tier" in text, name
        assert "train" in text, f"{name} must say what a free tier may do with the data"


def test_llm_providers_endpoint_exposes_names_and_tiers_but_no_keys():
    import server
    rows = [_prow("groq", True, api_key="sk-secret-groq"),
            _prow("gemini", False, api_key="sk-secret-gemini")]
    with patched(providers, "LLM_PROVIDERS", rows), \
         patched(server, "_account", lambda r: dict(ACCOUNT)):
        payload = server.llm_providers(_srv_req())
    body = repr(payload)
    assert "sk-secret" not in body, "provider keys must never leave the process"
    assert {p["name"] for p in payload["providers"]} == {"groq", "gemini"}
    assert [p["drafts_replies"] for p in payload["providers"]] == [False, True]


def test_outreach_prompt_split_out_matches_what_generation_sends():
    """eval_models.py scores agent.outreach_prompt directly; if that drifts from
    the prompt generate_outreach_email actually sends, every model comparison it
    prints is measuring the wrong thing."""
    sent = {}
    with patched(agent, "_chat", lambda s, u, p: sent.update(prompt=u) or GOOD_EMAIL):
        agent.generate_outreach_email(ACCOUNT, "John", "Acme", "spoke at a conference")
    assert sent["prompt"] == agent.outreach_prompt(ACCOUNT, "John", "Acme", "spoke at a conference")


# ------------------------------------------------------------------ plan quotas

def _quota_used(**by_kind):
    """Fake the usage meter with a fixed consumption per kind."""
    return patched(
        accounts_db, "usage_quantity_since",
        lambda account_id, kind, since_iso=None: by_kind.get(kind, 0),
    )


def test_an_unrecognized_plan_falls_back_to_the_smallest_entitlement():
    """A typo in the plan column, or a tier written by a newer version of this
    code, must not read as unlimited service. Fail toward the least
    entitlement, never the most."""
    assert plans.get("enterprise").name == "trial"
    assert plans.get(None).name == "trial"
    assert plans.get("").name == "trial"
    assert plans.get("  PILOT  ").name == "pilot", "case and padding are tolerated"


def test_the_daily_send_limit_follows_the_plan():
    """The cap used to be one global constant for every account."""
    assert plans.daily_send_limit_for({"plan": "trial"}) == 25
    assert plans.daily_send_limit_for({"plan": "team"}) == 100
    assert plans.daily_send_limit_for({}) == 25, "unmigrated row reads as trial"


def test_an_explicit_deployment_limit_still_outranks_the_plan():
    """Self-hosted single-tenant installs have no plans and no Stripe. An
    explicitly set DAILY_SEND_LIMIT must keep working for them."""
    with patched(config, "DAILY_SEND_LIMIT_OVERRIDE", 5):
        assert plans.daily_send_limit_for({"plan": "team"}) == 5


def test_reply_drafting_is_never_capped_because_the_site_sells_it_as_unlimited():
    for name in ("trial", "pilot", "team"):
        _, remaining, _ = plans.headroom({"id": "a", "plan": name}, usage.UNIT_DRAFT_REPLY)
        assert remaining is None, f"{name} must not cap reply drafts"


def test_the_quota_check_fails_closed_when_the_usage_query_fails():
    """A database blip must not read as "this account has used nothing". The
    naive implementation catches the error and returns 0, which turns a
    transient failure into free unlimited service for as long as it lasts."""
    def boom(account_id, kind, since_iso=None):
        raise RuntimeError("supabase unreachable")

    with patched(accounts_db, "usage_quantity_since", boom):
        try:
            plans.check({"id": "a", "plan": "trial"}, usage.UNIT_DRAFT_EMAIL)
        except RuntimeError:
            return
    assert False, "an unreadable usage count must refuse, not allow"


def test_lead_sourcing_refuses_before_spending_a_single_search():
    """The gate sits above the work. A naive implementation that checks the
    quota after sourcing has already burned three LLM calls and up to fifteen
    Tavily searches producing leads it must then throw away."""
    import leads

    spent = {"searches": 0, "llm": 0}
    with contextlib.ExitStack() as stack:
        stack.enter_context(_quota_used(**{usage.UNIT_LEAD_SOURCED: 50}))  # trial allows 50
        stack.enter_context(patched(
            leads.search, "tavily_search",
            lambda q, max_results=5: spent.__setitem__("searches", spent["searches"] + 1) or []))
        stack.enter_context(patched(
            leads, "_chat",
            lambda s, u, p: spent.__setitem__("llm", spent["llm"] + 1) or ""))
        try:
            leads.find_leads(ACCOUNT, "founders in logistics")
        except plans.QuotaExceeded as e:
            assert spent == {"searches": 0, "llm": 0}, "a refused batch must cost nothing"
            assert "Trial" in str(e) and "Upgrade to Pilot" in str(e), \
                "a quota refusal has to say what ran out and what to do about it"
            return
    assert False, "an exhausted lead allowance must refuse"


def test_sourced_leads_are_counted_by_quantity_not_one_row_per_batch():
    """Counting rows instead of summing quantity would let a trial account
    source unlimited leads simply by asking for them all in one call."""
    import leads

    recorded = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(accounts_db, "record_usage",
                                    lambda aid, kind, qty=1, detail="": recorded.append((kind, qty))))
        stack.enter_context(patched(leads, "_expand_queries", lambda t: ["q"]))
        stack.enter_context(patched(leads.search, "tavily_search",
                                    lambda q, max_results=5: [{"url": "u", "title": "t", "content": "c"}]))
        stack.enter_context(patched(leads, "_extract_candidates", lambda t, r: [
            {"name": f"P{i}", "company": "Acme", "url": "u",
             "reason": "fits", "email_guess": f"p{i}@acme.com"} for i in range(4)
        ]))
        stack.enter_context(patched(leads.sheets, "get_all_rows", lambda account: []))
        stack.enter_context(patched(leads.sheets, "append_rows", lambda account, rows: None))
        usage.set_account(ACCOUNT["id"])
        try:
            result = leads.find_leads(ACCOUNT, "founders")
        finally:
            usage.set_account(None)

    assert result["added"] == 4
    assert (usage.UNIT_LEAD_SOURCED, 4) in recorded, \
        f"four leads must be metered as quantity 4, got {recorded}"


def test_prepare_drafts_refuses_when_the_monthly_draft_quota_is_gone():
    drafted = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(_quota_used(**{usage.UNIT_DRAFT_EMAIL: 100}))  # trial allows 100
        stack.enter_context(patched(suppressions_db, "list_source_timestamps", lambda aid, src: []))
        stack.enter_context(patched(drafts_db, "count_sent_last_24_hours", lambda aid: 0))
        stack.enter_context(patched(drafts_db, "count_sent_since", lambda aid, since: 0))
        stack.enter_context(patched(sheets, "require_full_header", lambda account: None))
        stack.enter_context(patched(suppressions_db, "list_suppressed_emails", lambda aid: set()))
        stack.enter_context(patched(sheets, "campaign_readiness",
                                    lambda account, sent_today=0, suppressed_emails=None: _readiness([(2, _row())])))
        stack.enter_context(patched(sheets, "get_all_rows", lambda account: []))
        stack.enter_context(patched(drafts_db, "has_pending_for_row", lambda aid, idx: False))
        stack.enter_context(patched(agent, "generate_outreach_email",
                                    lambda *a, **k: drafted.append(1) or ("S", "B")))
        stack.enter_context(patched(send_outreach, "notify", lambda *a: None))
        try:
            send_outreach.prepare_drafts(ACCOUNT)
        except plans.QuotaExceeded as e:
            assert drafted == [], "no LLM call may be spent past the quota"
            assert "100" in str(e), "the refusal states the limit"
            return
    assert False, "an exhausted draft quota must refuse"


def test_prepare_drafts_trims_the_batch_to_what_the_plan_has_left():
    """A batch straddling the boundary delivers up to the line rather than
    failing whole -- but not one draft past it."""
    rows = [(i, _row(email=f"p{i}@x.com")) for i in range(2, 12)]  # 10 eligible
    drafted = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(_quota_used(**{usage.UNIT_DRAFT_EMAIL: 97}))  # 3 of 100 left
        stack.enter_context(patched(suppressions_db, "list_source_timestamps", lambda aid, src: []))
        stack.enter_context(patched(drafts_db, "count_sent_last_24_hours", lambda aid: 0))
        stack.enter_context(patched(drafts_db, "count_sent_since", lambda aid, since: 0))
        stack.enter_context(patched(sheets, "require_full_header", lambda account: None))
        stack.enter_context(patched(suppressions_db, "list_suppressed_emails", lambda aid: set()))
        stack.enter_context(patched(sheets, "campaign_readiness",
                                    lambda account, sent_today=0, suppressed_emails=None: _readiness(rows)))
        stack.enter_context(patched(sheets, "get_all_rows", lambda account: []))
        stack.enter_context(patched(drafts_db, "has_pending_for_row", lambda aid, idx: False))
        stack.enter_context(patched(agent, "generate_outreach_email",
                                    lambda *a, **k: drafted.append(1) or ("S", "B")))
        stack.enter_context(patched(drafts_db, "add_draft", lambda *a: len(drafted)))
        stack.enter_context(patched(send_outreach, "notify", lambda *a: None))
        result = send_outreach.prepare_drafts(ACCOUNT)

    assert len(drafted) == 3, f"exactly the remaining allowance, got {len(drafted)}"
    assert result["prepared"] == 3 and result["quota_trimmed"] == 7


def test_plan_limits_match_the_numbers_on_the_pricing_page():
    """The pricing page is the contract; this module is its implementation. A
    number that appears on one and not the other is a claim we do not honour --
    the same class of untruth the verification copy had to be fixed for."""
    page = (Path(__file__).resolve().parent.parent
            / "site" / "src" / "components" / "pricing.tsx").read_text(encoding="utf-8")

    assert "25 approved sends per day" in page
    assert plans.PLANS["trial"].daily_send_limit == 25

    assert "50 sourced leads" in page
    assert plans.PLANS["trial"].lead_allowance == 50
    assert plans.PLANS["trial"].lead_window == "lifetime", \
        "the page writes the trial's leads without a /mo suffix"

    assert "500 sourced leads/mo" in page
    assert plans.PLANS["pilot"].lead_allowance == 500
    assert plans.PLANS["pilot"].lead_window == "month"

    assert "Unlimited reply drafts" in page
    assert plans.PLANS["pilot"].monthly_replies is None

    for tier, price in (("Pilot", 49), ("Team", 149)):
        assert f"${price}/mo" in page
        assert plans.PLANS[tier.lower()].price_monthly_usd == price


# --------------------------------------------------------------------- billing

_WEBHOOK_SECRET = "whsec_test_secret"


def _signed(payload: bytes, secret=_WEBHOOK_SECRET, timestamp=None, scheme="v1"):
    import hashlib
    import hmac as _hmac

    ts = str(int(time.time() if timestamp is None else timestamp))
    sig = _hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},{scheme}={sig}"


def _event(kind, obj):
    import json as _json

    return _json.dumps({"type": kind, "data": {"object": obj}}).encode()


def test_the_webhook_refuses_an_event_it_cannot_verify():
    """This endpoint is public and grants paid plans. Every one of these is a
    free upgrade for a stranger if it is accepted."""
    import billing

    payload = _event("checkout.session.completed", {})
    with patched(billing, "STRIPE_WEBHOOK_SECRET", _WEBHOOK_SECRET):
        for description, header in [
            ("no signature header at all", None),
            ("empty header", ""),
            ("no v1 signature", "t=123"),
            ("garbage signature", f"t={int(time.time())},v1=deadbeef"),
            ("signed with the wrong secret", _signed(payload, secret="whsec_attacker")),
            ("valid signature over a different body", _signed(_event("x", {"a": 1}))),
        ]:
            try:
                billing.verify_webhook(payload, header)
            except billing.WebhookRejected:
                continue
            assert False, f"must reject: {description}"


def test_the_webhook_refuses_everything_when_no_secret_is_configured():
    """"Not configured" must never degrade into "trust the caller". An
    unconfigured deployment that accepted events would hand out free plans."""
    import billing

    payload = _event("checkout.session.completed", {})
    with patched(billing, "STRIPE_WEBHOOK_SECRET", ""):
        try:
            billing.verify_webhook(payload, _signed(payload, secret=""))
        except billing.WebhookRejected:
            return
    assert False, "an unset signing secret must refuse, not accept"


def test_a_captured_webhook_cannot_be_replayed_later():
    """The signature stays valid forever; the timestamp is what expires. Without
    the tolerance check, one event captured off the wire upgrades an account
    again every time it is replayed."""
    import billing

    payload = _event("checkout.session.completed", {"payment_status": "paid"})
    stale = _signed(payload, timestamp=time.time() - billing.SIGNATURE_TOLERANCE_SECONDS - 60)
    with patched(billing, "STRIPE_WEBHOOK_SECRET", _WEBHOOK_SECRET):
        try:
            billing.verify_webhook(payload, stale)
        except billing.WebhookRejected:
            pass
        else:
            assert False, "a signature older than the tolerance must be refused"
        # The same event inside the window is still accepted.
        fresh = _signed(payload)
        assert billing.verify_webhook(payload, fresh)["type"] == "checkout.session.completed"


def test_the_upgrade_follows_our_reference_not_anything_the_payer_typed():
    """client_reference_id is stamped by us from the authenticated session.
    customer_email is typed into Stripe's payment form by whoever is paying, so
    trusting it would let anyone upgrade a stranger's account -- or bill a
    stranger's card onto their own."""
    import billing

    change = billing.plan_change_from_event({
        "type": "checkout.session.completed",
        "data": {"object": {
            "payment_status": "paid",
            "client_reference_id": "acct-real",
            "customer_email": "victim@example.com",
            "metadata": {"account_id": "acct-real", "plan": "pilot"},
        }},
    })
    assert change == ("acct-real", "pilot")


def test_an_unpaid_checkout_session_grants_nothing():
    """Reaching the confirmation page is not paying."""
    import billing

    for status in ("unpaid", "no_payment_required", "paid"):
        change = billing.plan_change_from_event({
            "type": "checkout.session.completed",
            "data": {"object": {
                "payment_status": status,
                "client_reference_id": "acct-1",
                "metadata": {"account_id": "acct-1", "plan": "team"},
            }},
        })
        if status == "unpaid":
            assert change is None, "an unpaid session must not grant a plan"
        else:
            assert change == ("acct-1", "team")


def test_a_forged_plan_name_cannot_invent_a_tier():
    """The plan travels in metadata we set, but the event is still parsed
    defensively -- an unknown tier grants nothing rather than falling through to
    something permissive."""
    import billing

    assert billing.plan_change_from_event({
        "type": "checkout.session.completed",
        "data": {"object": {
            "payment_status": "paid",
            "client_reference_id": "acct-1",
            "metadata": {"account_id": "acct-1", "plan": "enterprise-unlimited"},
        }},
    }) is None


def test_cancelling_a_subscription_returns_the_account_to_the_trial():
    """Back to the free tier, not locked out: they still own their data."""
    import billing

    assert billing.plan_change_from_event({
        "type": "customer.subscription.deleted",
        "data": {"object": {"metadata": {"account_id": "acct-9"}}},
    }) == ("acct-9", "trial")


def test_an_unhandled_event_type_changes_nothing():
    """Stripe sends whatever the endpoint is subscribed to. An event we do not
    handle is not an error and must not raise."""
    import billing

    assert billing.plan_change_from_event(
        {"type": "invoice.created", "data": {"object": {}}}) is None
    assert billing.plan_change_from_event({}) is None


def test_checkout_is_refused_for_a_plan_that_is_not_sold():
    """The free tier has no price id; asking to check out on it must not
    produce a Stripe call with an empty price."""
    import billing

    with patched(billing, "STRIPE_SECRET_KEY", "sk_test_x"):
        try:
            billing.create_checkout_session({"id": "a", "email": "a@x.com"}, "trial")
        except billing.BillingNotConfigured:
            return
    assert False, "the trial is not a sellable plan"


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
