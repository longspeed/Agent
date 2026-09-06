"""Test suite for the outreach agent. All external boundaries (Gmail API,
OpenRouter, Supabase, Google Sheets) are faked -- nothing touches the network.

Run:  python tests/test_outreach_agent.py
"""
import base64
import contextlib
from datetime import datetime, timezone
import hashlib
import io
import json
import os
import sys
import time
import traceback
import zipfile
from pathlib import Path

import pytest

# Make config importable even without a .env (harmless if one exists).
for var, val in {
    "OPENROUTER_API_KEY": "test-key",
    "APP_SECRET_KEY": "test-secret",
    "SUPABASE_URL": "http://localhost",
    "SUPABASE_SECRET_KEY": "test-secret-key",
    # Worker telemetry is exercised with explicit fakes below. The main suite
    # must never reach Supabase merely because a watcher unit test runs.
    "WORKER_TELEMETRY_ENABLED": "0",
    "TRACKED_THREADS_ENABLED": "0",
    "GMAIL_THREAD_DISCOVERY_ENABLED": "0",
    # Legacy auto-send unit cases explicitly exercise the controlled path;
    # production defaults are verified separately with AUTO_SEND_ENABLED unset.
    "AUTO_SEND_ENABLED": "1",
    "COMMITMENTS_ENABLED": "0",
    "VOICE_FEEDBACK_ENABLED": "0",
}.items():
    os.environ.setdefault(var, val)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "outreach-agent"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for `import server`

import accounts_db
import agent
import auth
import bounces
import commitments
import commitments_db
import config
import dns_check
import drafts_db
import gmail
import google_auth
import notify
import providers
import ratelimit
import reviews_db
import sheet_template
import sheets
import send_outreach
import suppressions_db
import tracked_threads
import watch_replies

import plans
import usage
import voice_signals
import worker_db

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


def msg(from_addr, text, msg_id=None, extra_headers=None):
    """A thread message. Every real Gmail message carries an id and the reply
    dedupe key is built on it, so the default derives one deterministically from
    the content rather than leaving it absent -- a fixture without an id would
    silently exercise the legacy body-matching fallback instead of the real
    path. Pass msg_id explicitly to model two distinct messages with identical
    text, which is exactly the collision the body key could not survive.
    extra_headers adds headers beyond From, e.g. {"Auto-Submitted": "auto-replied"}."""
    digest = hashlib.md5(f"{from_addr}|{text}".encode()).hexdigest()[:12]
    headers = [{"name": "From", "value": f"Someone <{from_addr}>"}]
    if extra_headers:
        headers.extend({"name": k, "value": v} for k, v in extra_headers.items())
    return {
        "id": msg_id or f"m-{digest}",
        "payload": {
            "headers": headers,
            "body": {"data": _enc(text)},
        }
    }


def fake_gmail_service(messages, profile=None, send_as=None):
    """profile/send_as let a test exercise gmail.get_own_addresses directly;
    every existing call site passes only `messages` and gets the same
    {"emailAddress": "me@me.com"} / no-aliases defaults every fixture in this
    file already assumes."""
    class Exec:
        def __init__(self, value):
            self.value = value

        def execute(self):
            return self.value

    class Threads:
        def get(self, **kw):
            return Exec({"messages": messages})

    class SendAs:
        def list(self, **kw):
            if isinstance(send_as, Exception):
                raise send_as
            return Exec(send_as if send_as is not None else {"sendAs": []})

    class Settings:
        def sendAs(self):
            return SendAs()

    class Users:
        def threads(self):
            return Threads()

        def getProfile(self, **kw):
            return Exec(profile if profile is not None else {"emailAddress": "me@me.com"})

        def settings(self):
            return Settings()

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

OWN = {"me@me.com"}


def test_history_happy_path():
    messages = [msg("me@me.com", "our outreach"), msg("john@x.com", "their reply")]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        result = gmail.get_latest_reply_with_history(ACCOUNT, "t1", "john@x.com", OWN)
    assert result["text"] == "their reply"
    assert result["kind"] == "on_sheet"
    assert result["answered_elsewhere"] is False
    assert result["from_addr"] == "john@x.com"
    assert result["history"] == [(False, "our outreach")]
    assert result["message_id"] == messages[-1]["id"], (
        "the reply's own Gmail message id is the dedupe key -- returning None "
        "here silently drops the review queue back to matching on body text"
    )


def test_history_latest_is_ours_marks_answered_elsewhere():
    """This used to return None -- the exact bug Phase 0 exists to fix: the
    operator replies manually from Gmail before a poll runs, and the
    contact's earlier reply was silently buried forever. It must now surface,
    just flagged as already answered rather than drafted again."""
    messages = [msg("me@me.com", "outreach"), msg("john@x.com", "reply"), msg("me@me.com", "our answer")]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        result = gmail.get_latest_reply_with_history(ACCOUNT, "t1", "john@x.com", OWN)
    assert result["text"] == "reply"
    assert result["kind"] == "on_sheet"
    assert result["answered_elsewhere"] is True
    assert result["history"] == [(False, "outreach")]


def test_history_single_message_returns_none():
    with patched(gmail, "_get_service", lambda account: fake_gmail_service([msg("me@me.com", "outreach")])):
        assert gmail.get_latest_reply_with_history(ACCOUNT, "t1", "j@x.com", OWN) is None


def test_history_contact_match_case_insensitive():
    messages = [msg("me@me.com", "outreach"), msg("John@X.com", "yes!")]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        result = gmail.get_latest_reply_with_history(ACCOUNT, "t1", " john@x.com ", OWN)
    assert result["text"] == "yes!"


def test_history_multi_turn_labels():
    messages = [
        msg("me@me.com", "outreach"),
        msg("john@x.com", "question?"),
        msg("me@me.com", "answer"),
        msg("john@x.com", "follow-up"),
    ]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        result = gmail.get_latest_reply_with_history(ACCOUNT, "t1", "john@x.com", OWN)
    assert result["text"] == "follow-up"
    assert [h[0] for h in result["history"]] == [False, True, False]
    assert [h[1] for h in result["history"]] == ["outreach", "question?", "answer"]


def test_history_off_sheet_reply_is_flagged():
    messages = [msg("me@me.com", "outreach"), msg("assistant@other.com", "on behalf of john")]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        result = gmail.get_latest_reply_with_history(ACCOUNT, "t1", "john@x.com", OWN)
    assert result["kind"] == "off_sheet"
    assert result["from_addr"] == "assistant@other.com"
    assert result["answered_elsewhere"] is False
    assert result["history"] == [(False, "outreach")], "an off-sheet stranger is never labelled as the contact"


def test_history_off_sheet_senders_own_earlier_message_is_labelled_from_them():
    """History is labelled against the CANDIDATE's own address, not
    contact_email -- for an off-sheet candidate these differ. An earlier
    message from the same off-sheet sender must read as "from them" in the
    history the LLM sees, not as a stranger's aside, and this has to match
    what get_history_before rebuilds later at confirm time (see the
    consistency test below)."""
    messages = [
        msg("me@me.com", "outreach"),
        msg("assistant@other.com", "he's traveling, I'm covering for him"),
        msg("me@me.com", "understood, thanks"),
        msg("assistant@other.com", "following up on this"),
    ]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        result = gmail.get_latest_reply_with_history(ACCOUNT, "t1", "john@x.com", OWN)
    assert result["kind"] == "off_sheet"
    assert result["from_addr"] == "assistant@other.com"
    assert [h[0] for h in result["history"]] == [False, True, False], (
        "the assistant's own earlier message (index 1, 'he's traveling...') must "
        "read as from_contact=True -- labelling against contact_email (the sheet's "
        "address, never used here) would mark it False instead"
    )


def test_history_send_as_alias_is_ours_not_off_sheet():
    """A reply sent via a Send-As alias must not read as an unconfirmed
    stranger asking the operator to confirm their own email is really them."""
    messages = [msg("me@me.com", "outreach"), msg("alias@me.com", "reply from my alias")]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        result = gmail.get_latest_reply_with_history(
            ACCOUNT, "t1", "john@x.com", {"me@me.com", "alias@me.com"}
        )
    assert result is None, "a message from any of our own addresses is never a candidate"


def test_history_auto_responder_is_excluded_entirely():
    messages = [
        msg("me@me.com", "outreach"),
        msg("john@x.com", "out of office", extra_headers={"Auto-Submitted": "auto-replied"}),
    ]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        result = gmail.get_latest_reply_with_history(ACCOUNT, "t1", "john@x.com", OWN)
    assert result is None


def test_history_daemon_message_never_becomes_a_candidate():
    """A bounce must stay invisible here even when it's the only non-"ours"
    message on the thread -- watch_replies.py only calls find_bounce when
    this returns None, so a daemon message misread as a reply candidate
    would make bounce suppression unreachable."""
    messages = [msg("me@me.com", "outreach"), msg("mailer-daemon@x.com", "bounce report")]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        result = gmail.get_latest_reply_with_history(ACCOUNT, "t1", "john@x.com", OWN)
    assert result is None


def test_get_own_addresses_includes_verified_send_as_aliases():
    profile = {"emailAddress": "Me@Me.com"}
    send_as = {"sendAs": [
        {"sendAsEmail": "Alias@Me.com", "verificationStatus": "accepted"},
        {"sendAsEmail": "pending@me.com", "verificationStatus": "pending"},
    ]}
    with patched(gmail, "_get_service",
                 lambda account: fake_gmail_service([], profile=profile, send_as=send_as)):
        addresses, degraded = gmail.get_own_addresses(ACCOUNT)
    assert addresses == {"me@me.com", "alias@me.com"}, "verified alias in, unverified one out, all lower-cased"
    assert degraded is False


def test_get_own_addresses_degrades_to_primary_when_send_as_fails():
    with patched(gmail, "_get_service",
                 lambda account: fake_gmail_service([], send_as=RuntimeError("quota exceeded"))):
        addresses, degraded = gmail.get_own_addresses(ACCOUNT)
    assert addresses == {"me@me.com"}, "falls back to the primary rather than losing reply detection entirely"
    assert degraded is True


def test_get_history_before_matches_get_latest_reply_with_historys_own_history():
    """Drafting an off-sheet reply is deferred to confirm time, so
    get_history_before rebuilds what get_latest_reply_with_history would have
    built inline -- they must agree, not just happen to look similar, or the
    LLM sees different context depending on which path built it."""
    messages = [
        msg("me@me.com", "outreach"),
        msg("john@x.com", "question?"),
        msg("me@me.com", "answer"),
        msg("john@x.com", "follow-up", msg_id="m-followup"),
    ]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        built_inline = gmail.get_latest_reply_with_history(ACCOUNT, "t1", "john@x.com", OWN)
        rebuilt = gmail.get_history_before(ACCOUNT, "t1", "m-followup", "john@x.com", OWN)
    assert rebuilt == built_inline["history"]


def test_get_history_before_matches_for_an_off_sheet_candidate_too():
    """The consistency test above only exercises the on-sheet case, where
    contact_email and the candidate's own from_addr are the same string --
    it would pass even if the two functions used different targets entirely.
    This is the case that would actually catch that divergence:
    _confirm_sender_job calls get_history_before with review["email"], which
    for a flagged review is the OBSERVED off-sheet address, not the sheet's
    contact_email -- so that's what has to be passed here too."""
    messages = [
        msg("me@me.com", "outreach"),
        msg("assistant@other.com", "he's traveling, I'm covering for him", msg_id="m-first"),
        msg("me@me.com", "understood, thanks"),
        msg("assistant@other.com", "following up on this", msg_id="m-followup"),
    ]
    with patched(gmail, "_get_service", lambda account: fake_gmail_service(messages)):
        built_inline = gmail.get_latest_reply_with_history(ACCOUNT, "t1", "john@x.com", OWN)
        # Mirrors server._confirm_sender_job exactly: the target passed here
        # is the observed sender (built_inline["from_addr"]), never the
        # sheet's original contact_email.
        rebuilt = gmail.get_history_before(
            ACCOUNT, "t1", "m-followup", built_inline["from_addr"], OWN
        )
    assert built_inline["kind"] == "off_sheet"
    assert rebuilt == built_inline["history"]
    assert rebuilt == [
        (False, "outreach"),
        (True, "he's traveling, I'm covering for him"),
        (False, "understood, thanks"),
    ]


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
        self._in = []

    def table(self, name): return self
    def select(self, cols): return self
    def limit(self, n): return self
    def order(self, col): return self

    def eq(self, col, val):
        self._eq.append((col, val))
        return self

    def is_(self, col, val):
        self._null.append(col)
        return self

    def in_(self, col, values):
        self._in.append((col, list(values)))
        return self

    def execute(self):
        matched = [
            r for r in self.rows
            if all(r.get(c) == v for c, v in self._eq)
            and all(r.get(c) is None for c in self._null)
            and all(r.get(c) in vs for c, vs in self._in)
        ]
        self._eq, self._null, self._in = [], [], []
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


class _ReviewsTable:
    """Fuller PostgREST-shaped fake than _ReviewRows above: adds insert/update
    so add_review and confirm_sender can be exercised against real rows,
    not just the read chain find_review_id uses."""

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self._next_id = max([r["id"] for r in self.rows], default=0) + 1
        self._eq = []
        self._null = []
        self._mode = None
        self._values = None

    def table(self, name):
        return self

    def select(self, cols):
        self._mode = "select"
        return self

    def insert(self, values):
        self._mode = "insert"
        self._values = values
        return self

    def update(self, values):
        self._mode = "update"
        self._values = values
        return self

    def limit(self, n): return self
    def order(self, col): return self

    def eq(self, col, val):
        self._eq.append((col, val))
        return self

    def is_(self, col, val):
        self._null.append(col)
        return self

    def _matched(self):
        return [
            r for r in self.rows
            if all(r.get(c) == v for c, v in self._eq)
            and all(r.get(c) is None for c in self._null)
        ]

    def execute(self):
        mode, eq, null = self._mode, self._eq, self._null
        self._eq, self._null = [], []
        if mode == "insert":
            row = dict(self._values)
            row["id"] = self._next_id
            self._next_id += 1
            self.rows.append(row)
            return type("R", (), {"data": [row]})()
        matched = [
            r for r in self.rows
            if all(r.get(c) == v for c, v in eq) and all(r.get(c) is None for c in null)
        ]
        if mode == "update":
            for r in matched:
                r.update(self._values)
        return type("R", (), {"data": matched})()


def test_add_review_defaults_to_pending_and_not_degraded():
    fake = _ReviewsTable()
    with patched(reviews_db, "_get_client", lambda: fake):
        reviews_db.add_review("a1", 2, "John", "john@x.com", "t1", "hi", "draft text")
    row = fake.rows[0]
    assert row["status"] == "pending"
    assert row["degraded_classification"] is False
    assert row["original_draft_reply"] == "draft text"


def test_add_review_writes_none_not_empty_string_when_there_is_no_draft():
    """Phase 6's edit-diff learning reads original_draft_reply against what the
    operator actually sent; ("", "the operator's entire hand-written reply")
    reads as "replace the model's output wholesale" instead of "the model
    wrote nothing, this pair teaches nothing." Applies regardless of why
    there's no draft -- a flagged review that skipped drafting, or a normal
    one where drafting failed, both land here the same way."""
    fake = _ReviewsTable()
    with patched(reviews_db, "_get_client", lambda: fake):
        reviews_db.add_review("a1", 2, "John", "john@x.com", "t1", "hi", "", status="flagged")
    assert fake.rows[0]["original_draft_reply"] is None
    assert fake.rows[0]["draft_reply"] == ""


def test_add_review_preserves_a_missing_sheet_row_for_gmail_only_reviews():
    """A Gmail-discovered thread may have no Sheet row.  Persist NULL rather
    than inventing a row number that could later address the wrong contact."""
    fake = _ReviewsTable()
    with patched(reviews_db, "_get_client", lambda: fake):
        reviews_db.add_review(
            "a1", None, "John", "john@x.com", "t1", "hi", "",
            gmail_message_id="msg-1", status="flagged",
        )
    assert fake.rows[0]["row_index"] is None


def test_add_review_retries_with_a_reserved_marker_on_a_legacy_not_null_schema():
    """Code and schema deploy independently. A pre-migration database rejects
    the truthful NULL; zero is outside the valid Sheet row domain and keeps the
    reply visible until the migration normalizes it back to NULL."""
    class LegacyReviewsTable(_ReviewsTable):
        rejected_null = False

        def execute(self):
            if (
                self._mode == "insert"
                and self._values.get("row_index") is None
                and not self.rejected_null
            ):
                self.rejected_null = True
                raise Exception(
                    "23502: null value in column row_index violates not-null constraint"
                )
            return super().execute()

    fake = LegacyReviewsTable()
    with patched(reviews_db, "_get_client", lambda: fake):
        review_id = reviews_db.add_review(
            "a1", None, "John", "john@x.com", "t1", "hi", "",
            gmail_message_id="msg-legacy", status="flagged",
        )
    assert review_id == 1
    assert fake.rows[0]["row_index"] == reviews_db.LEGACY_NO_SHEET_ROW


def test_add_review_accepts_an_explicit_status_and_degraded_flag():
    fake = _ReviewsTable()
    with patched(reviews_db, "_get_client", lambda: fake):
        reviews_db.add_review(
            "a1", 2, "John", "assistant@other.com", "t1", "hi", "",
            status="flagged", degraded_classification=True,
        )
    assert fake.rows[0]["status"] == "flagged"
    assert fake.rows[0]["degraded_classification"] is True


def test_add_manual_review_uses_message_dedupe_without_creating_an_alert():
    class RpcCapableReviews(_ReviewsTable):
        def rpc(self, *_args, **_kwargs):
            raise AssertionError("an operator-open composer must not enqueue an email alert")

    fake = RpcCapableReviews()
    with patched(reviews_db, "_get_client", lambda: fake):
        review_id, created = reviews_db.add_manual_review(
            "a1", None, "John", "john@x.com", "t1", "latest reply",
            "draft text", gmail_message_id="msg-manual", return_created=True,
        )

    assert (review_id, created) == (1, True)
    assert fake.rows[0]["gmail_message_id"] == "msg-manual"
    assert fake.rows[0]["original_draft_reply"] == "draft text"


def test_confirm_sender_promotes_flagged_to_pending_with_the_draft():
    fake = _ReviewsTable(rows=[
        {"id": 9, "account_id": "a1", "status": "flagged", "draft_reply": "", "original_draft_reply": None},
    ])
    with patched(reviews_db, "_get_client", lambda: fake):
        confirmed = reviews_db.confirm_sender("a1", 9, draft_reply="Sure, happy to help.")
    assert confirmed is True
    row = fake.rows[0]
    assert row["status"] == "pending"
    assert row["draft_reply"] == "Sure, happy to help."
    assert row["original_draft_reply"] == "Sure, happy to help."


def test_confirm_sender_writes_none_when_drafting_produced_nothing():
    fake = _ReviewsTable(rows=[{"id": 9, "account_id": "a1", "status": "flagged"}])
    with patched(reviews_db, "_get_client", lambda: fake):
        reviews_db.confirm_sender("a1", 9, draft_reply="")
    assert fake.rows[0]["original_draft_reply"] is None


def test_confirm_sender_is_a_no_op_on_a_non_flagged_review():
    for status in ("pending", "sent", "dismissed"):
        fake = _ReviewsTable(rows=[{"id": 9, "account_id": "a1", "status": status}])
        with patched(reviews_db, "_get_client", lambda: fake):
            confirmed = reviews_db.confirm_sender("a1", 9, draft_reply="whatever")
        assert confirmed is False, f"must not resurrect a {status} review"
        assert fake.rows[0]["status"] == status, "and must not touch it either"


def test_reviews_mark_rewritten_writes_the_flag():
    fake = _ReviewsTable(rows=[{"id": 9, "account_id": "a1", "rewritten": False}])
    with patched(reviews_db, "_get_client", lambda: fake):
        reviews_db.mark_rewritten("a1", 9)
    assert fake.rows[0]["rewritten"] is True


def test_reviews_mark_rewritten_is_best_effort():
    def boom():
        raise RuntimeError("supabase down")
    with patched(reviews_db, "_get_client", boom):
        reviews_db.mark_rewritten("a1", 9)  # must not raise


def test_reviews_mark_sent_does_not_touch_rewritten():
    """Regression guard for the finding that shaped this design: mark_sent
    is the durable record require_send_log-equivalent logic gates the whole
    send on -- rewritten must never join that update. Seeded True (as if
    mark_rewritten already ran) so an accidental "rewritten": False in
    mark_sent's own update dict would flip it back and be caught here,
    rather than trusting by convention that it was never added."""
    fake = _ReviewsTable(rows=[{"id": 9, "account_id": "a1", "status": "pending", "rewritten": True}])
    with patched(reviews_db, "_get_client", lambda: fake):
        reviews_db.mark_sent("a1", 9, "sent body")
    assert fake.rows[0]["rewritten"] is True, "mark_sent must never write the rewritten column"


# ------------------------------------------------- reply validation

def test_validate_reply_flags_each_failure_mode():
    long_reply = "Thanks, that works. " * 80
    cases = [
        ("hi", "truncated"),
        (long_reply, "characters"),
        ("Sounds good, book here: https://evil.example/pay", "Remove these links"),
        ("Sounds good დღის talk soon, best regards Sam", "garbled"),
        ("Happy to help [Name], talk soon and best wishes from Sam", "placeholder"),
        ("**Sounds good** to me, happy to talk next week, best Sam", "markdown"),
        ("Subject: Re: our chat\nSounds good, talk next week, best Sam", "subject line"),
        ("Just following up on this, let me know when suits, best Sam", "Remove these phrases"),
    ]
    for body, expected in cases:
        problems = agent._validate_reply(body, "https://cal.example/sam")
        assert any(expected.lower() in p.lower() for p in problems), (
            f"{expected!r} not flagged for {body[:50]!r}: {problems}"
        )


def test_validate_reply_allows_what_the_outreach_validator_would_reject():
    """The three concrete reasons _validate_outreach cannot be reused here. Each
    of these is a correct reply that the outreach rules would reject and then
    regenerate into a worse one."""
    link = "https://cal.example/sam"

    mirrored = "Thanks for reaching out, that timing works. I'll send an invite. Sam"
    assert agent._validate_reply(mirrored, link) == [], (
        "_BANNED_PHRASES holds 'reaching out', but REPLY_SYSTEM says to reference "
        "their actual words -- mirroring a prospect who wrote it must be allowed"
    )

    brief = "No problem, thanks for letting me know. Sam"
    assert agent._validate_reply(brief, link) == [], (
        "_MIN_BODY_CHARS is 120; the right answer to 'not interested' is about "
        f"{len(brief)} characters and REPLY_SYSTEM asks for exactly that"
    )

    no_link = "Good questions. We support SSO and SCIM today. Happy to go deeper. Sam"
    assert agent._validate_reply(no_link, link) == [], (
        "the calendar link is mandatory for outreach and conditional for replies "
        "-- REPLY_SYSTEM includes it only when they show interest"
    )


def test_validate_reply_allows_the_senders_own_link_only():
    link = "https://cal.example/sam"
    ours = f"Great, grab a slot here: {link} and I'll see you then. Sam"
    assert agent._validate_reply(ours, link) == []

    theirs = "Great, book instead at https://attacker.example/x and see you then. Sam"
    problems = agent._validate_reply(theirs, link)
    assert problems and "attacker.example" in problems[0], (
        "a link the prospect planted in their own message is the injection "
        f"outcome that costs something: {problems}"
    )


def test_a_reply_that_never_validates_is_returned_not_raised():
    """Outreach raises after two failed attempts, and that is right there --
    nothing was sent and the row is untouched. A reply that raises means no
    review row, which means the operator is never told a prospect wrote back.
    The draft is a convenience; the notification is the product."""
    calls = []

    def always_bad(system, prompt, purpose):
        calls.append(prompt)
        return "hi"

    with patched(agent, "_chat", always_bad):
        out = agent.draft_reply(ACCOUNT, "John", "Acme", "are you still there?")
    assert out == "hi", "the last attempt is handed back rather than lost"
    assert len(calls) == 2, f"one retry, showing the model its own output: {len(calls)}"
    assert "hi" in calls[1], "the retry must include the rejected text to correct"


# ------------------------------------------------------------- rewrite: reply

def test_rewrite_reply_prompt_contains_the_fenced_instruction_and_custom_instructions():
    account = dict(ACCOUNT, custom_instructions="Keep replies to two short lines.")
    prompt = agent.rewrite_reply_prompt(account, "John", "Acme", "sounds good", "current draft text", "more casual")
    assert "more casual" in prompt
    assert "never invent a fact" in prompt.lower()
    assert "two short lines" in prompt
    assert "current draft text" in prompt
    assert "sounds good" in prompt


def test_rewrite_reply_calls_chat_with_draft_reply_purpose():
    captured = {}
    def fake_chat(system, user_prompt, purpose):
        captured["purpose"] = purpose
        return "A perfectly fine short reply."
    with patched(agent, "_chat", fake_chat):
        agent.rewrite_reply(ACCOUNT, "John", "Acme", "sounds good", "old draft", "shorter")
    assert captured["purpose"] == usage.DRAFT_REPLY


def test_rewrite_reply_succeeds_on_a_valid_first_response():
    with patched(agent, "_chat", lambda s, u, p: "Sure, happy to help with that."):
        result = agent.rewrite_reply(ACCOUNT, "John", "Acme", "sounds good", "old draft", "shorter")
    assert result == "Sure, happy to help with that."


def test_rewrite_reply_retries_once_then_succeeds():
    outputs = iter(["hi", "Sure, happy to help with that whenever works for you."])
    with patched(agent, "_chat", lambda s, u, p: next(outputs)):
        result = agent.rewrite_reply(ACCOUNT, "John", "Acme", "sounds good", "old draft", "shorter")
    assert "happy to help" in result


def test_rewrite_reply_raises_after_2_failed_attempts_unlike_draft_reply():
    """The one deliberate behavioral divergence from draft_reply: draft_reply
    never raises, because failing there would mean no review row and the
    operator never learns a prospect replied. Here the review already
    exists with its current draft intact, so a failed rewrite has nothing
    to lose by raising -- and an honest error beats silently keeping a
    possibly-broken attempt."""
    calls = {"n": 0}
    def always_bad(system, prompt, purpose):
        calls["n"] += 1
        return "hi"
    with patched(agent, "_chat", always_bad):
        try:
            agent.rewrite_reply(ACCOUNT, "John", "Acme", "sounds good", "old draft", "shorter")
        except RuntimeError:
            assert calls["n"] == 2
            return
    raise AssertionError("a rewrite that never validates must raise, not return a broken draft")


def test_a_reply_is_queued_even_when_every_provider_is_down():
    """The failure this guards is silent and total: providers down or a free
    tier exhausted meant the exception escaped, add_review never ran, and a
    warm reply sat undetected with nothing anywhere saying so."""
    calls = {"rows": [(2, _row(status="Sent", thread="t1", name="John", email="john@x.com"))]}

    def dead(*a, **k):
        raise RuntimeError("No LLM provider could draft this (draft_reply).")

    targets = [t for t in _watch_env(calls, {"t1": ("are you still there?", [])})
               if not (t[0] is agent and t[1] == "draft_reply")]
    with contextlib.ExitStack() as stack:
        for target in targets:
            stack.enter_context(patched(*target))
        stack.enter_context(patched(agent, "draft_reply", dead))
        result = watch_replies.check_for_replies(ACCOUNT)

    assert len(result["reviews"]) == 1, "the reply must still reach the queue"
    assert calls["add_review"][0][2] == "", "queued with an empty draft, not lost"
    assert any("drafting a response failed" in e for e in result["row_errors"]), (
        f"and the operator is told why the box is empty: {result['row_errors']}"
    )


def test_link_check_catches_what_gmail_will_linkify_not_just_what_has_a_scheme():
    """The check was scheme-only, which is the one form an attacker has no
    reason to use: Gmail linkifies a bare domain, so it supplies the scheme on
    the recipient's screen for free. Requiring 'https://' meant checking for the
    considerate version of the attack."""
    link = "https://cal.com/oanh/15min"
    for body in ["Sure, book here: attacker.example/cal",
                 "Sure, book at www.attacker.example/cal",
                 "Details at evil-site.co.uk/pay now"]:
        assert agent._validate_reply(body + " " * 30, link), f"missed: {body!r}"


def test_the_senders_own_link_is_not_flagged_however_the_model_writes_it():
    """The cost of widening the pattern. A model that drops the scheme on the
    sender's OWN link would be flagged as foreign, the draft regenerated twice,
    and the operator handed a warning about their own calendar."""
    link = "https://cal.com/oanh/15min"
    for body in ["Grab a slot: https://cal.com/oanh/15min and see you then",
                 "Grab a slot: cal.com/oanh/15min and see you then",
                 "Grab a slot: www.cal.com/oanh/15min and see you then",
                 "Grab a slot: https://cal.com/oanh/15min. See you then"]:
        assert agent._validate_reply(body, link) == [], f"false flag: {body!r}"

    for prose in ["We support Xero. Also Stripe, and SSO is available today. Sam",
                  "Thanks Priya. I will follow up Tuesday, e.g. after your call. Sam"]:
        assert agent._validate_reply(prose, link) == [], f"false flag on prose: {prose!r}"


def test_the_link_check_does_not_pretend_to_be_complete():
    """Documents a door that is open on purpose. Mobile Gmail turns a phone
    number into a tel: handler with no URL involved -- a published exfiltration
    vector this pattern does not and cannot cover, along with whatever the next
    client decides to linkify. Recorded as a test so that if someone later
    closes it the failure reads as 'we improved this' rather than leaving a
    comment claiming coverage nobody verified.

    The reason it is acceptable to leave open: the control is provenance, not
    detection. A reply never enters the fast lane whatever this catches."""
    problems = agent._validate_reply(
        "Call +1 555 0100 to confirm the refund please." + " " * 20,
        "https://cal.com/oanh/15min",
    )
    assert problems == [], (
        "if this now fails, the phone-number vector is covered -- update the "
        "comment in agent._URL and this test rather than reverting"
    )
    assert reviews_db.lane({"validator_problems": None}) == "full", (
        "and the reply is still full-text regardless, which is the actual control"
    )


# ------------------------------------------------- approval lane (provenance)

def test_a_reply_can_never_reach_the_fast_lane():
    """The load-bearing guarantee. A reply is generated from a prompt containing
    a stranger's arbitrary text, so it goes to full-text review on provenance --
    a property of where the draft came from, not of what it says.

    Every argument below is a review that some content check might have waved
    through. None of them move the lane, because nothing about the individual
    reply is consulted."""
    assert reviews_db.FAST_LANE_ELIGIBLE is False
    for review in [
        {},
        {"draft_reply": "Sounds good, talk then. Sam", "validator_problems": None},
        {"draft_reply": "clean", "validator_problems": "", "status": "pending"},
        {"status": "pending", "degraded_classification": False},
    ]:
        assert reviews_db.lane(review) == "full", (
            f"no reply may be promoted to the fast path, got {reviews_db.lane(review)} "
            f"for {review}"
        )


def test_an_empty_problem_list_does_not_promote_a_reply():
    """The specific way this would rot. If a clean validator result moved a
    reply into the fast lane, then every blind spot in the validator would
    silently widen the low-attention surface -- and the blind spots are the
    known part. A bare domain like 'attacker.example/cal' carries no scheme, so
    the URL check does not fire, and Gmail linkifies it on the recipient's
    screen anyway. Detection decides what to SHOW the operator. It must never
    decide how long they look."""
    clean = {"draft_reply": "Great, speak then. Sam", "validator_problems": None}
    assert reviews_db.lane(clean) == "full"


def test_an_outreach_draft_in_the_queue_has_always_passed_validation():
    """Why drafts_db.lane needs no content check. generate_outreach_email RAISES
    after two failed attempts rather than returning the bad text, so a draft
    that reached the queue passed the validator by construction. A
    validator_problems check there would read as a safety gate and be a
    permanent no-op -- the shape this whole review keeps finding."""
    calls = []

    def always_bad(system, prompt, purpose):
        calls.append(prompt)
        return "Subject: hi\n\nhi"

    with patched(agent, "_chat", always_bad):
        try:
            agent.generate_outreach_email(ACCOUNT, "John", "Acme", "", "https://u/x")
            assert False, "must raise rather than return an invalid draft"
        except RuntimeError as e:
            assert "2 attempts" in str(e), str(e)
    assert len(calls) == 2
    assert drafts_db.lane({"subject": "hi", "body": "hi"}) == "fast"


def test_unresolved_reply_problems_are_recorded_on_the_review():
    """draft_reply hands back its last attempt even when validation still fails,
    so a queued reply can carry real problems. Before this column it looked
    identical to a clean one and the operator had no way to know which draft the
    validator had already objected to."""
    problems = agent.reply_problems(
        ACCOUNT, "Sure, book here instead: https://attacker.example/cal and see you then."
    )
    assert problems and "attacker.example" in problems[0], problems

    cap = _Capture()
    with patched(reviews_db, "find_review_id", lambda *a, **k: None), \
         patched(reviews_db, "_get_client", lambda: cap):
        reviews_db.add_review("a1", 2, "John", "j@x.com", "t1", "their reply",
                              "bad draft", gmail_message_id="m1",
                              validator_problems=problems)
    assert "attacker.example" in cap.inserted["validator_problems"]

    with patched(reviews_db, "find_review_id", lambda *a, **k: None), \
         patched(reviews_db, "_get_client", lambda: cap):
        reviews_db.add_review("a1", 2, "John", "j@x.com", "t1", "their reply",
                              "clean draft", gmail_message_id="m2",
                              validator_problems=[])
    assert cap.inserted["validator_problems"] is None, (
        "no problems must store NULL, not an empty string -- a reader cannot "
        "tell '' from 'not recorded yet'"
    )


# ------------------------------------------------- review visibility

def test_the_queue_shows_only_allowlisted_statuses():
    """Visibility is an explicit allowlist, not "anything not sent". Statuses
    are being added and they do not all behave alike: a review with no draft
    attached yet must be hidden, a flagged off-sheet sender must be visible or
    the confirm-sender question has nowhere to be asked. A rule like "not sent
    and not dismissed" would show the empty ones."""
    fake = _ReviewRows([
        {"id": 1, "account_id": "a1", "status": "pending"},
        {"id": 2, "account_id": "a1", "status": "sent"},
        {"id": 3, "account_id": "a1", "status": "dismissed"},
        {"id": 4, "account_id": "a1", "status": "drafting"},
        {"id": 5, "account_id": "a1", "status": "superseded"},
    ])
    with patched(reviews_db, "_get_client", lambda: fake):
        visible = [r["id"] for r in reviews_db.list_pending_reviews("a1")]
    assert visible == [1], f"only allowlisted statuses reach the queue, got {visible}"


def test_nothing_is_sendable_that_is_not_also_visible():
    """The invariant that keeps the two allowlists honest. A status the operator
    can send but cannot see is an email leaving on a review nobody looked at --
    which is the one thing this product promises cannot happen."""
    assert set(reviews_db.SENDABLE_STATUSES) <= set(reviews_db.VISIBLE_STATUSES), (
        "SENDABLE_STATUSES must be a subset of VISIBLE_STATUSES: "
        f"{set(reviews_db.SENDABLE_STATUSES) - set(reviews_db.VISIBLE_STATUSES)} "
        "is sendable but hidden"
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
         patched(drafts_db, "has_pending_for_row", lambda a, r: False), \
         patched(drafts_db, "has_first_touch_conflict", lambda *a, **k: False):
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


# --------------------------------------------------------- rewrite: outreach

_UNSUB = "https://app.example.com/unsubscribe?t=abc.def"


def test_strip_opt_out_line_removes_the_trailing_suffix():
    body = f"{GOOD_EMAIL_BODY}\n\nNot the right time? Unsubscribe here and I won't email again: {_UNSUB}"
    assert agent._strip_opt_out_line(body, _UNSUB) == GOOD_EMAIL_BODY


def test_strip_opt_out_line_removes_it_even_when_hand_edited_around():
    """A hand-edited body may have extra content after the opt-out line, an
    extra blank line, or whitespace drift -- none of which the operator did
    anything wrong to cause. A suffix-only match would silently no-op on any
    of these; content-based matching must not."""
    body = (
        f"{GOOD_EMAIL_BODY}\n\n"
        f"Not the right time? Unsubscribe here and I won't email again: {_UNSUB}\n\n"
        "P.S. one more thing I added after the opt-out line."
    )
    stripped = agent._strip_opt_out_line(body, _UNSUB)
    assert _UNSUB not in stripped
    assert "P.S. one more thing" in stripped


def test_strip_opt_out_line_is_a_no_op_when_absent():
    assert agent._strip_opt_out_line(GOOD_EMAIL_BODY, _UNSUB) == GOOD_EMAIL_BODY
    assert agent._strip_opt_out_line(GOOD_EMAIL_BODY, "") == GOOD_EMAIL_BODY


def test_rewrite_outreach_email_never_shows_the_model_the_unsubscribe_url():
    """rewrite_outreach_prompt renders whatever body it's handed verbatim --
    the stripping happens one level up, in rewrite_outreach_email, before
    the prompt is ever built. This tests that pipeline end to end via the
    prompt _chat actually receives, not the prompt-builder in isolation."""
    captured = {}
    body_with_optout = f"{GOOD_EMAIL_BODY}\n\nNot the right time? Unsubscribe here and I won't email again: {_UNSUB}"

    def fake_chat(system, user_prompt, purpose):
        captured["prompt"] = user_prompt
        return GOOD_EMAIL

    with patched(agent, "_chat", fake_chat):
        agent.rewrite_outreach_email(
            ACCOUNT, "John", "Acme", "Cutting invoice matching", body_with_optout,
            "make it shorter", unsubscribe_url=_UNSUB,
        )
    assert _UNSUB not in captured["prompt"], "the opt-out line must be stripped before the model ever sees the body"
    assert "make it shorter" in captured["prompt"]


def test_rewrite_outreach_prompt_contains_the_fenced_instruction_and_custom_instructions():
    account = dict(ACCOUNT, custom_instructions="Always mention our free trial.")
    prompt = agent.rewrite_outreach_prompt(
        account, "John", "Acme", "Subj", GOOD_EMAIL_BODY, "more casual",
    )
    assert "more casual" in prompt
    assert "never invent a fact" in prompt.lower()
    assert "free trial" in prompt


def test_rewrite_outreach_email_appends_the_opt_out_line_exactly_once():
    with patched(agent, "_chat", lambda s, u, p: GOOD_EMAIL):
        subject, body = agent.rewrite_outreach_email(
            ACCOUNT, "John", "Acme", "old subject", GOOD_EMAIL_BODY, "shorter", unsubscribe_url=_UNSUB,
        )
    assert body.count(_UNSUB) == 1


def test_rewrite_outreach_email_still_exactly_once_on_a_hand_edited_body():
    """Regression test for the failure mode this whole design targets: a
    hand-edited input body must not produce a doubled opt-out line."""
    hand_edited = (
        f"{GOOD_EMAIL_BODY}\n\n"
        f"Not the right time? Unsubscribe here and I won't email again: {_UNSUB}\n\n"
        "One more sentence the operator typed after it."
    )
    with patched(agent, "_chat", lambda s, u, p: GOOD_EMAIL):
        subject, body = agent.rewrite_outreach_email(
            ACCOUNT, "John", "Acme", "old subject", hand_edited, "shorter", unsubscribe_url=_UNSUB,
        )
    assert body.count(_UNSUB) == 1


def test_rewrite_outreach_email_calls_chat_with_draft_email_purpose():
    captured = {}
    def fake_chat(system, user_prompt, purpose):
        captured["purpose"] = purpose
        return GOOD_EMAIL
    with patched(agent, "_chat", fake_chat):
        agent.rewrite_outreach_email(ACCOUNT, "John", "Acme", "s", GOOD_EMAIL_BODY, "shorter")
    assert captured["purpose"] == usage.DRAFT_EMAIL


def test_rewrite_outreach_email_retries_once_then_succeeds():
    outputs = iter(["Subject: hi\n\ntoo short", GOOD_EMAIL])
    with patched(agent, "_chat", lambda s, u, p: next(outputs)):
        subject, body = agent.rewrite_outreach_email(ACCOUNT, "John", "Acme", "s", GOOD_EMAIL_BODY, "shorter")
    assert subject == "Cutting invoice matching"


def test_rewrite_outreach_email_raises_after_2_failed_attempts():
    calls = {"n": 0}
    def bad_chat(system, user_prompt, purpose):
        calls["n"] += 1
        return "Subject: hi\n\nLet's hop on a call to discuss synergies."
    with patched(agent, "_chat", bad_chat):
        try:
            agent.rewrite_outreach_email(ACCOUNT, "John", "Acme", "s", GOOD_EMAIL_BODY, "shorter")
        except RuntimeError:
            assert calls["n"] == 2
            return
    raise AssertionError("invalid rewrite output must raise, not return")


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


def test_meeting_purpose_guard_blocks_near_emptiness():
    """REGRESSION (BUGS.md BUG guard-audit / review 2026-08-05): the guard used
    to compare only against the literal DEFAULT_MEETING_PURPOSE string, so any
    other short filler sailed through. Below MIN_MEETING_PURPOSE_LENGTH is
    blocked regardless of wording."""
    account = {"sender_name": "Long", "meeting_purpose": "hi"}
    assert agent.account_send_blockers(account)
    account = {"sender_name": "Long", "meeting_purpose": "quick sync"}
    assert agent.account_send_blockers(account)


def test_meeting_purpose_guard_exact_length_boundary():
    """REGRESSION (eng review 2026-08-05): pins the exact cutoff so an
    off-by-one (< vs <=, or MIN_MEETING_PURPOSE_LENGTH drifting by one on a
    future edit) fails loudly instead of passing every other test silently.
    14 chars -- one under the floor -- must block; 15 -- exactly at it --
    must pass (assuming it isn't also a near-duplicate of the default,
    which neither of these is)."""
    assert len("xxxxxxxxxxxxxx") == 14
    assert len("xxxxxxxxxxxxxxx") == 15
    account = {"sender_name": "Long", "meeting_purpose": "xxxxxxxxxxxxxx"}
    assert agent.account_send_blockers(account)
    account = {"sender_name": "Long", "meeting_purpose": "xxxxxxxxxxxxxxx"}
    assert agent.account_send_blockers(account) == []


def test_settings_body_caps_meeting_purpose_length():
    """REGRESSION (eng review 2026-08-05, Performance): meeting_purpose had no
    length cap anywhere, so an adversarial paste ran unbounded through
    agent._is_near_duplicate_of_default's difflib comparison on every
    account_send_blockers() call. 500 chars is generous for "one sentence,
    used in every email" (the Settings UI's own description of the field)
    while bounding that cost."""
    import server
    from pydantic import ValidationError

    server.SettingsBody(meeting_purpose="x" * 500)  # must not raise
    try:
        server.SettingsBody(meeting_purpose="x" * 501)
        raise AssertionError("expected a validation error over 500 chars")
    except ValidationError:
        pass


def test_meeting_purpose_guard_allows_short_specific_purposes():
    """A short, concrete purpose must not be blocked just for being short --
    length is not a specificity check. Locks in the exact case that made the
    40-char version of this guard (an earlier draft) wrong: it would have
    blocked this."""
    account = {"sender_name": "Long", "meeting_purpose": "sell CLIs to devs"}
    assert agent.account_send_blockers(account) == []


def test_meeting_purpose_guard_blocks_paraphrases_of_the_default():
    from config import DEFAULT_MEETING_PURPOSE

    account = {"sender_name": "Long", "meeting_purpose": DEFAULT_MEETING_PURPOSE}
    assert agent.account_send_blockers(account)
    # A light reword of the default, not a byte-identical copy -- this is
    # what the near-duplicate check exists for; the old exact-string
    # comparison let this straight through.
    account = {
        "sender_name": "Long",
        "meeting_purpose": "a fast intro call to see if we are a fit to work together",
    }
    assert agent.account_send_blockers(account)


def test_meeting_purpose_guard_disclosed_gap_wordy_but_empty():
    """DISCLOSED GAP, not a bug: a wordy-but-content-free purpose that neither
    resembles the default nor is short enough to trip the near-emptiness floor
    is NOT caught. Verified during the 2026-08-05 review that no length floor
    can close this without also blocking legitimate short purposes (the
    previous test) -- length is not a proxy for specificity in either
    direction. This test exists so a future reader sees the gap is known and
    intentional, not an unnoticed regression -- see TODOS.md."""
    account = {"sender_name": "Long", "meeting_purpose": "To have a meeting with our team"}
    assert agent.account_send_blockers(account) == []


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


def test_full_nine_column_header_survives_the_write_gate():
    """Regression: the email-only schema must remain A:I, end to end."""
    with patched(sheets, "_get_service", lambda account: fake_sheets_service([list(sheets.EXPECTED_HEADER)])):
        sheets.require_full_header(ACCOUNT)


def test_short_sheet_rows_are_padded_to_the_email_schema():
    """Older sheets may omit trailing Sendkeep-owned columns."""
    values = [list(sheets.EXPECTED_HEADER), ["Jane", "jane@example.com", "Acme"]]
    with patched(sheets, "_get_service", lambda account: fake_sheets_service(values)):
        _, row = sheets.get_all_rows(ACCOUNT)[0]
    assert len(row) == len(sheets.EXPECTED_HEADER)
    assert row[sheets.COL_EMAIL_CONFIDENCE] == ""


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


def _fake_verify_service(cells_by_row):
    """Fake sheets service for _verify_row_index's fast path: .get() on a
    single-cell range like "B2" returns whatever email fixture-configured
    for row 2, simulating the sheet's actual current state at that row."""
    class Exec:
        def __init__(self, value):
            self.value = value

        def execute(self):
            return self.value

    class Values:
        def get(self, spreadsheetId, range):
            row_num = int(range[1:])
            email = cells_by_row.get(row_num)
            return Exec({"values": [[email]] if email else []})

        def batchUpdate(self, spreadsheetId, body):
            return Exec({})

    class Spreadsheets:
        def values(self):
            return Values()

    class Service:
        def spreadsheets(self):
            return Spreadsheets()

    return Service()


def test_verify_row_index_fast_path_confirms_without_a_full_scan():
    """The common case -- nothing moved -- must never call get_all_rows.
    That's the entire point of the fast path: this phase makes the write it
    guards continuous for every account, and the whole-sheet fallback is
    exactly the cost it exists to avoid paying every time."""
    with patched(sheets, "_get_service", lambda account: _fake_verify_service({2: "a@x.com"})):
        with patched(sheets, "get_all_rows",
                     lambda account: (_ for _ in ()).throw(AssertionError("must not fall back"))):
            assert sheets._verify_row_index(ACCOUNT, 2, "a@x.com") == 2
            assert sheets._verify_row_index(ACCOUNT, 2, " A@X.com ") == 2, "case/whitespace insensitive"


def test_verify_row_index_match_moved_and_missing():
    """Fast path misses in both cases (row 2 now holds a different email),
    so both fall through to the existing follow-or-fail scan, unchanged."""
    cells = {2: "a@x.com"}
    rows = [(2, _row(email="a@x.com")), (3, _row(email="b@x.com"))]
    with patched(sheets, "_get_service", lambda account: _fake_verify_service(cells)):
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
        def __init__(self, value=None):
            self.value = value or {}

        def execute(self):
            return self.value

    class Values:
        def get(self, spreadsheetId, range):
            if range == "A1:I1":
                return Exec({"values": [sheets.EXPECTED_HEADER]})
            # Row 2 no longer holds the expected email -- the fast path
            # must miss here so update_row's fallback scan (get_all_rows,
            # faked below) is what actually follows it to row 5.
            return Exec({"values": []})

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


def test_campaign_readiness_blocks_sourced_unverified_addresses_until_confirmed():
    row = _row(email="sourced@example.com")
    row[sheets.COL_EMAIL_CONFIDENCE] = "unverified"
    rows = [(2, row)]
    blocked = sheets.campaign_readiness(ACCOUNT, sent_today=0, rows=rows)
    assert blocked["eligible"] == []

    row[sheets.COL_EMAIL_CONFIDENCE] = "verified"
    confirmed = sheets.campaign_readiness(ACCOUNT, sent_today=0, rows=rows)
    assert [candidate[sheets.COL_EMAIL] for _, candidate in confirmed["eligible"]] == ["sourced@example.com"]


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


def test_list_recent_sent_threads_discovers_external_recipients_and_dedupes():
    class Exec:
        def __init__(self, value):
            self.value = value

        def execute(self):
            return self.value

    messages = {
        "m1": {"payload": {"headers": [
            {"name": "To", "value": "Jane Doe <jane@example.com>"},
            {"name": "Cc", "value": "me@me.com"},
            {"name": "Subject", "value": "Checking in"},
        ]}},
        "m2": {"payload": {"headers": [
            {"name": "To", "value": "jane@example.com"},
            {"name": "Subject", "value": "Re: Checking in"},
        ]}},
        "m3": {"payload": {"headers": [
            {"name": "To", "value": "me@me.com"},
            {"name": "Subject", "value": "Internal note"},
        ]}},
    }

    class Messages:
        def list(self, **kwargs):
            assert kwargs["q"] == "in:sent newer_than:30d"
            assert kwargs["maxResults"] == 50
            return Exec({"messages": [
                {"id": "m1", "threadId": "t1"},
                {"id": "m2", "threadId": "t1"},
                {"id": "m3", "threadId": "t2"},
            ]})

        def get(self, **kwargs):
            return Exec(messages[kwargs["id"]])

    class Users:
        def messages(self):
            return Messages()

    with patched(gmail, "_get_service", lambda account: type("S", (), {"users": lambda self: Users()})()):
        found = gmail.list_recent_sent_threads(
            ACCOUNT, {"me@me.com"}, days=30, max_threads=50
        )

    assert found == [{
        "thread_id": "t1",
        "message_id": "m1",
        "email": "jane@example.com",
        "name": "Jane Doe",
        "subject": "Checking in",
        "source": "gmail_discovery",
    }]


def test_find_sent_reply_requires_exact_sent_recipient_subject_and_body():
    """An ambiguous follow-up is resolved only by an exact Gmail Sent match."""
    raw = base64.urlsafe_b64encode(b"A useful reminder\n\nUnsubscribe: https://x.test/u").decode()
    thread = {
        "messages": [
            {"id": "incoming", "labelIds": [], "payload": {"headers": [
                {"name": "Subject", "value": "Checking in"},
            ], "body": {"data": base64.urlsafe_b64encode(b"hello").decode()}}},
            {"id": "sent", "labelIds": ["SENT"], "payload": {"headers": [
                {"name": "To", "value": "Jane <jane@example.com>"},
                {"name": "Subject", "value": "Re: Checking in"},
            ], "body": {"data": raw}}},
        ],
    }
    assert gmail.find_sent_reply(
        ACCOUNT, "t1", "jane@example.com",
        "A useful reminder\n\nUnsubscribe: https://x.test/u", thread=thread,
    )["id"] == "sent"
    assert gmail.find_sent_reply(ACCOUNT, "t1", "jane@example.com", "different", thread=thread) is None
    assert gmail.find_sent_reply(ACCOUNT, "t1", "other@example.com", "A useful reminder\n\nUnsubscribe: https://x.test/u", thread=thread) is None


def test_auto_send_is_blocked_unless_deployment_enables_it():
    with patched(config, "AUTO_SEND_ENABLED", False):
        with pytest.raises(RuntimeError, match="disabled in safety-first mode"):
            send_outreach.prepare_drafts(ACCOUNT, auto_send=True)


# ------------------------------------------------------------- watch_replies

def _watch_env(calls, reply_map, existing_reviews=None, bounce_map=None):
    """Builds patches for a check_for_replies run. reply_map: thread_id ->
    (reply, history) for the common on-sheet case, or a full candidate dict
    (see gmail.get_latest_reply_with_history) for a test that specifically
    needs an off-sheet/answered-elsewhere/degraded shape.
    bounce_map: thread_id -> the dict gmail.find_bounce would return."""
    existing = existing_reviews if existing_reviews is not None else {}
    bounces_by_thread = bounce_map or {}

    def fake_add_review(account_id, row_index, name, email, thread_id, customer_reply,
                        draft_reply, gmail_message_id=None, status="pending",
                        degraded_classification=False, validator_problems=None,
                        return_created=False):
        calls.setdefault("add_review", []).append(
            (thread_id, customer_reply, draft_reply, status, degraded_classification, email)
        )
        key = (thread_id, gmail_message_id)
        if key in existing:
            return (existing[key], False) if return_created else existing[key]
        existing[key] = len(existing) + 1
        return (existing[key], True) if return_created else existing[key]

    def fake_get_reply(account, t, e, own_addresses, thread=None):
        result = reply_map.get(t)
        if isinstance(result, Exception):
            raise result
        if result is None:
            return None
        if isinstance(result, dict):
            return result
        # (reply, history) at the call sites -- synthesize the rest so adding
        # richer fields did not force an edit to every test that shares this
        # fixture. One stable id per thread is the right model: the same
        # reply re-read on a later poll is the same Gmail message.
        reply, history = result
        if not reply:
            return None
        return {
            "text": reply, "history": history, "message_id": f"msg-{t}",
            "kind": "on_sheet", "answered_elsewhere": False, "from_addr": e,
        }

    def fake_get_thread(account, t):
        # Counted so a regression back to two full fetches per row is visible.
        calls.setdefault("thread_reads", []).append(t)
        return {"messages": [], "id": t}

    return [
        (reviews_db, "find_review_id",
            lambda account_id, t, mid, reply=None: existing.get((t, mid))),
        (sheets, "require_full_header", lambda account: calls.setdefault("header_gate", []).append(True)),
        (sheets, "get_reply_check_rows", lambda account: calls["rows"]),
        (drafts_db, "list_active_follow_ups", lambda account_id: []),
        (drafts_db, "list_sends_needing_reconciliation", lambda account_id: []),
        (drafts_db, "list_follow_ups_needing_reconciliation", lambda account_id: []),
        (reviews_db, "list_reviews_stuck_in_send", lambda account_id: []),
        (sheets, "update_row", lambda account, idx, **kw: calls.setdefault("update_row", []).append((idx, kw))),
        (gmail, "get_thread", fake_get_thread),
        (gmail, "get_own_addresses", lambda account: ({"me@me.com"}, False)),
        (gmail, "get_latest_reply_with_history", fake_get_reply),
        (gmail, "find_bounce", lambda account, t, e, thread=None: bounces_by_thread.get(t)),
        (reviews_db, "list_sent_bodies_for_thread", lambda account_id, thread_id: []),
        (suppressions_db, "add", lambda account_id, email, source="unsubscribe_link":
            calls.setdefault("suppressed", []).append((email, source))),
        (suppressions_db, "is_suppressed", lambda account_id, email: False),
        (plans, "check", lambda account, bucket: None),
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


def test_tracked_thread_is_checked_when_its_sheet_row_is_gone():
    calls = {"rows": []}
    tracked = [{
        "thread_id": "t1",
        "name": "John",
        "email": "john@x.com",
        "company": "Acme",
        "row_index": None,
    }]
    with contextlib.ExitStack() as stack:
        for target in _watch_env(calls, {"t1": ("still interested", [(False, "orig")])}):
            stack.enter_context(patched(*target))
        stack.enter_context(patched(
            watch_replies.tracked_threads, "list_active", lambda account_id: tracked,
        ))
        result = watch_replies.check_for_replies(ACCOUNT)
    assert len(result["reviews"]) == 1
    assert calls["thread_reads"] == ["t1"]
    assert "update_row" not in calls, "a deleted Sheet row must not block Gmail coverage"


def test_old_tracked_thread_cannot_mark_a_reused_sheet_row_replied():
    calls = {"rows": [
        (2, _row(status="Sent", thread="t-new", name="John", email="john@x.com")),
    ]}
    tracked = [{
        "thread_id": "t-old",
        "name": "John",
        "email": "john@x.com",
        "company": "Acme",
        "row_index": 2,
        "source": "sheet_backfill",
    }]
    with contextlib.ExitStack() as stack:
        for target in _watch_env(calls, {"t-old": ("an old reply", [(False, "old email")])}):
            stack.enter_context(patched(*target))
        stack.enter_context(patched(
            watch_replies.tracked_threads, "list_active", lambda account_id: tracked,
        ))
        result = watch_replies.check_for_replies(ACCOUNT)

    assert len(result["reviews"]) == 1, "the old Gmail reply remains visible in its own audit trail"
    assert "update_row" not in calls, "an old thread must not mutate a row now owned by a new thread"


def test_tracked_thread_works_for_an_account_without_a_sheet():
    calls = {"rows": []}
    tracked = [{
        "thread_id": "t1",
        "name": "John",
        "email": "john@x.com",
        "company": "Acme",
        "row_index": None,
    }]

    def sheet_must_not_be_read(account):
        raise AssertionError("a no-Sheet tracked account must not read Sheets")

    account = {**ACCOUNT, "google_sheet_id": ""}
    with contextlib.ExitStack() as stack:
        for target in _watch_env(calls, {"t1": ("still interested", [(False, "orig")])}):
            stack.enter_context(patched(*target))
        stack.enter_context(patched(sheets, "get_reply_check_rows", sheet_must_not_be_read))
        stack.enter_context(patched(
            watch_replies.tracked_threads, "list_active", lambda account_id: tracked,
        ))
        result = watch_replies.check_for_replies(account)
    assert len(result["reviews"]) == 1
    assert result["row_errors"] == []
    assert calls["thread_reads"] == ["t1"]


def test_reply_commitment_is_extracted_and_persisted_as_an_additive_signal():
    calls = {"rows": [(2, _row(status="Sent", thread="t1", name="John", email="john@x.com"))]}
    saved = []

    with contextlib.ExitStack() as stack:
        for target in _watch_env(calls, {"t1": ("I'll send the deck by Friday.", [])}):
            stack.enter_context(patched(*target))
        stack.enter_context(patched(
            commitments_db, "add_candidates",
            lambda account_id, thread_id, message_id, candidates:
                saved.extend(candidates) or [dict(candidates[0], id=11)],
        ))
        result = watch_replies.check_for_replies(ACCOUNT)

    assert len(result["commitments"]) == 1
    assert saved[0]["action_text"] == "send the deck by Friday"
    assert saved[0]["due_text"].lower() == "by friday"


def test_off_sheet_reply_commitment_is_stored_before_sender_confirmation():
    calls = {"rows": [(2, _row(status="Sent", thread="t1", name="John", email="john@x.com"))]}
    saved = []
    off_sheet = {
        "text": "I'll send the security docs by Friday.",
        "history": [],
        "message_id": "off-sheet-message",
        "kind": "off_sheet",
        "answered_elsewhere": False,
        "from_addr": "assistant@other.com",
    }

    with contextlib.ExitStack() as stack:
        for target in _watch_env(calls, {"t1": off_sheet}):
            stack.enter_context(patched(*target))
        stack.enter_context(patched(
            commitments_db, "add_candidates",
            lambda account_id, thread_id, message_id, candidates:
                saved.extend(candidates) or [dict(candidates[0], id=12)],
        ))
        result = watch_replies.check_for_replies(ACCOUNT)

    assert len(result["commitments"]) == 1
    assert saved[0]["action_text"] == "send the security docs by Friday"


def test_an_echo_of_our_own_message_never_cancels_a_pending_follow_up():
    """Regression: the echo check originally ran AFTER
    cancel_follow_up(follow_up, "reply"), so our own misclassified outgoing
    message killed a scheduled follow-up before anyone noticed it was never a
    customer reply. Echo detection must act before anything else does."""
    calls = {"rows": [(2, _row(status="Sent", thread="t1", name="John", email="john@x.com", body="orig"))]}
    follow_up = {"id": 77, "thread_id": "t1", "name": "John",
                 "email": "john@x.com", "company": "Acme"}
    acted_on = []

    def fake_mark_replied(account_id, draft_id):
        acted_on.append(("replied", draft_id))
        return True

    def fake_cancel(account_id, draft_id, reason):
        acted_on.append((reason, draft_id))
        return True

    def fake_dismiss_for_source(account_id, source_draft_id):
        acted_on.append(("dismissed", source_draft_id))

    with contextlib.ExitStack() as stack:
        for target in _watch_env(calls, {"t1": ("YES   let's  talk", [(False, "orig")])}):
            stack.enter_context(patched(*target))
        for target in [
            # What we sent on this thread differs only in case/whitespace --
            # exactly what the normalizer exists to see through.
            (reviews_db, "list_sent_bodies_for_thread",
             lambda account_id, t: ["yes let's TALK"]),
            (drafts_db, "list_active_follow_ups", lambda account_id: [follow_up]),
            (drafts_db, "list_follow_ups_needing_reconciliation", lambda account_id: []),
            (drafts_db, "mark_follow_up_replied", fake_mark_replied),
            (drafts_db, "cancel_follow_up", fake_cancel),
            (reviews_db, "dismiss_follow_up_for_source", fake_dismiss_for_source),
        ]:
            stack.enter_context(patched(*target))
        result = watch_replies.check_for_replies(ACCOUNT)

    assert result["reviews"] == [], "an echo must not be queued as a reply"
    assert acted_on == [], (
        "the pending follow-up must survive an echo untouched "
        f"(got {acted_on})"
    )


def test_stuck_reply_send_is_reconciled_from_gmail_sent_proof():
    """A reply claimed for sending whose request died before mark_sent ran
    must be closed by the worker when Gmail's Sent thread proves it left --
    otherwise a delivered reply is recorded nowhere."""
    stuck = [{"id": 9, "thread_id": "t1", "email": "j@x.com", "draft_reply": "on my way"}]
    marked = []

    with contextlib.ExitStack() as stack:
        for target in [
            (reviews_db, "list_reviews_stuck_in_send", lambda account_id: stuck),
            (gmail, "get_thread", lambda account, t: {"messages": [], "id": t}),
            (gmail, "find_sent_reply",
             lambda account, t, to, body, thread=None: {"id": "sent-1"}),
            (reviews_db, "mark_sent",
             lambda account_id, rid, sent_body: marked.append((rid, sent_body))),
            (reviews_db, "mark_stuck_send_visible",
             lambda account_id, rid: pytest.fail("proof means the row is closed, not surfaced")),
        ]:
            stack.enter_context(patched(*target))
        errors = []
        watch_replies._reconcile_review_sends(ACCOUNT, errors)

    assert errors == []
    assert marked == [(9, "on my way")], marked


def test_unprovable_stuck_reply_send_becomes_visible_without_losing_the_fence():
    """When Gmail offers no exact proof (operator edited before sending), the
    row must surface as send_uncertain -- visible and permanently
    non-sendable -- instead of staying invisible forever. Never a retry."""
    stuck = [{"id": 10, "thread_id": "t1", "email": "j@x.com", "draft_reply": ""}]
    flipped = []
    sent_calls = []

    with contextlib.ExitStack() as stack:
        for target in [
            (reviews_db, "list_reviews_stuck_in_send", lambda account_id: stuck),
            (gmail, "get_thread", lambda account, t: {"messages": [], "id": t}),
            (gmail, "find_sent_reply",
             lambda account, t, to, body, thread=None: None),
            (reviews_db, "mark_sent",
             lambda account_id, rid, sent_body: sent_calls.append(rid)),
            (reviews_db, "mark_stuck_send_visible",
             lambda account_id, rid: flipped.append(rid) or True),
        ]:
            stack.enter_context(patched(*target))
        errors = []
        watch_replies._reconcile_review_sends(ACCOUNT, errors)

    assert errors == []
    assert sent_calls == [], "no proof means never mark sent"
    assert flipped == [10], flipped


def test_commitment_extractor_stays_quiet_for_non_promises_and_resolves_dates():
    reference = datetime(2026, 8, 19, 14, 0, tzinfo=timezone.utc)  # Wednesday
    assert commitments.extract_commitments("Sounds good, let's talk soon.", reference) == []
    assert commitments.extract_commitments("we will have a", reference) == []
    assert commitments.extract_commitments("I'll send the", reference) == []
    result = commitments.extract_commitments("I'll send the deck by Friday.", reference)
    assert len(result) == 1
    assert result[0]["action_text"] == "send the deck by Friday"
    assert result[0]["due_at"].startswith("2026-08-21T09:00:00")
    multiple = commitments.extract_commitments(
        "I'll send the deck by Friday. I'll schedule the demo tomorrow.", reference,
    )
    assert [item["due_text"].lower() for item in multiple] == ["by friday", "tomorrow"]


def test_commitment_extractor_accepts_gmail_iso_timestamp():
    result = commitments.extract_commitments(
        "I'll send the deck by Friday.",
        "2026-08-19T14:00:00+00:00",
    )
    assert len(result) == 1
    assert result[0]["due_at"].startswith("2026-08-21T09:00:00")


def test_due_commitments_use_the_atomic_mark_and_alert_transition():
    due = [{"id": 17, "status": "scheduled", "action_text": "send the deck"}]
    marked = []
    errors = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(
            watch_replies.commitments_db,
            "list_due",
            lambda account_id: due,
        ))
        stack.enter_context(patched(
            watch_replies.commitments_db,
            "mark_due",
            lambda account_id, commitment_id: marked.append((account_id, commitment_id)) or True,
        ))
        result = watch_replies._process_due_commitments({"id": "a1"}, errors)
    assert errors == []
    assert marked == [("a1", 17)]
    assert result == [{"id": 17, "status": "due", "action_text": "send the deck"}]


class _CommitmentsTable:
    def __init__(self, rows):
        self.rows = rows
        self.events = []
        self._table = "commitments"
        self._eq = []
        self._in = []
        self._lte = []
        self._values = None
        self._mode = None

    def table(self, name):
        self._table = name
        return self
    def rpc(self, name, payload):
        self._mode = "rpc"
        self._values = (name, payload)
        return self
    def select(self, cols):
        self._mode = "select"
        return self
    def update(self, values):
        self._mode = "update"
        self._values = values
        return self
    def eq(self, col, val):
        self._eq.append((col, val))
        return self
    def in_(self, col, values):
        self._in.append((col, values))
        return self
    def lte(self, col, value):
        self._lte.append((col, value))
        return self
    def order(self, *args, **kwargs): return self
    def limit(self, *args, **kwargs): return self
    def execute(self):
        if self._mode == "rpc":
            name, payload = self._values
            if name == "mark_commitment_due_with_alert":
                row = next((item for item in self.rows if item.get("account_id") == payload["p_account_id"] and item.get("id") == payload["p_commitment_id"]), None)
                if row and row.get("status") == "scheduled":
                    row["status"] = "due"
                    return type("R", (), {"data": True})()
                return type("R", (), {"data": False})()
            assert name == "transition_commitment_with_event"
            row = next((item for item in self.rows if item.get("account_id") == payload["p_account_id"] and item.get("id") == payload["p_commitment_id"]), None)
            if not row:
                return type("R", (), {"data": None})()
            key = payload["p_transition_key"]
            if not any(event["transition_key"] == key for event in self.events):
                previous = row.get("status")
                row["status"] = payload["p_to_status"]
                if payload.get("p_due_at") is not None:
                    row["due_at"] = payload["p_due_at"]
                row["reminder_at"] = payload.get("p_reminder_at") or row.get("reminder_at") or row.get("due_at")
                row["timezone"] = payload.get("p_timezone") or row.get("timezone") or "UTC"
                if payload["p_to_status"] == "completed":
                    row["completed_at"] = row.get("completed_at") or "now"
                if payload["p_to_status"] == "dismissed":
                    row["dismissal_reason"] = payload.get("p_dismissal_reason")
                self.events.append({"transition_key": key, "from_status": previous, "to_status": row["status"]})
            self._eq, self._in, self._lte, self._values, self._mode = [], [], [], None, None
            return type("R", (), {"data": dict(row)})()
        source = self.events if self._table == "commitment_events" else self.rows
        matched = [
            row for row in source
            if all(row.get(col) == value for col, value in self._eq)
            and all(row.get(col) in values for col, values in self._in)
            and all(row.get(col) is not None and row.get(col) <= value for col, value in self._lte)
        ]
        if self._mode == "update":
            for row in matched:
                row.update(self._values)
        self._eq, self._in, self._lte, self._values, self._mode = [], [], [], None, None
        return type("R", (), {"data": matched})()


def test_commitment_confirmation_does_not_erase_detected_due_date():
    due = "2026-08-21T09:00:00+00:00"
    fake = _CommitmentsTable([{
        "id": 4, "account_id": "a1", "status": "detected", "due_at": due,
        "reminder_at": None,
    }])
    with patched(commitments_db, "_get_client", lambda: fake):
        confirmed = commitments_db.confirm("a1", 4)
    assert confirmed["status"] == "scheduled"
    assert confirmed["due_at"] == due
    assert fake.rows[0]["reminder_at"] == due
    assert fake.events == [{"transition_key": "schedule:4", "from_status": "detected", "to_status": "scheduled"}]


def test_commitment_confirmation_records_timezone_and_reminder():
    fake = _CommitmentsTable([{
        "id": 5, "account_id": "a1", "status": "detected", "due_at": None,
        "reminder_at": None,
    }])
    with patched(commitments_db, "_get_client", lambda: fake):
        confirmed = commitments_db.confirm(
            "a1", 5, due_at="2026-09-10T09:00:00+00:00",
            reminder_at="2026-09-09T09:00:00+00:00", timezone_name="Asia/Bangkok",
        )
    assert confirmed["status"] == "scheduled"
    assert confirmed["reminder_at"] == "2026-09-09T09:00:00+00:00"
    assert confirmed["timezone"] == "Asia/Bangkok"


def test_commitment_completion_is_idempotent_and_keeps_the_graph_row():
    fake = _CommitmentsTable([{
        "id": 6, "account_id": "a1", "status": "due", "completed_at": None,
    }])
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(commitments_db, "_get_client", lambda: fake))
        stack.enter_context(patched(commitments_db, "enabled", lambda: True))
        first = commitments_db.complete("a1", 6)
        second = commitments_db.complete("a1", 6)
    assert first["status"] == "completed"
    assert second["status"] == "completed"
    assert fake.rows[0]["status"] == "completed"


def test_commitment_summary_separates_open_value_from_completed_value():
    fake = _CommitmentsTable([
        {"id": 7, "account_id": "a1", "status": "scheduled", "estimated_value": 14000},
        {"id": 8, "account_id": "a1", "status": "due", "estimated_value": None},
        {"id": 9, "account_id": "a1", "status": "completed", "estimated_value": 9000},
    ])
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(commitments_db, "_get_client", lambda: fake))
        stack.enter_context(patched(commitments_db, "enabled", lambda: True))
        result = commitments_db.summary("a1")
    assert result["open_count"] == 2
    assert result["overdue_count"] == 1
    assert result["valued_count"] == 1
    assert result["estimated_value"] == 14000.0
    assert result["value_provenance"] == "user_entered"
    assert result["completed_count"] == 1
    assert result["reviewed_count"] == 3
    assert result["dismissed_count"] == 0
    assert result["precision_status"] == "healthy"


def test_commitment_precision_thresholds_make_false_positives_the_gate():
    def status_for(dismissed, confirmed):
        rows = [
            {
                "id": i, "account_id": "a1", "status": "dismissed",
                "dismissal_reason": "incorrect", "estimated_value": None,
            }
            for i in range(dismissed)
        ] + [
            {"id": 100 + i, "account_id": "a1", "status": "completed", "estimated_value": None}
            for i in range(confirmed)
        ]
        fake = _CommitmentsTable(rows)
        with patched(commitments_db, "_get_client", lambda: fake), \
             patched(commitments_db, "enabled", lambda: True):
            return commitments_db.summary("a1")

    assert status_for(0, 10)["precision_status"] == "healthy"
    assert status_for(9, 91)["precision_status"] == "healthy"
    assert status_for(1, 9)["precision_status"] == "dangerous"
    assert status_for(2, 8)["precision_status"] == "dangerous"
    assert status_for(20, 80)["precision_status"] == "dangerous"
    assert status_for(21, 79)["precision_status"] == "stop"
    stopped = status_for(3, 7)
    assert stopped["precision_status"] == "stop"
    assert stopped["stop_onboarding"] is True


def test_commitment_precision_counts_only_explicit_incorrect_dismissals():
    fake = _CommitmentsTable([
        {
            "id": 1, "account_id": "a1", "status": "dismissed",
            "dismissal_reason": "incorrect", "estimated_value": None,
        },
        {
            "id": 2, "account_id": "a1", "status": "dismissed",
            "dismissal_reason": "not_applicable", "estimated_value": None,
        },
        {"id": 3, "account_id": "a1", "status": "completed", "estimated_value": None},
    ])
    with patched(commitments_db, "_get_client", lambda: fake), \
         patched(commitments_db, "enabled", lambda: True):
        result = commitments_db.summary("a1")
    assert result["reviewed_count"] == 3
    assert result["dismissed_count"] == 1
    assert result["false_positive_rate"] == pytest.approx(1 / 3)
    assert result["stop_onboarding"] is True


def test_global_promise_precision_does_not_gate_on_three_samples():
    fake = _CommitmentsTable([
        {
            "id": 1, "account_id": "a1", "status": "dismissed",
            "dismissal_reason": "incorrect",
        },
        {"id": 2, "account_id": "a2", "status": "completed"},
        {"id": 3, "account_id": "a3", "status": "completed"},
    ])
    with patched(commitments_db, "_get_client", lambda: fake), \
         patched(commitments_db, "enabled", lambda: True):
        result = commitments_db.onboarding_precision()
    assert result["reviewed_count"] == 3
    assert result["false_positive_rate"] == pytest.approx(1 / 3)
    assert result["precision_status"] == "insufficient_data"
    assert result["stop_onboarding"] is False


def test_commitment_queue_excludes_scheduled_and_lists_only_due_schedules():
    now = "2026-08-21T09:00:00+00:00"
    fake = _CommitmentsTable([
        {"id": 1, "account_id": "a1", "status": "detected", "reminder_at": None},
        {"id": 2, "account_id": "a1", "status": "scheduled", "reminder_at": "2026-08-20T09:00:00+00:00"},
        {"id": 3, "account_id": "a1", "status": "scheduled", "reminder_at": "2026-08-22T09:00:00+00:00"},
        {"id": 4, "account_id": "a1", "status": "due", "reminder_at": "2026-08-19T09:00:00+00:00"},
    ])
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(commitments_db, "_get_client", lambda: fake))
        stack.enter_context(patched(commitments_db, "enabled", lambda: True))
        active = commitments_db.list_active("a1")
        due = commitments_db.list_due("a1", now=now)
    assert [row["id"] for row in active] == [1, 4]
    assert [row["id"] for row in due] == [2]


def test_the_quoted_chain_is_trimmed_before_storage_but_not_before_dedupe():
    """Two things at once, because they pull in opposite directions.

    Stored: trimmed. A reply on a long thread quotes the whole conversation
    back, and an operator scanning a queue at a few seconds per item cannot find
    the two new sentences inside 20KB of '>' lines.

    Dedupe: raw. The body is only consulted as a legacy fallback for rows
    written before gmail_message_id existed, and those rows hold the untrimmed
    text. Passing the trimmed version would miss them and queue a duplicate
    review for a reply already handled -- which on a fast queue is a duplicate
    reply to a prospect."""
    raw = "Yes, let's talk.\n\nOn Mon, Jan 1, John wrote:\n> our original outreach\n> more quoted text"
    calls = {"rows": [(2, _row(status="Sent", thread="t1", name="John", email="john@x.com"))]}
    seen = []

    targets = _watch_env(calls, {"t1": (raw, [])})
    targets = [
        (reviews_db, "find_review_id",
            lambda account_id, t, mid, reply=None: seen.append(reply) or None)
        if tgt[0] is reviews_db and tgt[1] == "find_review_id" else tgt
        for tgt in targets
    ]
    with contextlib.ExitStack() as stack:
        for target in targets:
            stack.enter_context(patched(*target))
        watch_replies.check_for_replies(ACCOUNT)

    stored = calls["add_review"][0][1]
    assert stored == "Yes, let's talk.", f"quoted tail must not reach the queue: {stored!r}"
    assert seen == [raw], (
        "the legacy dedupe fallback must see the untrimmed body, or rows stored "
        "before the message-id column can never be matched again"
    )


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


def test_check_for_replies_off_sheet_reply_is_flagged_without_drafting():
    """The least-trusted input this system sees does not reach the LLM before
    a human confirms the sender -- drafting is deferred to the confirm-sender
    endpoint (server.py), not attempted here."""
    rows = [(2, _row(status="Sent", thread="t1", name="John", email="john@x.com"))]
    reply_map = {"t1": {
        "text": "on behalf of john", "history": [], "message_id": "m-1",
        "kind": "off_sheet", "answered_elsewhere": False, "from_addr": "assistant@other.com",
    }}
    calls = {"rows": rows}
    with contextlib.ExitStack() as stack:
        for target in _watch_env(calls, reply_map):
            stack.enter_context(patched(*target))
        result = watch_replies.check_for_replies(ACCOUNT)
    assert len(result["reviews"]) == 1
    assert calls["add_review"][0] == (
        "t1", "on behalf of john", "", "flagged", False, "assistant@other.com"
    ), "flagged review stores the OBSERVED sender, not the sheet contact's address"
    assert "draft" not in calls, "an off-sheet reply must not be drafted at detection time"


def test_check_for_replies_answered_elsewhere_has_no_draft():
    rows = [(2, _row(status="Sent", thread="t1", name="John", email="john@x.com"))]
    reply_map = {"t1": {
        "text": "sure thing", "history": [], "message_id": "m-1",
        "kind": "on_sheet", "answered_elsewhere": True, "from_addr": "john@x.com",
    }}
    calls = {"rows": rows}
    with contextlib.ExitStack() as stack:
        for target in _watch_env(calls, reply_map):
            stack.enter_context(patched(*target))
        result = watch_replies.check_for_replies(ACCOUNT)
    assert len(result["reviews"]) == 1
    assert calls["add_review"][0] == ("t1", "sure thing", "", "answered_elsewhere", False, "john@x.com")
    assert "draft" not in calls, "nothing to send, so nothing worth drafting"


def test_check_for_replies_degraded_lookup_marks_the_review():
    rows = [(2, _row(status="Sent", thread="t1", name="John", email="john@x.com"))]
    reply_map = {"t1": ("yes let's talk", [(False, "orig")])}
    calls = {"rows": rows}
    targets = [
        (gmail, "get_own_addresses", lambda account: ({"me@me.com"}, True))
        if tgt[0] is gmail and tgt[1] == "get_own_addresses" else tgt
        for tgt in _watch_env(calls, reply_map)
    ]
    with contextlib.ExitStack() as stack:
        for target in targets:
            stack.enter_context(patched(*target))
        watch_replies.check_for_replies(ACCOUNT)
    assert calls["add_review"][0][4] is True, "a degraded own-addresses lookup must mark the review it produced"


def test_check_for_replies_quota_exceeded_surfaces_the_rich_message_verbatim():
    """QuotaExceeded.__str__ is a plan-specific upgrade-path message -- plans.py
    calls a quota refusal a sales moment. It must reach the row_error exactly,
    not get folded into the generic 'drafting a response failed' wrapper text
    used for every other drafting failure."""
    rows = [(2, _row(status="Sent", thread="t1", name="John", email="john@x.com"))]
    reply_map = {"t1": ("yes let's talk", [(False, "orig")])}
    calls = {"rows": rows}
    exc = plans.QuotaExceeded("trial", usage.UNIT_DRAFT_REPLY, 10, 10, "Your Trial plan includes 10 reply drafts.")

    def boom(account, bucket):
        raise exc

    targets = [
        (plans, "check", boom) if tgt[0] is plans and tgt[1] == "check" else tgt
        for tgt in _watch_env(calls, reply_map)
    ]
    with contextlib.ExitStack() as stack:
        for target in targets:
            stack.enter_context(patched(*target))
        result = watch_replies.check_for_replies(ACCOUNT)
    assert result["row_errors"] == [f"John: {exc}"], result["row_errors"]
    assert "draft" not in calls, "quota was checked before drafting was attempted"


# ------------------------------------------ check_for_replies: real classifier

def test_check_for_replies_off_sheet_reply_through_the_real_classifier():
    """Every other test in this file either exercises gmail.py's classifier
    directly, or exercises check_for_replies against fake_get_reply -- never
    both at once. If the real function's `kind` values ever drifted from what
    the fake assumes, every test on both sides would still pass while
    production silently took the wrong branch. This is the one test that
    connects them: only the Gmail API boundary is faked."""
    thread = {"messages": [
        msg("me@me.com", "our outreach"),
        msg("assistant@other.com", "he's traveling, I can help"),
    ]}
    rows = [(2, _row(status="Sent", thread="t1", name="John", email="john@x.com"))]
    calls = {"rows": rows}
    with contextlib.ExitStack() as stack:
        for target in _watch_env(calls, {}):
            # Keep every DB/sheets/agent fake, but let the real gmail
            # classification run -- only its own API boundary is faked.
            if target[0] is gmail and target[1] in (
                "get_latest_reply_with_history", "get_own_addresses", "get_thread"
            ):
                continue
            stack.enter_context(patched(*target))
        stack.enter_context(patched(gmail, "_get_service", lambda account: fake_gmail_service(thread["messages"])))
        result = watch_replies.check_for_replies(ACCOUNT)
    assert len(result["reviews"]) == 1
    assert calls["add_review"][0][3] == "flagged", (
        "the real classifier must produce the same status the fake-based "
        "tests assume -- a stray naming mismatch here would pass on both "
        "sides of the mock boundary and only break in production"
    )
    assert calls["add_review"][0][5] == "assistant@other.com"


# ------------------------------------------------- watch_replies: run_all_accounts

def _acct(id, token="tok", sheet="sheet-1"):
    return {"id": id, "email": f"{id}@x.com", "google_sheet_id": sheet, "google_token": token}


def test_run_all_accounts_processes_every_account_and_isolates_failures():
    """One account raising must not stop the next one -- the whole point of
    this phase -- and each account's check must see its own id as the
    current usage-attribution account, not the previous one's or none at
    all (usage.run_as is what makes this true; this proves it end to end
    rather than trusting the call site alone)."""
    accounts = [_acct("a1"), _acct("a2"), _acct("a3")]
    seen = []

    def fake_check(account):
        seen.append((account["id"], usage._current_account_id.get()))
        if account["id"] == "a2":
            raise RuntimeError("thread fetch failed")
        return {"reviews": [1], "row_errors": [], "bounces": []}

    heartbeats = []
    sleeps = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(accounts_db, "list_accounts_for_worker", lambda: accounts))
        stack.enter_context(patched(watch_replies, "check_for_replies", fake_check))
        stack.enter_context(patched(accounts_db, "set_worker_heartbeat",
            lambda aid, error=None: heartbeats.append((aid, error))))
        stack.enter_context(patched(watch_replies.time, "sleep", lambda s: sleeps.append(s)))
        result = watch_replies.run_all_accounts()

    assert result["checked"] == 2 and result["failed"] == 1 and result["skipped_no_token"] == 0
    assert seen == [("a1", "a1"), ("a2", "a2"), ("a3", "a3")]
    assert heartbeats == [("a1", None), ("a2", "thread fetch failed"), ("a3", None)]
    assert sleeps == [
        watch_replies.INTER_ACCOUNT_DELAY_SECONDS, watch_replies.INTER_ACCOUNT_DELAY_SECONDS,
    ], "a gap between accounts, none after the last one"


def test_run_all_accounts_skips_gmail_work_for_a_dead_token_but_keeps_heartbeat_fresh():
    """Regression test for the finding that shaped this design: a missing
    token skips the Gmail work for this cycle, but the account still gets a
    fresh heartbeat with a specific, actionable last_error -- never treated
    as a check failure, and never silently dropped from the summary."""
    accounts = [_acct("a1", token=None)]
    checked = []
    heartbeats = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(accounts_db, "list_accounts_for_worker", lambda: accounts))
        stack.enter_context(patched(watch_replies, "check_for_replies",
            lambda a: checked.append(a) or {"reviews": [], "row_errors": [], "bounces": []}))
        stack.enter_context(patched(accounts_db, "set_worker_heartbeat",
            lambda aid, error=None: heartbeats.append((aid, error))))
        result = watch_replies.run_all_accounts()
    assert checked == [], "no token means no Gmail work at all"
    assert result["checked"] == 0 and result["skipped_no_token"] == 1 and result["failed"] == 0
    assert heartbeats == [("a1", "Google disconnected — reconnect in Settings")]


def test_run_all_accounts_debug_mode_never_writes_a_heartbeat():
    """main()'s single-account debug path passes write_heartbeat=False --
    a one-off manual run must not forge the "the continuous worker is alive"
    signal for the account it happens to be pointed at."""
    accounts = [_acct("a1")]
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(watch_replies, "check_for_replies",
            lambda a: {"reviews": [], "row_errors": [], "bounces": []}))
        stack.enter_context(patched(accounts_db, "set_worker_heartbeat",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not write a heartbeat in debug mode"))))
        watch_replies.run_all_accounts(accounts=accounts, write_heartbeat=False)


def test_run_all_accounts_survives_an_account_listing_failure():
    def boom():
        raise RuntimeError("supabase down")
    with patched(accounts_db, "list_accounts_for_worker", boom):
        result = watch_replies.run_all_accounts()
    assert result == {"checked": 0, "skipped_no_token": 0, "failed": 0, "elapsed": 0.0}


def test_run_all_accounts_warns_when_a_cycle_runs_long():
    """schedule doesn't overlap runs, it just starts the next one late -- so
    a cycle that's already slower than the interval has to say so, or
    detection latency grows with the only symptom being a customer noticing
    replies take longer to show up."""
    accounts = [_acct("a1")]
    buf = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(accounts_db, "list_accounts_for_worker", lambda: accounts))
        stack.enter_context(patched(watch_replies, "check_for_replies",
            lambda a: {"reviews": [], "row_errors": [], "bounces": []}))
        stack.enter_context(patched(accounts_db, "set_worker_heartbeat", lambda *a, **k: None))
        stack.enter_context(patched(watch_replies, "CHECK_INTERVAL_MINUTES", 0))
        stack.enter_context(contextlib.redirect_stdout(buf))
        watch_replies.run_all_accounts()
    assert "WARNING" in buf.getvalue() and "longer than the" in buf.getvalue()


def test_run_all_accounts_records_a_durable_cycle_and_account_event():
    accounts = [_acct("a1")]
    events = []
    finished = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(watch_replies.worker_db, "start_run", lambda: 42))
        stack.enter_context(patched(
            watch_replies.worker_db, "record_event",
            lambda *args: events.append(args),
        ))
        stack.enter_context(patched(
            watch_replies.worker_db, "finish_run",
            lambda *args: finished.append(args),
        ))
        stack.enter_context(patched(accounts_db, "list_accounts_for_worker", lambda: accounts))
        stack.enter_context(patched(
            watch_replies, "check_for_replies",
            lambda a: {"reviews": [1], "row_errors": [], "bounces": []},
        ))
        stack.enter_context(patched(accounts_db, "set_worker_heartbeat", lambda *a, **k: None))
        result = watch_replies.run_all_accounts()

    assert events == [(42, "a1", "account_check", "success", {
        "reviews": 1, "row_errors": 0, "bounces": 0,
    })]
    assert finished and finished[0][0] == 42
    assert finished[0][2] == "success"
    assert finished[0][1]["checked"] == 1
    assert finished[0][1]["degraded"] == 0
    assert finished[0][1]["degraded_reasons"] == {}
    assert result["failed"] == 0


def test_run_all_accounts_persists_bounded_degraded_reason_and_raw_event_detail():
    accounts = [_acct("a1")]
    events = []
    finished = []
    raw_error = "reviews insert failed: constraint 23502"
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(watch_replies.worker_db, "start_run", lambda: 43))
        stack.enter_context(patched(
            watch_replies.worker_db, "record_event",
            lambda *args: events.append(args),
        ))
        stack.enter_context(patched(
            watch_replies.worker_db, "finish_run",
            lambda *args: finished.append(args),
        ))
        stack.enter_context(patched(accounts_db, "list_accounts_for_worker", lambda: accounts))
        stack.enter_context(patched(
            watch_replies, "check_for_replies",
            lambda a: {"reviews": [], "row_errors": [raw_error], "bounces": []},
        ))
        stack.enter_context(patched(accounts_db, "set_worker_heartbeat", lambda *a, **k: None))
        stack.enter_context(patched(watch_replies.notifications, "account_error", lambda *a: None))
        result = watch_replies.run_all_accounts()

    assert result["degraded"] == 1
    assert result["degraded_reasons"] == {"row_errors": 1}
    assert finished[0][2] == "degraded"
    assert finished[0][1]["degraded_reasons"] == {"row_errors": 1}
    assert events[0][4]["error"] == raw_error
    assert raw_error not in json.dumps(result)


def test_worker_event_writer_upserts_a_stable_run_scoped_dedupe_key():
    """The unique indexes only protect writes if the writer supplies the key.

    A retry of the same account check must update one durable event rather than
    creating an unbounded stream of indistinguishable rows.
    """
    calls = []

    class Table:
        def upsert(self, payload, **kwargs):
            calls.append((payload, kwargs))
            return self

        def execute(self):
            return type("R", (), {"data": []})()

    class Client:
        def table(self, name):
            assert name == worker_db.EVENTS_TABLE
            return Table()

    with patched(worker_db, "enabled", lambda: True), \
         patched(worker_db, "_get_client", lambda: Client()):
        worker_db.record_event(42, "a1", "account_check", "success", {"reviews": 1})

    assert calls == [(
        {
            "run_id": 42,
            "account_id": "a1",
            "event_type": "account_check",
            "status": "success",
            "details": {"reviews": 1},
            "dedupe_key": "42:a1:account_check",
        },
        {"on_conflict": "run_id,event_type,dedupe_key"},
    )]


def test_worker_event_writer_uses_account_scope_for_events_without_a_run():
    calls = []

    class Table:
        def upsert(self, payload, **kwargs):
            calls.append((payload, kwargs))
            return self

        def execute(self):
            return type("R", (), {"data": []})()

    class Client:
        def table(self, name):
            return Table()

    with patched(worker_db, "enabled", lambda: True), \
         patched(worker_db, "_get_client", lambda: Client()):
        worker_db.record_event(None, "a1", "cycle_error", "failed")

    assert calls[0][0]["dedupe_key"] == "adhoc:a1:cycle_error"
    assert calls[0][1]["on_conflict"] == "account_id,event_type,dedupe_key"


def test_legacy_direct_notification_entry_points_are_disabled():
    account = {"email": "owner@example.com", "last_error": "Gmail is down"}
    assert notify.account_error(account, "Google disconnected — reconnect in Settings") is False
    assert notify.account_recovered(account) is False
    assert notify.new_reply(account, {"id": 1}) is False


def test_reconnect_alert_copy_contains_no_account_or_error_detail():
    subject, body, path = notify._message({
        "event_type": "gmail_disconnected", "source_id": "account-1",
    })
    assert subject == "Sendkeep needs your attention"
    assert "owner@example.com" not in body
    assert "traceback" not in body.lower()
    assert path == "/settings"


def test_worker_health_endpoint_requires_a_token_and_reports_snapshot():
    import server

    class Request:
        def __init__(self, headers):
            self.headers = headers

    previous = os.environ.get("WORKER_HEALTH_TOKEN")
    try:
        os.environ.pop("WORKER_HEALTH_TOKEN", None)
        not_configured = server.worker_health(Request({}))
        assert not_configured.status_code == 503

        os.environ["WORKER_HEALTH_TOKEN"] = "health-secret"
        unauthorized = server.worker_health(Request({"x-worker-health-token": "wrong"}))
        assert unauthorized.status_code == 401
        with patched(server.worker_db, "health_snapshot", lambda: {
            "ok": True, "status": "healthy", "age_seconds": 12,
        }):
            healthy = server.worker_health(Request({"x-worker-health-token": "health-secret"}))
        assert healthy.status_code == 200
    finally:
        if previous is None:
            os.environ.pop("WORKER_HEALTH_TOKEN", None)
        else:
            os.environ["WORKER_HEALTH_TOKEN"] = previous


def test_worker_health_marks_a_live_but_degraded_cycle_unhealthy():
    now = datetime.now(timezone.utc).isoformat()
    with patched(worker_db, "latest_run", lambda: {
        "status": "degraded", "started_at": now, "finished_at": now,
        "checked_accounts": 1, "skipped_accounts": 1, "failed_accounts": 0,
        "degraded_accounts": 1, "degraded_reasons": {"google_disconnected": 1},
    }):
        snapshot = worker_db.health_snapshot(stale_after_seconds=900)
    assert snapshot["status"] == "degraded"
    assert snapshot["ok"] is False
    assert snapshot["degraded_accounts"] == 1
    assert snapshot["degraded_reasons"] == {"google_disconnected": 1}


def test_commitments_endpoint_adds_contact_context_from_tracked_threads():
    import server

    class Request:
        def __init__(self):
            self.state = type("State", (), {"account_id": "a1"})()

    commitment = {"id": 3, "thread_id": "t1", "action_text": "send deck"}
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server.commitments_db, "list_active", lambda aid: [commitment]))
        stack.enter_context(patched(
            server.tracked_threads, "list_active",
            lambda aid: [{"thread_id": "t1", "name": "John", "email": "john@x.com"}],
        ))
        result = server.list_commitments(Request())
    assert result[0]["contact_name"] == "John"
    assert result[0]["contact_email"] == "john@x.com"


def test_completed_commitment_endpoint_returns_contact_context_for_outcome_nudge():
    import server

    class Request:
        def __init__(self):
            self.state = type("State", (), {"account_id": "a1"})()

    completed = {"id": 4, "thread_id": "t1", "status": "completed", "estimated_value": 14000}
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server.commitments_db, "complete", lambda aid, cid: completed))
        stack.enter_context(patched(
            server.tracked_threads, "list_active",
            lambda aid: [{"thread_id": "t1", "name": "John", "email": "john@x.com"}],
        ))
        result = server.complete_commitment(Request(), 4)
    assert result["contact_name"] == "John"
    assert result["contact_email"] == "john@x.com"


def test_commitment_dismissal_records_extraction_errors_separately_from_due_cleanup():
    import server

    class Request:
        state = type("State", (), {"account_id": "a1"})()

    reasons = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(
            server.commitments_db, "get",
            lambda account_id, commitment_id: {
                "id": commitment_id,
                "status": "detected" if commitment_id == 1 else "due",
            },
        ))
        stack.enter_context(patched(
            server.commitments_db, "dismiss",
            lambda account_id, commitment_id, reason: reasons.append(reason) or True,
        ))
        assert server.dismiss_commitment(Request(), 1)["reason"] == "incorrect"
        assert server.dismiss_commitment(Request(), 2)["reason"] == "not_applicable"
    assert reasons == ["incorrect", "not_applicable"]


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
    assert result["draft_ids"] == [1]
    assert result["failed"] == [{"email": "f@x.com", "error": "LLM exploded"}]
    assert calls["sends"] == 0, "prepare must NOT send any email"
    assert calls["updates"] == 0, "prepare must NOT write the sheet"
    # add_draft(account_id, row_index, name, email, company, subject, body)
    assert len(calls["drafts"]) == 1
    a = calls["drafts"][0]
    assert a[1] == 2 and a[3] == "j@x.com" and a[5] == "Subject line"
    assert "Prepared 1 of 2" in calls["notify"][1]


def test_auto_send_preparation_notification_does_not_claim_manual_review():
    rows = [(2, _row())]
    calls = {}
    with contextlib.ExitStack() as stack:
        # This test exercises the separately gated controlled deployment path;
        # make that prerequisite explicit instead of inheriting a developer's
        # .env value during pytest collection.
        stack.enter_context(patched(config, "AUTO_SEND_ENABLED", True))
        stack.enter_context(patched(sheets, "require_full_header", lambda account: None))
        stack.enter_context(patched(send_outreach.bounces, "assert_sendable", lambda account: None))
        stack.enter_context(patched(suppressions_db, "list_suppressed_emails", lambda aid: set()))
        stack.enter_context(patched(drafts_db, "count_sent_last_24_hours", lambda aid: 0))
        stack.enter_context(patched(sheets, "campaign_readiness", lambda account, **kw: _readiness(rows)))
        stack.enter_context(patched(sheets, "get_all_rows", lambda account: []))
        stack.enter_context(patched(drafts_db, "has_pending_for_row", lambda aid, idx: False))
        stack.enter_context(patched(drafts_db, "add_draft", lambda *args: 7))
        stack.enter_context(patched(agent, "generate_outreach_email", lambda *args, **kwargs: ("Subject", "Body")))
        stack.enter_context(patched(send_outreach, "notify", lambda account, subject, body: calls.update(subject=subject, body=body)))
        send_outreach.prepare_drafts(ACCOUNT, auto_send=True)
    assert calls["subject"] == "Outreach drafts are sending automatically"
    assert "sending automatically" in calls["body"]


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
    assert result == {"prepared": 0, "total": 0, "failed": [], "daily_limit": 25, "remaining_today": 25, "draft_ids": []}


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
    stack.enter_context(patched(bounces, "assert_sendable", lambda account: None))
    stack.enter_context(patched(send_outreach, "assert_domain_safe", lambda account: None))
    stack.enter_context(patched(
        sheets, "get_all_rows",
        lambda account: [(2, _row(status="", email="j@x.com"))],
    ))
    stack.enter_context(patched(suppressions_db, "is_suppressed", lambda aid, email: False))
    stack.enter_context(patched(drafts_db, "has_first_touch_conflict", lambda *a, **k: False))
    stack.enter_context(patched(tracked_threads, "upsert_thread", lambda *a, **k: None))
    stack.enter_context(patched(auth, "unsubscribe_url", lambda aid, e: "https://u/unsub?t=x.y"))

    def fake_send(account, to, s, b, unsubscribe_url="", message_id=""):
        order.append(("gmail", {"to": to, "body": b, "header": unsubscribe_url}))
        return thread_id

    def fake_mark_sent(aid, did, subject, body, **kwargs):
        order.append(("queue", {"draft_id": did, "subject": subject, "body": body}))
        return True

    def fake_update_row(account, idx, **kw):
        order.append(("sheet", {"row": idx, **kw}))
        if sheet_raises:
            raise sheet_raises

    stack.enter_context(patched(gmail, "send_email", fake_send))
    stack.enter_context(patched(drafts_db, "claim_send", lambda *args: True))
    stack.enter_context(patched(drafts_db, "release_send_claim", lambda *args: True))
    stack.enter_context(patched(drafts_db, "mark_send_uncertain", lambda *args: True))
    stack.enter_context(patched(drafts_db, "mark_sent", fake_mark_sent))
    stack.enter_context(patched(send_outreach.send_capacity_db, "reserve", lambda *args: True))
    stack.enter_context(patched(send_outreach.send_capacity_db, "complete", lambda *args: True))
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
            assert drafts_db.schema_contract.BASELINE_MIGRATION in str(e)
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


def test_a_send_refuses_when_any_authoritative_send_column_is_missing():
    """Regression: production had sent_at but lacked follow_up_cancel_reason.

    The old probe selected only sent_at, so it passed, Gmail accepted the
    message, and mark_sent then failed on the wider update. The preflight must
    exercise every column written by mark_sent before Gmail can be called.
    """
    sent = []

    class _PartiallyMigrated:
        def table(self, name): return self
        def select(self, columns):
            if "follow_up_cancel_reason" in columns:
                raise RuntimeError("column outreach_drafts.follow_up_cancel_reason does not exist")
            return self
        def limit(self, n): return self
        def execute(self): return type("R", (), {"data": []})()

    with patched(drafts_db, "_send_log_verified", False), \
         patched(drafts_db, "_get_client", lambda: _PartiallyMigrated()), \
         patched(gmail, "send_email", lambda *a, **k: sent.append(a) or "t"):
        try:
            send_outreach.send_prepared_draft(ACCOUNT, dict(_DRAFT_TO_SEND))
            assert False, "must refuse before sending"
        except RuntimeError as e:
            assert "follow-up workflow" in str(e)
            assert drafts_db.schema_contract.BASELINE_MIGRATION in str(e)
    assert sent == [], "no email may leave when mark_sent cannot complete"


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
    sent, discarded = [], []
    rows = [
        (2, _row(email="a@x.com")),
        (3, _row(email="b@x.com")),
        (4, _row(email="c@x.com")),
    ]
    reservations = iter((True, False))
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(send_outreach, "_SEND_ALL_MIN_GAP", 0))
        stack.enter_context(patched(send_outreach, "_SEND_ALL_MAX_GAP", 0))
        stack.enter_context(patched(drafts_db, "list_pending_drafts", lambda aid: drafts))
        stack.enter_context(patched(drafts_db, "require_send_log", lambda: None))
        stack.enter_context(patched(bounces, "assert_sendable", lambda account: None))
        stack.enter_context(patched(send_outreach, "assert_domain_safe", lambda account: None))
        stack.enter_context(patched(sheets, "get_all_rows", lambda account: rows))
        stack.enter_context(patched(suppressions_db, "is_suppressed", lambda aid, email: email == "a@x.com"))
        stack.enter_context(patched(drafts_db, "discard", lambda aid, did: discarded.append(did)))
        stack.enter_context(patched(drafts_db, "has_first_touch_conflict", lambda *a, **k: False))
        stack.enter_context(patched(drafts_db, "claim_send", lambda *a, **k: True))
        stack.enter_context(patched(drafts_db, "release_send_claim", lambda *a, **k: True))
        stack.enter_context(patched(send_outreach.send_capacity_db, "reserve", lambda *a, **k: next(reservations)))
        stack.enter_context(patched(send_outreach.send_capacity_db, "complete", lambda *a, **k: True))
        stack.enter_context(patched(auth, "unsubscribe_url", lambda *a: "https://u/x"))
        stack.enter_context(patched(gmail, "send_email", lambda account, email, *a, **k: sent.append(email) or "t"))
        stack.enter_context(patched(drafts_db, "mark_sent", lambda *a, **k: True))
        stack.enter_context(patched(tracked_threads, "upsert_thread", lambda *a, **k: None))
        stack.enter_context(patched(send_outreach, "mark_row_sent", lambda *a, **k: None))
        stack.enter_context(patched(send_outreach, "notify", lambda *a: None))
        result = send_outreach.send_all_prepared(ACCOUNT)
    assert sent == ["b@x.com"], "suppressed skipped, then one sent before the cap bit"
    assert discarded == [1], "the suppressed draft is discarded, not left pending"
    assert result["sent"] == 1
    reasons = " ".join(s["reason"].lower() for s in result["skipped"])
    assert "opted out" in reasons and "rolling 24-hour" in reasons


def test_send_all_retires_a_draft_that_became_unverified_after_preparation():
    drafts = [
        {"id": 1, "row_index": 2, "email": "stale@x.com", "subject": "S", "body": "b"},
        {"id": 2, "row_index": 3, "email": "verified@x.com", "subject": "S", "body": "b"},
    ]
    stale = _row(email="stale@x.com")
    stale[sheets.COL_EMAIL_CONFIDENCE] = "unverified"
    rows = [(2, stale), (3, _row(email="verified@x.com"))]
    with contextlib.ExitStack() as stack:
        log = _sendall_env(stack, drafts, rows, {})
        result = send_outreach.send_all_prepared(ACCOUNT)
    assert log["sent"] == ["verified@x.com"]
    assert log["discarded"] == [1]
    assert any("unverified" in item["reason"].lower() for item in result["skipped"])


def test_send_all_prepared_can_limit_a_batch_to_new_drafts():
    """Auto mode must not turn an older, manually queued draft into an
    automatic send just because a fresh batch was prepared."""
    drafts = [
        {"id": 1, "row_index": 2, "email": "older@x.com", "subject": "S", "body": "b"},
        {"id": 2, "row_index": 3, "email": "new@x.com", "subject": "S", "body": "b"},
    ]
    rows = [(2, _row(status="", email="older@x.com")), (3, _row(status="", email="new@x.com"))]
    with contextlib.ExitStack() as stack:
        log = _sendall_env(stack, drafts, rows, {})
        result = send_outreach.send_all_prepared(ACCOUNT, draft_ids=[2])
    assert log["sent"] == ["new@x.com"]
    assert result["sent"] == 1


def test_auto_mode_holds_send_time_validator_failures_for_manual_review():
    """The unattended boundary validates the durable copy, not just whatever
    the generator produced earlier. A bad/stale row stays pending and Gmail is
    never called, so an operator can repair it instead of losing the draft."""
    draft = {
        "id": 1, "row_index": 2, "email": "held@x.com",
        "subject": "Broken", "body": "truncated",
    }
    account = dict(ACCOUNT, outreach_send_mode="auto")
    rows = [(2, _row(status="", email="held@x.com"))]
    with contextlib.ExitStack() as stack:
        log = _sendall_env(stack, [draft], rows, {})
        stack.enter_context(patched(
            agent, "outreach_problems",
            lambda *a, **k: ["Body is truncated.", "Unsubscribe line is missing."],
        ))
        result = send_outreach.send_all_prepared(account)
    assert log["sent"] == []
    assert log["discarded"] == [], "held drafts remain available for manual repair"
    assert result["sent"] == 0
    assert result["needs_review"] == [{
        "email": "held@x.com",
        "problems": ["Body is truncated.", "Unsubscribe line is missing."],
    }]


def test_manual_batch_does_not_turn_advisory_copy_rules_into_a_send_blocker():
    """Manual approval remains authoritative: a human may intentionally edit
    copy outside the model's house style. The hard validator is for unattended
    delivery only."""
    draft = {"id": 1, "row_index": 2, "email": "manual@x.com", "subject": "S", "body": "b"}
    rows = [(2, _row(status="", email="manual@x.com"))]
    with contextlib.ExitStack() as stack:
        log = _sendall_env(stack, [draft], rows, {})
        stack.enter_context(patched(
            agent, "outreach_problems",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("manual mode must not invoke the auto gate")),
        ))
        result = send_outreach.send_all_prepared(dict(ACCOUNT, outreach_send_mode="manual"))
    assert log["sent"] == ["manual@x.com"]
    assert result["needs_review"] == []


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
    stack.enter_context(patched(drafts_db, "require_send_log", lambda: None))
    stack.enter_context(patched(bounces, "assert_sendable", lambda account: None))
    stack.enter_context(patched(send_outreach, "assert_domain_safe", lambda account: None))
    stack.enter_context(patched(drafts_db, "has_first_touch_conflict", lambda *a, **k: False))
    stack.enter_context(patched(sheets, "get_all_rows", lambda account: rows))
    stack.enter_context(patched(auth, "unsubscribe_url", lambda *a: "https://u/x"))
    stack.enter_context(patched(drafts_db, "claim_send", lambda *a, **k: True))
    stack.enter_context(patched(drafts_db, "release_send_claim", lambda *a, **k: True))
    capacity = {"used": sent_today}
    def reserve(account_id, operation_key, limit):
        if capacity["used"] >= limit:
            return False
        capacity["used"] += 1
        return True
    stack.enter_context(patched(send_outreach.send_capacity_db, "reserve", reserve))
    stack.enter_context(patched(send_outreach.send_capacity_db, "complete", lambda *a, **k: True))
    stack.enter_context(patched(drafts_db, "mark_sent", lambda *a, **k: True))
    stack.enter_context(patched(tracked_threads, "upsert_thread", lambda *a, **k: None))
    def fake_gmail(account, email, *args, **kwargs):
        outcome = outcomes.get(email)
        if isinstance(outcome, Exception):
            raise outcome
        log["sent"].append(email)
        return "t"
    def fake_sheet(account, row_index, email, *args, **kwargs):
        outcome = outcomes.get(email)
        if isinstance(outcome, str):
            raise RuntimeError(outcome)
    stack.enter_context(patched(gmail, "send_email", fake_gmail))
    stack.enter_context(patched(send_outreach, "mark_row_sent", fake_sheet))
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
            for i in range(1, 25)] + [
        (draft["row_index"], _row(status="", email=draft["email"])) for draft in drafts
    ]
    with contextlib.ExitStack() as stack:
        log = _sendall_env(stack, drafts, rows, outcomes, sent_today=24)
        result = send_outreach.send_all_prepared(ACCOUNT)
    assert len(log["sent"]) == 1, f"cap must stop at one; sent {log['sent']}"
    assert result["sent"] == 1
    assert result["failed"] == [], "a sheet write failure is not a failed send"
    assert len(result["sheet_warnings"]) == 1
    assert any("rolling 24-hour" in s["reason"] for s in result["skipped"])


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
    assert any("already handled" in s["reason"] for s in result["skipped"])


def test_a_deleted_sheet_row_blocks_before_gmail_and_retires_the_draft():
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
        stack.enter_context(patched(
            drafts_db, "discard",
            lambda aid, did: queue_status.update(status="discarded") or True,
        ))
        # drafts_db.mark_sent takes the draft out of the pending set; reflect that
        # so the second batch sees what production would. Wraps _send_env's
        # recorder rather than replacing it, so the write still shows up in order.
        recorded = drafts_db.mark_sent

        def marking(aid, did, s, b, **kwargs):
            queue_status["status"] = "sent"
            return recorded(aid, did, s, b, **kwargs)

        stack.enter_context(patched(drafts_db, "mark_sent", marking))

        first = send_outreach.send_all_prepared(ACCOUNT)
        second = send_outreach.send_all_prepared(ACCOUNT)

    assert [s for s, _ in order].count("gmail") == 0
    assert first["sent"] == 0
    assert second["sent"] == 0, "nothing left to send"
    assert first["failed"] == []
    assert first["skipped"], "the stale row must be reported as a permanent block"


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
        with patched(server.send_outreach, "prepare_drafts", lambda a, **k: {"prepared": 0, "draft_ids": []}):
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


def test_server_auto_mode_sends_only_drafts_created_by_this_prepare():
    import server
    from types import SimpleNamespace

    account = dict(ACCOUNT, outreach_send_mode="auto")
    req = SimpleNamespace(state=SimpleNamespace(account_id=account["id"]))
    body = server.PrepareCampaignBody(confirmed=True)
    captured = {}
    sent_ids = []
    server._accounts_sending.discard(account["id"])
    with contextlib.ExitStack() as stack:
        # Auto mode is only valid for a controlled deployment. Keep this unit
        # test independent from the local .env while production remains manual
        # by default.
        stack.enter_context(patched(server.config, "AUTO_SEND_ENABLED", True))
        stack.enter_context(patched(server, "_account_monitoring", lambda a: {"status": "healthy", "message": "Reply monitoring is active.", "last_checked_at": "2099-01-01T00:00:00+00:00"}))
        stack.enter_context(patched(server, "_account", lambda r: account))
        stack.enter_context(patched(server.ratelimit, "check", lambda *a, **k: True))
        stack.enter_context(patched(server, "_campaign_preview", lambda a: {"eligible": 1, "blockers": []}))
        stack.enter_context(patched(server, "start_job", lambda aid, fn: captured.update(fn=fn) or "job-1"))
        stack.enter_context(patched(
            server.send_outreach, "prepare_drafts",
            lambda a, auto_send=False: {"prepared": 1, "total": 1, "draft_ids": [42], "auto_mode_seen": auto_send},
        ))
        stack.enter_context(patched(
            server.send_outreach, "send_all_prepared",
            lambda a, draft_ids=None: sent_ids.extend(draft_ids or []) or {"sent": 1, "skipped": [], "failed": []},
        ))
        server.prepare_campaigns(req, body)
        result = captured["fn"]()
    assert sent_ids == [42]
    assert result["auto_mode_seen"] is True
    assert result["auto_sent"]["sent"] == 1


def test_server_auto_mode_pauses_when_monitoring_is_not_healthy():
    import server
    from types import SimpleNamespace

    account = dict(ACCOUNT, outreach_send_mode="auto")
    req = SimpleNamespace(state=SimpleNamespace(account_id=account["id"]))
    body = server.PrepareCampaignBody(confirmed=True)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server.config, "AUTO_SEND_ENABLED", True))
        stack.enter_context(patched(server, "_account", lambda r: account))
        stack.enter_context(patched(server, "_account_monitoring", lambda a: {
            "status": "stale",
            "message": "Reply monitoring is delayed. Check the worker or reconnect Gmail.",
            "last_checked_at": "2020-01-01T00:00:00+00:00",
        }))
        with pytest.raises(server.HTTPException) as exc:
            server.prepare_campaigns(req, body)
    assert exc.value.status_code == 409
    assert "first-touch preparation is paused" in exc.value.detail
    assert "stale" in exc.value.detail


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


def test_me_surfaces_send_blockers():
    """REGRESSION (BUGS.md BUG-1): /api/me must expose the same blockers the
    campaign preview gates sending on, so Settings can render them next to the
    field that fixes each one instead of only at send time."""
    import server
    account = dict(ACCOUNT, email="oanh@x.com", sender_name="", meeting_purpose=agent.DEFAULT_MEETING_PURPOSE)
    from types import SimpleNamespace
    req = SimpleNamespace(state=SimpleNamespace(account_id=account["id"]))
    with patched(server, "_account", lambda r: account):
        result = server.me(req)
    assert len(result["sendBlockers"]) == 2, result["sendBlockers"]

    ready_account = dict(ACCOUNT, email="oanh@x.com")
    with patched(server, "_account", lambda r: ready_account):
        result = server.me(req)
    assert result["sendBlockers"] == []


def test_me_surfaces_account_monitoring_state():
    """A connected OAuth token is not enough: Settings must show the worker
    heartbeat/error that proves reply monitoring is actually working."""
    import server
    from types import SimpleNamespace

    req = SimpleNamespace(state=SimpleNamespace(account_id=ACCOUNT["id"]))
    connected = dict(ACCOUNT, email="owner@example.com", google_token="ciphertext", last_error=None)

    with patched(server, "_account", lambda r: connected), patched(
        server.google_auth,
        "capability_status",
        lambda account: {"monitor": True, "send": False, "sheets": False},
    ):
        result = server.me(req)
    assert result["monitoring"]["status"] == "waiting"

    broken = dict(connected, last_error="Google disconnected — reconnect in Settings")
    with patched(server, "_account", lambda r: broken), patched(
        server.google_auth,
        "capability_status",
        lambda account: {"monitor": True, "send": False, "sheets": False},
    ):
        result = server.me(req)
    assert result["monitoring"] == {
        "status": "attention",
        "message": "Google disconnected — reconnect in Settings",
        "last_checked_at": None,
    }

    healthy = dict(connected, worker_heartbeat_at="2099-01-01T00:00:00+00:00")
    with patched(server, "_account", lambda r: healthy), patched(
        server.google_auth,
        "capability_status",
        lambda account: {"monitor": True, "send": False, "sheets": False},
    ):
        result = server.me(req)
    assert result["monitoring"]["status"] == "healthy"


def test_record_outcome_requires_a_monitored_conversation_and_is_idempotent():
    import server
    from types import SimpleNamespace

    req = SimpleNamespace(state=SimpleNamespace(account_id="a1"))
    campaign = {"email": "prospect@example.com", "name": "Prospect", "company": "Acme", "source": "gmail"}
    saved = {
        "id": 12,
        "event_type": "meeting_booked",
        "status": "confirmed",
        "details": {"email": "prospect@example.com", "name": "Prospect", "company": "Acme"},
        "created_at": "2026-08-20T00:00:00+00:00",
    }
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "list_campaigns", lambda request: [campaign]))
        stack.enter_context(patched(server.outcomes_db, "list_outcomes", lambda account_id: []))
        stack.enter_context(patched(server.outcomes_db, "record", lambda *args, **kwargs: saved))
        result = server.record_outcome(req, server.OutcomeBody(email="Prospect@example.com", outcome="meeting_booked"))
    assert result["outcome"] == "meeting_booked"
    assert result["email"] == "prospect@example.com"

    with patched(server, "list_campaigns", lambda request: []):
        with pytest.raises(server.HTTPException) as exc:
            server.record_outcome(req, server.OutcomeBody(email="unknown@example.com", outcome="meeting_booked"))
    assert exc.value.status_code == 404


# --------------------------------------------------------- drafts_db: rewritten

class _DraftsTable:
    """Minimal PostgREST-shaped fake for outreach_drafts, enough for
    mark_rewritten/mark_sent: update + eq + execute."""

    def __init__(self, rows):
        self.rows = rows
        self._eq = []
        self._values = None

    def table(self, name):
        return self

    def update(self, values):
        self._values = values
        return self

    def eq(self, col, val):
        self._eq.append((col, val))
        return self

    def execute(self):
        eq = self._eq
        self._eq = []
        matched = [r for r in self.rows if all(r.get(c) == v for c, v in eq)]
        for r in matched:
            r.update(self._values)
        return type("R", (), {"data": matched})()


def test_drafts_mark_rewritten_writes_the_flag():
    fake = _DraftsTable(rows=[{"id": 7, "account_id": "a1", "rewritten": False}])
    with patched(drafts_db, "_get_client", lambda: fake):
        drafts_db.mark_rewritten("a1", 7)
    assert fake.rows[0]["rewritten"] is True


def test_drafts_mark_rewritten_is_best_effort():
    def boom():
        raise RuntimeError("supabase down")
    with patched(drafts_db, "_get_client", boom):
        drafts_db.mark_rewritten("a1", 7)  # must not raise


def test_drafts_mark_sent_does_not_touch_rewritten():
    """Regression guard for the finding that shaped this design: mark_sent
    is the durable record require_send_log gates the whole send on --
    rewritten must never join that update. Seeded True (as if
    mark_rewritten already ran) so an accidental "rewritten": False in
    mark_sent's own update dict would flip it back and be caught here."""
    fake = _DraftsTable(rows=[{"id": 7, "account_id": "a1", "status": "pending", "rewritten": True}])
    with patched(drafts_db, "_get_client", lambda: fake):
        drafts_db.mark_sent("a1", 7, "subject", "body")
    assert fake.rows[0]["rewritten"] is True, "mark_sent must never write the rewritten column"


class _MetricsTable:
    """Small read-only PostgREST fake for aggregate review-signal queries."""

    def __init__(self, rows):
        self.rows = rows
        self.filters = []

    def table(self, name):
        return self

    def select(self, columns):
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def execute(self):
        rows = [
            row for row in self.rows
            if all(row.get(column) == value for column, value in self.filters)
        ]
        self.filters = []
        return type("R", (), {"data": rows})()


def test_drafts_edit_metrics_separates_human_edits_from_model_rewrites():
    fake = _MetricsTable([
        {"account_id": "a1", "status": "sent", "subject": "S", "body": "same", "original_subject": "S", "original_body": "same", "rewritten": False},
        {"account_id": "a1", "status": "sent", "subject": "S", "body": "operator copy", "original_subject": "S", "original_body": "model copy", "rewritten": False},
        {"account_id": "a1", "status": "sent", "subject": "S2", "body": "rewrite", "original_subject": "S", "original_body": "model copy", "rewritten": True},
        {"account_id": "a1", "status": "pending", "subject": "S", "body": "queued", "original_subject": "S", "original_body": "queued", "rewritten": False},
    ])
    with patched(drafts_db, "_get_client", lambda: fake):
        result = drafts_db.edit_metrics("a1")
    assert result == {
        "drafted": 4,
        "approved": 3,
        "human_edited": 1,
        "model_rewritten": 1,
        "untouched": 1,
    }


def test_reply_edit_metrics_excludes_follow_up_rows():
    fake = _MetricsTable([
        {"account_id": "a1", "kind": "reply", "status": "sent", "draft_reply": "edited", "original_draft_reply": "model", "rewritten": False},
        {"account_id": "a1", "kind": "reply", "status": "sent", "draft_reply": "same", "original_draft_reply": "same", "rewritten": False},
        {"account_id": "a1", "kind": "follow_up", "status": "sent", "draft_reply": "follow-up", "original_draft_reply": "model", "rewritten": False},
    ])
    with patched(reviews_db, "_get_client", lambda: fake):
        result = reviews_db.reply_edit_metrics("a1")
    assert result == {
        "drafted": 2,
        "approved": 2,
        "human_edited": 1,
        "model_rewritten": 0,
        "untouched": 1,
    }


def test_voice_profile_uses_only_unrewritten_edits_and_keeps_copy_aggregate_only():
    calls = []
    first_touch = [
        {
            "original_body": "model draft about a prospect",
            "body": "Hi there, can we talk Tuesday? Oanh",
            "rewritten": False,
        },
        {
            "original_body": "another model draft",
            "body": "Hi there, could we talk Thursday? Oanh",
            "rewritten": False,
        },
        {
            "original_body": "A short approved draft.",
            "body": "A short approved draft.",
            "rewritten": False,
        },
        {
            "original_body": "model copy",
            "body": "private prospect detail that must not leak",
            "rewritten": True,
        },
    ]
    replies = [{
        "original_draft_reply": "model reply",
        "draft_reply": "Hello, here is the update. Oanh",
        "rewritten": False,
    }]

    def load_first(account_id):
        calls.append(("first_touch", account_id))
        return first_touch

    def load_replies(account_id):
        calls.append(("reply", account_id))
        return replies

    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(config, "VOICE_FEEDBACK_ENABLED", True))
        stack.enter_context(patched(drafts_db, "list_sent_edit_pairs", load_first))
        stack.enter_context(patched(reviews_db, "list_sent_edit_pairs", load_replies))
        voice_signals.clear()
        profile = voice_signals.profile("acct-1")

    assert calls == [("first_touch", "acct-1"), ("reply", "acct-1")]
    assert "4 human approvals" in profile
    assert "3 were edited" in profile
    assert "words" in profile
    assert "private prospect detail" not in profile
    assert "Tuesday" not in profile and "Thursday" not in profile
    voice_signals.clear()


def test_voice_profile_is_fenced_as_a_soft_sender_preference():
    profile = (
        "Observed from 3 human approvals in this inbox; 3 were edited: keep messages around 24 words."
    )
    prompt = agent.outreach_prompt(dict(ACCOUNT, voice_profile=profile), "John", "Acme")
    assert "bounded style signal" in prompt
    assert "soft preference" in prompt
    assert "no prospect facts" in prompt
    assert profile in prompt


def test_generation_attaches_account_scoped_voice_profile_before_prompting():
    attached = []

    def attach(account):
        attached.append(account["id"])
        return dict(account, voice_profile="Observed from 3 human approvals in this inbox")

    captured = {}
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(agent.voice_signals, "with_profile", attach))
        stack.enter_context(patched(
            agent, "_chat", lambda system, prompt, purpose: captured.update(prompt=prompt) or GOOD_EMAIL,
        ))
        agent.generate_outreach_email(ACCOUNT, "John", "Acme")

    assert attached == ["acct-1"]
    assert "human approvals" in captured["prompt"]


def test_send_all_prepared_never_marks_a_draft_rewritten():
    """send_prepared_draft's rewritten default is False by construction for
    the batch path -- there is no browser-side rewritten state a
    server-side batch send could pass. Proven against the REAL
    send_prepared_draft (not a fake standing in for it), by patching
    mark_rewritten to raise on any call and confirming a full batch run
    still completes -- not just by trusting today's default value."""
    drafts = [{"id": 1, "row_index": 2, "email": "a@x.com", "subject": "S", "body": "b"}]

    def boom(*a, **k):
        raise AssertionError("send_all_prepared must never mark a draft rewritten")

    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(bounces, "assert_sendable", lambda account: None))
        stack.enter_context(patched(send_outreach, "_MARK_RETRY_DELAY", 0))
        stack.enter_context(patched(send_outreach, "_SEND_ALL_MIN_GAP", 0))
        stack.enter_context(patched(send_outreach, "_SEND_ALL_MAX_GAP", 0))
        stack.enter_context(patched(drafts_db, "require_send_log", lambda: None))
        stack.enter_context(patched(auth, "unsubscribe_url", lambda aid, e: "https://u/unsub?t=x.y"))
        stack.enter_context(patched(drafts_db, "list_pending_drafts", lambda aid: drafts))
        stack.enter_context(patched(drafts_db, "count_sent_last_24_hours", lambda aid: 0))
        stack.enter_context(patched(drafts_db, "count_sent_since", lambda aid, since: 0))
        stack.enter_context(patched(suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(sheets, "campaign_readiness", lambda account, sent_today=0, rows=None: {"remaining_today": 5}))
        stack.enter_context(patched(sheets, "get_all_rows", lambda account: [(2, _row(email="a@x.com"))]))
        stack.enter_context(patched(send_outreach, "assert_domain_safe", lambda account: None))
        stack.enter_context(patched(drafts_db, "has_first_touch_conflict", lambda *a, **k: False))
        stack.enter_context(patched(gmail, "send_email", lambda *a, **k: "t-1"))
        stack.enter_context(patched(drafts_db, "claim_send", lambda *a, **k: True))
        stack.enter_context(patched(drafts_db, "mark_sent", lambda *a, **k: True))
        stack.enter_context(patched(send_outreach.send_capacity_db, "reserve", lambda *a, **k: True))
        stack.enter_context(patched(send_outreach.send_capacity_db, "complete", lambda *a, **k: True))
        stack.enter_context(patched(drafts_db, "mark_rewritten", boom))
        stack.enter_context(patched(send_outreach, "mark_row_sent", lambda *a, **k: None))
        stack.enter_context(patched(send_outreach, "notify", lambda *a: None))
        result = send_outreach.send_all_prepared(ACCOUNT)
    assert result["sent"] == 1


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


_REWRITABLE_DRAFT = dict(_PENDING_DRAFT, name="John", company="Acme")


def test_rewrite_draft_404_when_missing_or_handled():
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: None))
        try:
            server.rewrite_draft(_srv_req(), 7, server.RewriteDraftBody(subject="s", body="b", instruction="shorter"))
            raise AssertionError("expected 404")
        except server.HTTPException as e:
            assert e.status_code == 404


def test_rewrite_draft_400_on_blank_instruction():
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: dict(_REWRITABLE_DRAFT)))
        try:
            server.rewrite_draft(_srv_req(), 7, server.RewriteDraftBody(subject="s", body="b", instruction="   "))
            raise AssertionError("expected 400")
        except server.HTTPException as e:
            assert e.status_code == 400


def test_rewrite_draft_429_scoped_per_draft_not_per_account():
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: dict(_REWRITABLE_DRAFT, id=did)))
        stack.enter_context(patched(server, "start_job", lambda aid, fn: "job-x"))
        body = server.RewriteDraftBody(subject="s", body="b", instruction="shorter")
        for _ in range(10):
            server.rewrite_draft(_srv_req(), 7, body)  # exhaust the limit for draft 7
        try:
            server.rewrite_draft(_srv_req(), 7, body)
            raise AssertionError("expected 429 once the per-draft limit is exhausted")
        except server.HTTPException as e:
            assert e.status_code == 429
        # A different draft is unaffected -- proves the scoping, not just that some limit exists.
        assert server.rewrite_draft(_srv_req(), 8, body) == {"job_id": "job-x"}


def test_rewrite_draft_job_uses_the_payloads_text_not_the_stored_row():
    """Regression test for the finding that shaped this design: the stored
    row's subject/body must never be what gets rewritten, only the
    operator's current (payload) text -- otherwise a hand-edit vanishes
    with no warning the moment a preset is clicked."""
    import server
    captured = {}
    stored = dict(_REWRITABLE_DRAFT, subject="STALE stored subject", body="STALE stored body")
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.ratelimit, "check", lambda *a, **k: True))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: dict(stored)))
        stack.enter_context(patched(server.auth, "unsubscribe_url", lambda aid, e: "https://u/unsub?t=x"))
        stack.enter_context(patched(server.agent, "rewrite_outreach_email",
            lambda account, name, company, subject, body, instruction, unsubscribe_url="":
                captured.update(subject=subject, body=body) or (subject, body)))
        captured_fn = {}
        stack.enter_context(patched(server, "start_job", lambda aid, fn: captured_fn.update(fn=fn) or "job-1"))
        server.rewrite_draft(
            _srv_req(), 7,
            server.RewriteDraftBody(subject="fresh typed subject", body="fresh typed body", instruction="shorter"),
        )
        captured_fn["fn"]()
    assert captured["subject"] == "fresh typed subject"
    assert captured["body"] == "fresh typed body"


def test_rewrite_draft_never_calls_plans_check():
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.ratelimit, "check", lambda *a, **k: True))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: dict(_REWRITABLE_DRAFT)))
        stack.enter_context(patched(server.auth, "unsubscribe_url", lambda aid, e: "https://u/unsub?t=x"))
        stack.enter_context(patched(server.agent, "rewrite_outreach_email",
            lambda *a, **k: ("s", "b")))
        stack.enter_context(patched(server.plans, "check",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not check quota"))))
        captured_fn = {}
        stack.enter_context(patched(server, "start_job", lambda aid, fn: captured_fn.update(fn=fn) or "job-1"))
        server.rewrite_draft(_srv_req(), 7, server.RewriteDraftBody(subject="s", body="b", instruction="shorter"))
        result = captured_fn["fn"]()
    assert result == {"subject": "s", "body": "b"}


def test_rewrite_draft_never_writes_to_drafts_db():
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.ratelimit, "check", lambda *a, **k: True))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: dict(_REWRITABLE_DRAFT)))
        stack.enter_context(patched(server.auth, "unsubscribe_url", lambda aid, e: "https://u/unsub?t=x"))
        stack.enter_context(patched(server.agent, "rewrite_outreach_email", lambda *a, **k: ("s", "b")))
        for method in ("mark_sent", "mark_rewritten", "add_draft", "discard"):
            stack.enter_context(patched(server.drafts_db, method,
                lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"must not call drafts_db.{method}"))))
        captured_fn = {}
        stack.enter_context(patched(server, "start_job", lambda aid, fn: captured_fn.update(fn=fn) or "job-1"))
        server.rewrite_draft(_srv_req(), 7, server.RewriteDraftBody(subject="s", body="b", instruction="shorter"))
        captured_fn["fn"]()  # must not raise


def test_send_draft_409_and_discards_when_suppressed():
    import server
    discarded = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: dict(_PENDING_DRAFT)))
        stack.enter_context(patched(server.drafts_db, "require_send_log", lambda: None))
        stack.enter_context(patched(server.bounces, "assert_sendable", lambda account: None))
        stack.enter_context(patched(server.send_outreach, "assert_domain_safe", lambda account: None))
        stack.enter_context(patched(server.sheets, "get_all_rows", lambda account: [(2, _row())]))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: True))
        stack.enter_context(patched(server.drafts_db, "discard", lambda aid, did: discarded.append(did)))
        try:
            server.send_draft(_srv_req(), 7, server.SendDraftBody(subject="s", body="b"))
            raise AssertionError("expected 409")
        except send_outreach.SendFailure as e:
            assert e.code == "send_blocked" and e.retryable is False
    assert discarded == [7], "a draft whose recipient opted out is discarded, not sent"


def test_send_draft_409_when_cap_reached():
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: dict(_PENDING_DRAFT)))
        stack.enter_context(patched(server.drafts_db, "require_send_log", lambda: None))
        stack.enter_context(patched(server.bounces, "assert_sendable", lambda account: None))
        stack.enter_context(patched(server.send_outreach, "assert_domain_safe", lambda account: None))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(server.sheets, "get_all_rows", lambda account: [(2, _row())]))
        stack.enter_context(patched(server.drafts_db, "has_first_touch_conflict", lambda *a, **k: False))
        stack.enter_context(patched(server.drafts_db, "claim_send", lambda *a, **k: True))
        stack.enter_context(patched(server.drafts_db, "release_send_claim", lambda *a, **k: True))
        stack.enter_context(patched(server.send_capacity_db, "reserve", lambda *a, **k: False))
        try:
            server.send_draft(_srv_req(), 7, server.SendDraftBody(subject="s", body="b"))
            raise AssertionError("expected 409")
        except send_outreach.SendFailure as e:
            assert e.code == "capacity_reached" and e.retryable is True


def test_send_draft_retires_a_source_guess_that_is_still_unverified():
    import server
    row = _row(email=_PENDING_DRAFT["email"])
    row[sheets.COL_EMAIL_CONFIDENCE] = "unverified"
    discarded = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: dict(_PENDING_DRAFT)))
        stack.enter_context(patched(server.drafts_db, "require_send_log", lambda: None))
        stack.enter_context(patched(server.bounces, "assert_sendable", lambda account: None))
        stack.enter_context(patched(server.send_outreach, "assert_domain_safe", lambda account: None))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(server.sheets, "get_all_rows", lambda account: [(2, row)]))
        stack.enter_context(patched(server.drafts_db, "discard", lambda aid, did: discarded.append(did)))
        try:
            server.send_draft(_srv_req(), 7, server.SendDraftBody(subject="s", body="b"))
            raise AssertionError("expected source-verification gate")
        except send_outreach.SendFailure as e:
            assert e.code == "send_blocked"
            assert "unverified" in e.detail
    assert discarded == [7]


def test_send_draft_happy_path_marks_edited_copy():
    import server
    marked = {}
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "count_sent_last_24_hours", lambda aid: 0))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: dict(_PENDING_DRAFT)))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(server.suppressions_db, "list_suppressed_emails", lambda aid: set()))
        stack.enter_context(patched(server.sheets, "get_all_rows", lambda account: [(2, _row())]))
        stack.enter_context(patched(server.sheets, "campaign_readiness", lambda account, **k: {"remaining_today": 5}))

        def fake_send(a, d, subject=None, body=None, rewritten=False):
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
        stack.enter_context(patched(server.sheets, "get_all_rows", lambda account: [(2, _row())]))
        stack.enter_context(patched(server.sheets, "campaign_readiness", lambda account, **k: {"remaining_today": 5}))
        stack.enter_context(patched(server.send_outreach, "send_prepared_draft",
            lambda a, d, subject=None, body=None, rewritten=False: {
                "thread_id": "t-1", "body": "b",
                "sheet_error": "j@x.com was emailed successfully, but its sheet row was not updated",
            }))
        result = server.send_draft(_srv_req(), 7, server.SendDraftBody(subject="s", body="b"))
    assert result["ok"] is True
    assert "emailed successfully" in result["sheet_warning"]


def test_send_draft_calls_mark_rewritten_when_payload_says_so():
    """send_draft doesn't call mark_rewritten itself -- it delegates to the
    real send_outreach.send_prepared_draft, which does (see
    test_send_all_prepared_never_marks_a_draft_rewritten for the batch-path
    half of this invariant). send_prepared_draft is deliberately NOT faked
    away here, unlike the tests above, so the payload.rewritten ->
    mark_rewritten wire is proven end to end rather than assumed."""
    import server
    marked = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "count_sent_last_24_hours", lambda aid: 0))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: dict(_PENDING_DRAFT)))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(server.suppressions_db, "list_suppressed_emails", lambda aid: set()))
        stack.enter_context(patched(server.sheets, "get_all_rows", lambda account: [(2, _row())]))
        stack.enter_context(patched(server.sheets, "campaign_readiness", lambda account, **k: {"remaining_today": 5}))
        stack.enter_context(patched(drafts_db, "require_send_log", lambda: None))
        stack.enter_context(patched(bounces, "assert_sendable", lambda account: None))
        stack.enter_context(patched(send_outreach, "assert_domain_safe", lambda account: None))
        stack.enter_context(patched(drafts_db, "has_first_touch_conflict", lambda *a, **k: False))
        stack.enter_context(patched(auth, "unsubscribe_url", lambda aid, e: "https://u/unsub?t=x"))
        stack.enter_context(patched(gmail, "send_email", lambda *a, **k: "t-1"))
        stack.enter_context(patched(drafts_db, "claim_send", lambda *a, **k: True))
        stack.enter_context(patched(drafts_db, "mark_sent", lambda *a, **k: True))
        stack.enter_context(patched(send_outreach.send_capacity_db, "reserve", lambda *a, **k: True))
        stack.enter_context(patched(send_outreach.send_capacity_db, "complete", lambda *a, **k: True))
        stack.enter_context(patched(drafts_db, "mark_rewritten", lambda aid, did: marked.append(did)))
        stack.enter_context(patched(send_outreach, "mark_row_sent", lambda *a, **k: None))
        stack.enter_context(patched(tracked_threads, "upsert_thread", lambda *a, **k: None))
        result = server.send_draft(_srv_req(), 7, server.SendDraftBody(subject="s", body="b", rewritten=True))
    assert result == {"ok": True, "sheet_warning": None}
    assert marked == [7]


def test_send_draft_does_not_call_mark_rewritten_by_default():
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "count_sent_last_24_hours", lambda aid: 0))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: dict(_PENDING_DRAFT)))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(server.suppressions_db, "list_suppressed_emails", lambda aid: set()))
        stack.enter_context(patched(server.sheets, "get_all_rows", lambda account: [(2, _row())]))
        stack.enter_context(patched(server.sheets, "campaign_readiness", lambda account, **k: {"remaining_today": 5}))
        stack.enter_context(patched(drafts_db, "require_send_log", lambda: None))
        stack.enter_context(patched(bounces, "assert_sendable", lambda account: None))
        stack.enter_context(patched(send_outreach, "assert_domain_safe", lambda account: None))
        stack.enter_context(patched(drafts_db, "has_first_touch_conflict", lambda *a, **k: False))
        stack.enter_context(patched(auth, "unsubscribe_url", lambda aid, e: "https://u/unsub?t=x"))
        stack.enter_context(patched(gmail, "send_email", lambda *a, **k: "t-1"))
        stack.enter_context(patched(drafts_db, "claim_send", lambda *a, **k: True))
        stack.enter_context(patched(drafts_db, "mark_sent", lambda *a, **k: True))
        stack.enter_context(patched(send_outreach.send_capacity_db, "reserve", lambda *a, **k: True))
        stack.enter_context(patched(send_outreach.send_capacity_db, "complete", lambda *a, **k: True))
        stack.enter_context(patched(drafts_db, "mark_rewritten",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not mark rewritten without the flag"))))
        stack.enter_context(patched(send_outreach, "mark_row_sent", lambda *a, **k: None))
        stack.enter_context(patched(tracked_threads, "upsert_thread", lambda *a, **k: None))
        result = server.send_draft(_srv_req(), 7, server.SendDraftBody(subject="s", body="b"))
    assert result == {"ok": True, "sheet_warning": None}


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


# --------------------------------------------------- server: reply review routes

_PENDING_REVIEW = {"id": 9, "row_index": 2, "thread_id": "t1", "name": "John",
                   "email": "john@x.com", "customer_reply": "sounds good",
                   "gmail_message_id": "m-1", "status": "pending"}
_FLAGGED_REVIEW = dict(_PENDING_REVIEW, email="assistant@other.com", status="flagged")


def _tracked_reply_candidate(**overrides):
    candidate = {
        "text": "Could you send the pricing details?",
        "history": [(False, "Initial outreach")],
        "message_id": "m-1",
        "kind": "on_sheet",
        "answered_elsewhere": False,
        "from_addr": "john@x.com",
        "timestamp": "2026-08-31T02:00:00+00:00",
    }
    candidate.update(overrides)
    return candidate


def test_draft_tracked_reply_endpoint_is_account_scoped_and_starts_a_job():
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT, id="acct-1")))
        stack.enter_context(patched(server.tracked_threads, "get_by_id",
            lambda aid, tid: {"id": tid, "status": "active"} if aid == "acct-1" else None))
        stack.enter_context(patched(server.ratelimit, "check", lambda *a, **k: True))
        stack.enter_context(patched(server, "start_job", lambda aid, fn: "job-thread-reply"))
        assert server.draft_tracked_reply(_srv_req(), 73) == {"job_id": "job-thread-reply"}


def test_draft_tracked_reply_endpoint_hides_foreign_thread_ids():
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT, id="acct-1")))
        stack.enter_context(patched(server.tracked_threads, "get_by_id", lambda *a: None))
        with pytest.raises(server.HTTPException) as exc:
            server.draft_tracked_reply(_srv_req(), 999)
    assert exc.value.status_code == 404


def test_draft_tracked_reply_job_creates_normal_review_without_exposing_gmail_thread_id():
    import server
    tracked = {
        "id": 73, "status": "active", "thread_id": "gmail-secret-thread",
        "row_index": None, "name": "John", "email": "john@x.com", "company": "Acme",
    }
    review = dict(_PENDING_REVIEW, id=81, row_index=None, draft_reply="Here are the pricing details.")
    captured = {}
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server.tracked_threads, "get_by_id", lambda *a: dict(tracked)))
        stack.enter_context(patched(server.gmail, "get_thread", lambda *a: {"messages": []}))
        stack.enter_context(patched(server.gmail, "get_own_addresses", lambda *a: ({"me@x.com"}, False)))
        stack.enter_context(patched(server.gmail, "get_reply_candidates_with_history",
            lambda *a, **k: [_tracked_reply_candidate()]))
        stack.enter_context(patched(server.gmail, "get_reply_subject", lambda thread: "Re: Pricing"))
        stack.enter_context(patched(server.reviews_db, "find_review_id", lambda *a: None))
        stack.enter_context(patched(server.plans, "check", lambda *a: None))
        stack.enter_context(patched(server.agent, "draft_reply",
            lambda *a, **k: "Here are the pricing details."))
        stack.enter_context(patched(server.agent, "reply_problems", lambda *a: []))
        stack.enter_context(patched(server.reviews_db, "add_manual_review",
            lambda *a, **k: captured.update(args=a, kwargs=k) or (81, True)))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda *a: dict(review)))
        result = server._draft_tracked_reply_job(dict(ACCOUNT, id="acct-1"), 73)

    assert result["review_id"] == 81
    assert result["existing"] is False
    assert result["contact"] == {"name": "John", "email": "john@x.com"}
    assert result["latest_message"] == "sounds good"
    assert "thread_id" not in result
    assert "gmail-secret-thread" not in repr(result)
    assert captured["kwargs"]["gmail_message_id"] == "m-1"
    assert captured["kwargs"]["return_created"] is True


def test_draft_tracked_reply_job_reopens_existing_pending_review_without_spending_ai():
    import server
    tracked = {
        "id": 73, "status": "active", "thread_id": "t1", "row_index": None,
        "name": "John", "email": "john@x.com", "company": "Acme",
    }
    existing = dict(_PENDING_REVIEW, id=44, draft_reply="Existing draft", validator_problems="")
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server.tracked_threads, "get_by_id", lambda *a: dict(tracked)))
        stack.enter_context(patched(server.gmail, "get_thread", lambda *a: {"messages": []}))
        stack.enter_context(patched(server.gmail, "get_own_addresses", lambda *a: ({"me@x.com"}, False)))
        stack.enter_context(patched(server.gmail, "get_reply_candidates_with_history",
            lambda *a, **k: [_tracked_reply_candidate()]))
        stack.enter_context(patched(server.gmail, "get_reply_subject", lambda thread: "Re: Hello"))
        stack.enter_context(patched(server.reviews_db, "find_review_id", lambda *a: 44))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda *a: dict(existing)))
        stack.enter_context(patched(server.plans, "check",
            lambda *a: (_ for _ in ()).throw(AssertionError("existing draft must not spend quota"))))
        stack.enter_context(patched(server.agent, "draft_reply",
            lambda *a: (_ for _ in ()).throw(AssertionError("existing draft must not call AI"))))
        result = server._draft_tracked_reply_job(dict(ACCOUNT, id="acct-1"), 73)

    assert result["existing"] is True
    assert result["draft"] == "Existing draft"


def test_draft_tracked_reply_job_refuses_non_actionable_gmail_messages():
    import server
    tracked = {"id": 73, "status": "active", "thread_id": "t1", "email": "john@x.com"}
    cases = [
        (_tracked_reply_candidate(answered_elsewhere=True), "already answered"),
        (_tracked_reply_candidate(kind="off_sheet", from_addr="assistant@other.com"), "No reply from this contact"),
    ]
    for candidate, error_text in cases:
        with contextlib.ExitStack() as stack:
            stack.enter_context(patched(server.tracked_threads, "get_by_id", lambda *a: dict(tracked)))
            stack.enter_context(patched(server.gmail, "get_thread", lambda *a: {"messages": []}))
            stack.enter_context(patched(server.gmail, "get_own_addresses", lambda *a: ({"me@x.com"}, False)))
            stack.enter_context(patched(server.gmail, "get_reply_candidates_with_history",
                lambda *a, **k: [candidate]))
            with pytest.raises(RuntimeError) as exc:
                server._draft_tracked_reply_job(dict(ACCOUNT, id="acct-1"), 73)
        assert error_text.lower() in str(exc.value).lower()


def test_draft_tracked_reply_job_refuses_stale_context_after_newer_participant_message():
    import server
    tracked = {"id": 73, "status": "active", "thread_id": "t1", "email": "john@x.com"}
    candidates = [
        _tracked_reply_candidate(),
        _tracked_reply_candidate(
            message_id="m-2", kind="off_sheet", from_addr="assistant@other.com",
            text="Jumping in with updated requirements.",
        ),
    ]
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server.tracked_threads, "get_by_id", lambda *a: dict(tracked)))
        stack.enter_context(patched(server.gmail, "get_thread", lambda *a: {"messages": []}))
        stack.enter_context(patched(server.gmail, "get_own_addresses", lambda *a: ({"me@x.com"}, False)))
        stack.enter_context(patched(server.gmail, "get_reply_candidates_with_history",
            lambda *a, **k: candidates))
        with pytest.raises(RuntimeError) as exc:
            server._draft_tracked_reply_job(dict(ACCOUNT, id="acct-1"), 73)
    assert "newer message from another participant" in str(exc.value)


def test_draft_tracked_reply_job_preserves_plan_quota_message_for_the_ui():
    import server
    tracked = {"id": 73, "status": "active", "thread_id": "t1", "email": "john@x.com"}
    quota = server.plans.QuotaExceeded(
        "trial", server.usage.UNIT_DRAFT_REPLY, 10, 10,
        "Your Trial plan includes 10 reply drafts.",
    )
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server.tracked_threads, "get_by_id", lambda *a: dict(tracked)))
        stack.enter_context(patched(server.gmail, "get_thread", lambda *a: {"messages": []}))
        stack.enter_context(patched(server.gmail, "get_own_addresses", lambda *a: ({"me@x.com"}, False)))
        stack.enter_context(patched(server.gmail, "get_reply_candidates_with_history",
            lambda *a, **k: [_tracked_reply_candidate()]))
        stack.enter_context(patched(server.reviews_db, "find_review_id", lambda *a: None))
        stack.enter_context(patched(server.plans, "check", lambda *a: (_ for _ in ()).throw(quota)))
        with pytest.raises(RuntimeError) as exc:
            server._draft_tracked_reply_job(dict(ACCOUNT, id="acct-1"), 73)
    assert "10 reply drafts" in str(exc.value)


def test_draft_tracked_reply_job_fails_closed_when_alias_classification_is_degraded():
    import server
    tracked = {"id": 73, "status": "active", "thread_id": "t1", "email": "john@x.com"}
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server.tracked_threads, "get_by_id", lambda *a: dict(tracked)))
        stack.enter_context(patched(server.gmail, "get_thread", lambda *a: {"messages": []}))
        stack.enter_context(patched(server.gmail, "get_own_addresses", lambda *a: ({"me@x.com"}, True)))
        with pytest.raises(RuntimeError) as exc:
            server._draft_tracked_reply_job(dict(ACCOUNT, id="acct-1"), 73)
    assert "aliases could not be verified" in str(exc.value)


def test_send_reply_409_when_suppressed():
    """Unlike the outreach-send path, nothing gated this at all before now --
    an unsubscribed contact could still receive a reply."""
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: dict(_PENDING_REVIEW)))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: True))
        try:
            server.send_reply(_srv_req(), 9, server.SendReplyBody(body="hi"))
            raise AssertionError("expected 409")
        except server.HTTPException as e:
            assert e.status_code == 409
            assert "john@x.com" in e.detail


def test_send_reply_happy_path_when_not_suppressed():
    import server
    sent = {}
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server, "_assert_send_capacity", lambda account: None))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: dict(_PENDING_REVIEW)))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(server.reviews_db, "claim_send", lambda *args: True))
        stack.enter_context(patched(server.send_capacity_db, "reserve", lambda *args: True))
        stack.enter_context(patched(server.send_capacity_db, "complete", lambda *args: True))
        stack.enter_context(patched(server.reviews_db, "release_send_claim", lambda aid, rid: True))
        stack.enter_context(patched(server.gmail, "get_thread", lambda *a, **k: {"messages": []}))
        stack.enter_context(patched(server.gmail, "get_own_addresses", lambda *a: ({"me@x.com"}, False)))
        stack.enter_context(patched(server.gmail, "get_latest_reply_with_history", lambda *a, **k: {"message_id": "m-1"}))
        stack.enter_context(patched(server.gmail, "find_bounce", lambda *a, **k: None))
        stack.enter_context(patched(server.gmail, "send_reply",
            lambda a, tid, to, body: sent.update(to=to, body=body)))
        stack.enter_context(patched(server.reviews_db, "mark_sent",
            lambda aid, rid, body: sent.update(marked=(rid, body))))
        assert server.send_reply(_srv_req(), 9, server.SendReplyBody(body="hi")) == {"ok": True}
    assert sent == {"to": "john@x.com", "body": "hi", "marked": (9, "hi")}


def test_send_reply_fails_closed_when_gmail_cannot_revalidate_candidate():
    import server
    released = []
    sent = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server, "_assert_send_capacity", lambda account: None))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: dict(_PENDING_REVIEW)))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(server.reviews_db, "claim_send", lambda *args: True))
        stack.enter_context(patched(server.send_capacity_db, "reserve", lambda *args: True))
        stack.enter_context(patched(server.reviews_db, "release_send_claim", lambda aid, rid: released.append(rid)))
        stack.enter_context(patched(server.gmail, "get_thread", lambda *a, **k: {"messages": []}))
        stack.enter_context(patched(server.gmail, "get_own_addresses", lambda *a: ({"me@x.com"}, False)))
        stack.enter_context(patched(server.gmail, "get_latest_reply_with_history", lambda *a, **k: None))
        stack.enter_context(patched(server.gmail, "send_reply", lambda *a, **k: sent.append(True)))
        try:
            server.send_reply(_srv_req(), 9, server.SendReplyBody(body="hi"))
            raise AssertionError("expected a fail-closed 409")
        except server.HTTPException as e:
            assert e.status_code == 409
            assert "re-validate" in e.detail
    assert released == [9]
    assert sent == []


def test_send_capacity_blocks_a_paid_inbox_at_fifty_sends():
    import server
    account = dict(ACCOUNT, plan="pilot")
    with patched(server.drafts_db, "count_sent_last_24_hours", lambda aid: 50):
        with pytest.raises(server.HTTPException) as exc:
            server._assert_send_capacity(account)
    assert exc.value.status_code == 409
    assert "50-send" in exc.value.detail


def test_send_reply_calls_mark_rewritten_when_payload_says_so():
    import server
    marked = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server, "_assert_send_capacity", lambda account: None))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: dict(_PENDING_REVIEW)))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(server.reviews_db, "claim_send", lambda *args: True))
        stack.enter_context(patched(server.send_capacity_db, "reserve", lambda *args: True))
        stack.enter_context(patched(server.reviews_db, "release_send_claim", lambda aid, rid: True))
        stack.enter_context(patched(server.gmail, "get_thread", lambda *a, **k: {"messages": []}))
        stack.enter_context(patched(server.gmail, "get_own_addresses", lambda *a: ({"me@x.com"}, False)))
        stack.enter_context(patched(server.gmail, "get_latest_reply_with_history", lambda *a, **k: {"message_id": "m-1"}))
        stack.enter_context(patched(server.gmail, "find_bounce", lambda *a, **k: None))
        stack.enter_context(patched(server.gmail, "send_reply", lambda a, tid, to, body: None))
        stack.enter_context(patched(server.reviews_db, "mark_sent", lambda aid, rid, body: None))
        stack.enter_context(patched(server.reviews_db, "mark_rewritten", lambda aid, rid: marked.append(rid)))
        result = server.send_reply(_srv_req(), 9, server.SendReplyBody(body="hi", rewritten=True))
    assert result == {"ok": True}
    assert marked == [9]


def test_send_reply_does_not_call_mark_rewritten_by_default():
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server, "_assert_send_capacity", lambda account: None))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: dict(_PENDING_REVIEW)))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(server.reviews_db, "claim_send", lambda *args: True))
        stack.enter_context(patched(server.send_capacity_db, "reserve", lambda *args: True))
        stack.enter_context(patched(server.reviews_db, "release_send_claim", lambda aid, rid: True))
        stack.enter_context(patched(server.gmail, "get_thread", lambda *a, **k: {"messages": []}))
        stack.enter_context(patched(server.gmail, "get_own_addresses", lambda *a: ({"me@x.com"}, False)))
        stack.enter_context(patched(server.gmail, "get_latest_reply_with_history", lambda *a, **k: {"message_id": "m-1"}))
        stack.enter_context(patched(server.gmail, "find_bounce", lambda *a, **k: None))
        stack.enter_context(patched(server.gmail, "send_reply", lambda a, tid, to, body: None))
        stack.enter_context(patched(server.reviews_db, "mark_sent", lambda aid, rid, body: None))
        stack.enter_context(patched(server.reviews_db, "mark_rewritten",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not mark rewritten without the flag"))))
        result = server.send_reply(_srv_req(), 9, server.SendReplyBody(body="hi"))
    assert result == {"ok": True}


def test_rewrite_reply_404_when_missing_or_handled():
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: None))
        try:
            server.rewrite_reply_draft(_srv_req(), 9, server.RewriteReplyBody(body="b", instruction="shorter"))
            raise AssertionError("expected 404")
        except server.HTTPException as e:
            assert e.status_code == 404


def test_rewrite_reply_404_when_flagged():
    """SENDABLE_STATUSES, not VISIBLE_STATUSES: a flagged review has nothing
    drafted yet to adjust until the sender is confirmed, so it must be
    excluded from rewrite the same way it's excluded from send."""
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: dict(_FLAGGED_REVIEW)))
        try:
            server.rewrite_reply_draft(_srv_req(), 9, server.RewriteReplyBody(body="b", instruction="shorter"))
            raise AssertionError("expected 404 for a flagged review")
        except server.HTTPException as e:
            assert e.status_code == 404


def test_rewrite_reply_400_on_blank_instruction():
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: dict(_PENDING_REVIEW)))
        try:
            server.rewrite_reply_draft(_srv_req(), 9, server.RewriteReplyBody(body="b", instruction="   "))
            raise AssertionError("expected 400")
        except server.HTTPException as e:
            assert e.status_code == 400


def test_rewrite_reply_429_scoped_per_review_not_per_account():
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: dict(_PENDING_REVIEW, id=rid)))
        stack.enter_context(patched(server, "start_job", lambda aid, fn: "job-x"))
        body = server.RewriteReplyBody(body="b", instruction="shorter")
        for _ in range(10):
            server.rewrite_reply_draft(_srv_req(), 9, body)  # exhaust the limit for review 9
        try:
            server.rewrite_reply_draft(_srv_req(), 9, body)
            raise AssertionError("expected 429 once the per-review limit is exhausted")
        except server.HTTPException as e:
            assert e.status_code == 429
        # A different review is unaffected -- proves the scoping, not just that some limit exists.
        assert server.rewrite_reply_draft(_srv_req(), 10, body) == {"job_id": "job-x"}


def test_rewrite_reply_job_uses_the_payloads_text_not_the_stored_row():
    """Regression test mirroring test_rewrite_draft_job_uses_the_payloads_text_not_the_stored_row:
    the stored review's draft_reply must never be what gets rewritten, only
    the operator's current (payload) text."""
    import server
    captured = {}
    stored = dict(_PENDING_REVIEW, draft_reply="STALE stored reply")
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.ratelimit, "check", lambda *a, **k: True))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: dict(stored)))
        stack.enter_context(patched(server.agent, "rewrite_reply",
            lambda account, name, company, customer_reply, draft_text, instruction:
                captured.update(draft_text=draft_text) or "rewritten reply"))
        captured_fn = {}
        stack.enter_context(patched(server, "start_job", lambda aid, fn: captured_fn.update(fn=fn) or "job-1"))
        server.rewrite_reply_draft(
            _srv_req(), 9,
            server.RewriteReplyBody(body="fresh typed reply", instruction="shorter"),
        )
        result = captured_fn["fn"]()
    assert captured["draft_text"] == "fresh typed reply"
    assert result == {"body": "rewritten reply"}


def test_rewrite_reply_never_calls_plans_check():
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.ratelimit, "check", lambda *a, **k: True))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: dict(_PENDING_REVIEW)))
        stack.enter_context(patched(server.agent, "rewrite_reply", lambda *a, **k: "b"))
        stack.enter_context(patched(server.plans, "check",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not check quota"))))
        captured_fn = {}
        stack.enter_context(patched(server, "start_job", lambda aid, fn: captured_fn.update(fn=fn) or "job-1"))
        server.rewrite_reply_draft(_srv_req(), 9, server.RewriteReplyBody(body="b", instruction="shorter"))
        result = captured_fn["fn"]()
    assert result == {"body": "b"}


def test_rewrite_reply_never_writes_to_reviews_db():
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.ratelimit, "check", lambda *a, **k: True))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: dict(_PENDING_REVIEW)))
        stack.enter_context(patched(server.agent, "rewrite_reply", lambda *a, **k: "b"))
        for method in ("mark_sent", "mark_rewritten", "add_review", "dismiss"):
            stack.enter_context(patched(server.reviews_db, method,
                lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"must not call reviews_db.{method}"))))
        captured_fn = {}
        stack.enter_context(patched(server, "start_job", lambda aid, fn: captured_fn.update(fn=fn) or "job-1"))
        server.rewrite_reply_draft(_srv_req(), 9, server.RewriteReplyBody(body="b", instruction="shorter"))
        captured_fn["fn"]()  # must not raise


def test_confirm_sender_404_when_not_flagged():
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: dict(_PENDING_REVIEW)))
        try:
            server.confirm_reply_sender(_srv_req(), 9)
            raise AssertionError("expected 404 for a non-flagged review")
        except server.HTTPException as e:
            assert e.status_code == 404


def _confirm_sender_env(stack, review, history=(), row=None):
    """Common patches for exercising _confirm_sender_job without a real
    Gmail/Sheets/Supabase/LLM. Returns the dict draft/quota calls land in."""
    import server
    calls = {}
    stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
    stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: dict(review)))
    stack.enter_context(patched(server.sheets, "get_all_rows",
        lambda a: [(review["row_index"], row if row is not None else _row(company="Acme"))]))
    stack.enter_context(patched(server.gmail, "get_own_addresses", lambda a: ({"me@me.com"}, False)))
    stack.enter_context(patched(server.gmail, "get_history_before",
        lambda a, tid, mid, email, own: list(history)))
    stack.enter_context(patched(server.plans, "check", lambda a, bucket: None))
    stack.enter_context(patched(server.agent, "draft_reply",
        lambda a, name, company, reply, hist: calls.setdefault("draft", []).append(1) or "Here's a reply."))
    return calls


def test_confirm_sender_job_happy_path_drafts_and_confirms():
    import server
    with contextlib.ExitStack() as stack:
        calls = _confirm_sender_env(stack, _FLAGGED_REVIEW)
        confirmed = {}
        stack.enter_context(patched(server.reviews_db, "confirm_sender",
            lambda aid, rid, draft_reply="", validator_problems=None: confirmed.update(id=rid, draft=draft_reply) or True))
        result = server._confirm_sender_job(ACCOUNT, dict(_FLAGGED_REVIEW))
    assert result == {"warning": None}
    assert confirmed == {"id": 9, "draft": "Here's a reply."}
    assert calls["draft"] == [1]


def test_confirm_sender_job_does_not_read_sheets_without_a_row_index():
    import server
    for missing_row in (None, reviews_db.LEGACY_NO_SHEET_ROW):
        review = dict(_FLAGGED_REVIEW, row_index=missing_row)
        with contextlib.ExitStack() as stack:
            calls = _confirm_sender_env(stack, review)
            stack.enter_context(patched(
                server.sheets, "get_all_rows",
                lambda a: (_ for _ in ()).throw(
                    AssertionError("a Gmail-only review must not read Sheets")
                ),
            ))
            confirmed = {}
            stack.enter_context(patched(
                server.reviews_db, "confirm_sender",
                lambda aid, rid, draft_reply="", validator_problems=None:
                    confirmed.update(id=rid, draft=draft_reply) or True,
            ))
            result = server._confirm_sender_job(ACCOUNT, review)
        assert result == {"warning": None}
        assert confirmed == {"id": 9, "draft": "Here's a reply."}
        assert calls["draft"] == [1]


def test_confirm_sender_job_quota_exceeded_confirms_with_empty_draft_and_the_rich_message():
    import server
    exc = plans.QuotaExceeded("trial", usage.UNIT_DRAFT_REPLY, 10, 10, "Your Trial plan includes 10 reply drafts.")
    with contextlib.ExitStack() as stack:
        _confirm_sender_env(stack, _FLAGGED_REVIEW)

        def boom(a, bucket):
            raise exc
        stack.enter_context(patched(server.plans, "check", boom))
        confirmed = {}
        stack.enter_context(patched(server.reviews_db, "confirm_sender",
            lambda aid, rid, draft_reply="", validator_problems=None: confirmed.update(draft=draft_reply) or True))
        result = server._confirm_sender_job(ACCOUNT, dict(_FLAGGED_REVIEW))
    assert result == {"warning": str(exc)}
    assert confirmed == {"draft": ""}, "quota exceeded must still confirm the sender, just with no draft"


def test_confirm_sender_job_drafting_failure_confirms_anyway():
    import server
    with contextlib.ExitStack() as stack:
        _confirm_sender_env(stack, _FLAGGED_REVIEW)

        def boom(a, name, company, reply, hist):
            raise RuntimeError("provider down")
        stack.enter_context(patched(server.agent, "draft_reply", boom))
        confirmed = {}
        stack.enter_context(patched(server.reviews_db, "confirm_sender",
            lambda aid, rid, draft_reply="", validator_problems=None: confirmed.update(draft=draft_reply) or True))
        result = server._confirm_sender_job(ACCOUNT, dict(_FLAGGED_REVIEW))
    assert "Confirmed, but drafting failed" in result["warning"]
    assert confirmed == {"draft": ""}


def test_confirm_sender_job_skips_drafting_if_already_handled():
    """A double-click, or the operator dismissing the review while an earlier
    click's job is still running, must not spend a quota check and an LLM
    call on a write that's always going to be refused."""
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server.reviews_db, "get_review",
            lambda aid, rid: dict(_FLAGGED_REVIEW, status="dismissed")))
        stack.enter_context(patched(server.plans, "check",
            lambda a, bucket: (_ for _ in ()).throw(AssertionError("must not check quota"))))
        stack.enter_context(patched(server.agent, "draft_reply",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not draft"))))
        result = server._confirm_sender_job(ACCOUNT, dict(_FLAGGED_REVIEW))
    assert result == {"warning": "This review was handled elsewhere while confirming."}


def test_confirm_sender_job_discards_the_draft_if_dismissed_mid_flight():
    """Dismissed between the re-check and confirm_sender's write: the draft
    that was just generated has nowhere to go. Exercises that the job actually
    reads confirm_sender's return value rather than assuming success."""
    import server
    with contextlib.ExitStack() as stack:
        _confirm_sender_env(stack, _FLAGGED_REVIEW)
        stack.enter_context(patched(server.reviews_db, "confirm_sender",
            lambda aid, rid, draft_reply="", validator_problems=None: False))
        result = server._confirm_sender_job(ACCOUNT, dict(_FLAGGED_REVIEW))
    assert result == {"warning": "This review was handled elsewhere while confirming."}


def test_confirm_sender_job_propagates_a_history_read_failure():
    """Unlike a drafting failure, a thread/sheet-row that can no longer be
    read is a real job error -- confirming the sender should not silently
    succeed if the context it needs couldn't be read at all."""
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: dict(_FLAGGED_REVIEW)))
        stack.enter_context(patched(server.sheets, "get_all_rows",
            lambda a: (_ for _ in ()).throw(RuntimeError("sheet unreachable"))))
        try:
            server._confirm_sender_job(ACCOUNT, dict(_FLAGGED_REVIEW))
            raise AssertionError("expected the sheet-read failure to propagate")
        except RuntimeError:
            pass


def test_confirm_reply_sender_endpoint_starts_a_job():
    import server
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: dict(_FLAGGED_REVIEW)))
        stack.enter_context(patched(server, "start_job", lambda aid, fn: "job-42"))
        assert server.confirm_reply_sender(_srv_req(), 9) == {"job_id": "job-42"}


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
    assert "promise detection" in plan["note"]


# ------------------------------------------------------------------------ auth

def test_session_token_roundtrip_and_tamper():
    token = auth.create_session_token("acct-42", ttl_seconds=60)
    assert auth.verify_session_token(token) == "acct-42"
    assert auth.verify_session_token(token[:-1] + ("0" if token[-1] != "0" else "1")) is None
    assert auth.verify_session_token(None) is None
    assert auth.verify_session_token("a.b") is None
    expired = auth.create_session_token("acct-42", ttl_seconds=-1)
    assert auth.verify_session_token(expired) is None


def test_oauth_state_roundtrip():
    state = auth.create_oauth_state()
    assert auth.verify_oauth_state(state)
    assert not auth.verify_oauth_state("123.deadbeef")
    assert not auth.verify_oauth_state(auth.create_session_token("x"))  # two dots


def test_public_product_theme_is_available_before_login():
    import server

    assert "/static/product-theme.css" in server.PUBLIC_PATHS


def test_oauth_callback_rejects_missing_browser_state_cookie_with_safe_retry():
    import server
    from types import SimpleNamespace

    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server.auth, "create_oauth_state", lambda: "signed-state"))
        stack.enter_context(patched(server.auth, "verify_oauth_state", lambda state: state == "signed-state"))
        stack.enter_context(patched(
            server.google_auth, "build_login_auth_url",
            lambda state: f"https://accounts.google.test/?state={state}",
        ))
        response = server.google_login_start("/outreach")
        assert "oauth_state=signed-state" in response.headers["set-cookie"]
        response = server.google_login_callback(
            SimpleNamespace(cookies={}),
            state="signed-state",
            code="code",
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/login?google_error=oauth_state_expired"
    assert "oauth_state=" in response.headers["set-cookie"]
    assert response.headers["cache-control"] == "private, no-store"


def test_expired_oauth_callback_preserves_safe_next_without_exchanging_code():
    import server
    from types import SimpleNamespace

    expired_state = auth.create_oauth_state(ttl_seconds=-1)
    with patched(
        server.google_auth,
        "exchange_login_code",
        lambda code: pytest.fail("an expired callback must never exchange its code"),
    ):
        response = server.google_login_callback(
            SimpleNamespace(cookies={
                server.OAUTH_STATE_COOKIE: expired_state,
                server.NEXT_COOKIE_NAME: "/outreach?tab=inbox",
            }),
            state=expired_state,
            code="discard-me",
        )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/login?google_error=oauth_state_expired&next=%2Foutreach%3Ftab%3Dinbox"
    )


def test_google_grant_state_binds_account_and_capability():
    state = auth.create_google_grant_state("acct-42", "monitor", ttl_seconds=60)
    assert auth.verify_google_grant_state(state) == ("acct-42", "monitor")
    tampered = state[:-1] + ("0" if state[-1] != "0" else "1")
    assert auth.verify_google_grant_state(tampered) is None
    assert auth.verify_google_grant_state(auth.create_session_token("acct-42")) is None


def test_session_signing_key_split_preserves_unsubscribe_but_not_sessions():
    """SESSION_SIGNING_KEY defaults to APP_SECRET_KEY, so every token signed
    today is effectively signed under APP_SECRET_KEY. Once an operator later
    sets SESSION_SIGNING_KEY to something distinct, unsubscribe links --
    already sent, no expiry, must work forever -- have to keep verifying.
    Sessions and OAuth state are short-lived and deliberately do not: a
    rotation logging everyone out is correct, not a defect."""
    unsub = auth.create_unsubscribe_token("acct-9", "p@x.com")
    session = auth.create_session_token("acct-9", ttl_seconds=60)
    state = auth.create_oauth_state()
    with patched(auth, "SESSION_SIGNING_KEY", "a-new-distinct-signing-key"):
        assert auth.verify_unsubscribe_token(unsub) == ("acct-9", "p@x.com")
        assert auth.verify_session_token(session) is None
        assert not auth.verify_oauth_state(state)


# -------------------------------------------------------------- google_auth

def test_google_capabilities_are_incremental_and_legacy_modify_still_works():
    account = {"id": "acct-7", "google_token": "ciphertext"}
    monitor = json.dumps({"scopes": [config.GMAIL_MONITOR_SCOPES[0]]})
    with patched(accounts_db, "get_google_token", lambda account: monitor):
        assert google_auth.capability_status(account) == {
            "monitor": True, "send": False, "sheets": False,
        }
    legacy = json.dumps({"scopes": [google_auth.LEGACY_GMAIL_MODIFY, *config.SHEETS_SCOPES]})
    with patched(accounts_db, "get_google_token", lambda account: legacy):
        assert google_auth.capability_status(account) == {
            "monitor": True, "send": True, "sheets": True,
        }


def test_incremental_google_grant_preserves_refresh_token_and_scope_union():
    existing = json.dumps({
        "refresh_token": "keep-me", "scopes": config.GMAIL_MONITOR_SCOPES,
        "google_account_email": "owner@example.com",
    })
    granted = json.dumps({
        "token": "new-access", "scopes": config.GMAIL_SEND_SCOPES,
        "google_account_email": "owner@example.com",
    })
    merged = json.loads(google_auth.merge_token_json(existing, granted))
    assert merged["refresh_token"] == "keep-me"
    assert set(merged["scopes"]) == set(config.GMAIL_MONITOR_SCOPES + config.GMAIL_SEND_SCOPES)


def test_incremental_google_grant_rejects_a_different_mailbox():
    existing = json.dumps({
        "refresh_token": "keep-me",
        "scopes": config.GMAIL_MONITOR_SCOPES,
        "google_account_email": "owner@example.com",
    })
    granted = json.dumps({
        "token": "attacker-or-wrong-mailbox",
        "scopes": config.GMAIL_SEND_SCOPES,
        "google_account_email": "other@example.com",
    })
    with pytest.raises(google_auth.GoogleIdentityMismatch):
        google_auth.merge_token_json(existing, granted)


def test_sheets_only_grant_verifies_identity_from_the_id_token_not_gmail():
    """A sheets-only consent carries no Gmail scope, so the old unconditional
    getProfile call answered 403 and the whole connect flow died with a
    misleading error. Identity comes from the exchanged ID token instead --
    and the Gmail API is never touched."""
    claims = base64.urlsafe_b64encode(
        json.dumps({"email": "Owner@Example.com"}).encode()
    ).decode().rstrip("=")
    fake_creds = type("Creds", (), {})()
    fake_creds.scopes = list(config.SHEETS_SCOPES)
    fake_creds.to_json = lambda: json.dumps({"token": "t", "scopes": config.SHEETS_SCOPES})
    fake_creds.id_token = f"header.{claims}.signature"

    class FakeFlow:
        credentials = fake_creds

        def fetch_token(self, code):
            assert code == "code-1"

    def gmail_must_not_be_called(*a, **k):
        raise AssertionError("a sheets-only grant must not call the Gmail API")

    with patched(google_auth, "_flow", lambda capability: FakeFlow()), \
            patched(google_auth, "build", gmail_must_not_be_called):
        token = json.loads(google_auth.exchange_code("code-1", "sheets"))
    assert token["google_account_email"] == "owner@example.com"


def test_mail_grant_still_verifies_identity_via_gmail_profile():
    """A grant carrying Gmail scope keeps the stronger identity source: the
    live mailbox profile, not just the ID-token claim."""
    fake_creds = type("Creds", (), {})()
    fake_creds.scopes = list(config.GMAIL_MONITOR_SCOPES)
    fake_creds.to_json = lambda: json.dumps(
        {"token": "t", "scopes": config.GMAIL_MONITOR_SCOPES}
    )
    fake_creds.id_token = None

    class FakeService:
        def users(self):
            return self

        def getProfile(self, userId):
            return self

        def execute(self):
            return {"emailAddress": "owner@example.com"}

    class FakeFlow:
        credentials = fake_creds

        def fetch_token(self, code):
            pass

    with patched(google_auth, "_flow", lambda capability: FakeFlow()), \
            patched(google_auth, "build",
                    lambda *a, **k: FakeService()) as _build:
        token = json.loads(google_auth.exchange_code("code-1"))
    assert token["google_account_email"] == "owner@example.com"


def test_google_connect_pauses_only_new_monitoring_when_precision_is_over_twenty_percent():
    import server

    class Request:
        state = type("State", (), {"account_id": "acct-7"})()

    account = {"id": "acct-7"}
    stopped = {
        "false_positive_rate": 0.25,
        "precision_status": "stop",
        "stop_onboarding": True,
    }
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda request: account))
        stack.enter_context(patched(
            server.google_auth, "capability_status",
            lambda account: {"monitor": False, "send": False, "sheets": False},
        ))
        stack.enter_context(patched(server.commitments_db, "onboarding_precision", lambda: stopped))
        with pytest.raises(server.HTTPException) as exc:
            server.google_connect(Request(), "monitor")
    assert exc.value.status_code == 503
    assert "25%" in str(exc.value.detail)

    calls = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda request: account))
        stack.enter_context(patched(
            server.google_auth, "capability_status",
            lambda account: {"monitor": True, "send": False, "sheets": False},
        ))
        stack.enter_context(patched(server.commitments_db, "onboarding_precision", lambda: stopped))
        stack.enter_context(patched(
            server.auth, "create_google_grant_state",
            lambda aid, capability, ttl: f"state:{aid}:{capability}",
        ))
        stack.enter_context(patched(
            server.google_auth, "build_auth_url",
            lambda state, capability: calls.append((state, capability)) or "https://google.test/auth",
        ))
        response = server.google_connect(Request(), "monitor")
        send_response = server.google_connect(Request(), "send")
    assert response.status_code in (302, 307)
    assert send_response.status_code in (302, 307)
    assert calls == [
        ("state:acct-7:monitor", "monitor"),
        ("state:acct-7:send", "send"),
    ]

def test_get_credentials_clears_token_on_fernet_key_rotation():
    """If the Fernet key derived from APP_SECRET_KEY is ever rotated, every
    previously stored Google token becomes permanently undecryptable --
    accounts_db.decrypt_secret raises InvalidToken, not RefreshError.
    get_credentials must treat that the same way as an unrefreshable token:
    clear it and tell the user to reconnect, not crash with a raw
    InvalidToken (which callers only know to catch as RuntimeError)."""
    from cryptography.fernet import InvalidToken

    account = {"id": "acct-7", "google_token": "irrelevant-ciphertext"}
    cleared = []

    def fake_get_google_token(acct):
        raise InvalidToken()

    def fake_set_google_token(account_id, token_json):
        cleared.append((account_id, token_json))

    with patched(accounts_db, "get_google_token", fake_get_google_token), \
         patched(accounts_db, "set_google_token", fake_set_google_token):
        try:
            google_auth.get_credentials(account)
        except RuntimeError as e:
            assert "reconnect Google" in str(e)
        else:
            raise AssertionError("InvalidToken must surface as RuntimeError")
    assert cleared == [("acct-7", None)]


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

def test_link_google_account_links_by_email_or_reuses_by_google_id():
    """link_or_create_google_account: matches an existing account by email
    (linking the google_id to it), or reuses one already linked to this
    google_id -- no email lookup at all in that case."""
    linked = {"id": "acct-9", "email": "u@corp.com", "google_id": "google-xyz"}

    class _Resp:
        data = [linked]

    class _Query:
        def update(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def is_(self, *a, **k):
            return self

        def execute(self):
            return _Resp()

    class _Client:
        def table(self, *a, **k):
            return _Query()

    with patched(accounts_db, "get_account_by_google_id", lambda gid: None), \
         patched(accounts_db, "get_account_by_email",
                 lambda e: {"id": "acct-9", "email": e}), \
         patched(accounts_db, "_get_client", lambda: _Client()):
        assert accounts_db.link_or_create_google_account("u@corp.com", "google-xyz") == linked

    # This google_id is already linked -> reuse, no email lookup at all.
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


def test_dns_safety_gate_blocks_authentication_failures_but_not_custom_dkim_warning():
    blocked = {
        "managed": False,
        "findings": [
            {"name": "SPF", "status": "warning", "detail": "Google is not authorised."},
            {"name": "DKIM", "status": "warning", "detail": "Custom selector not found."},
            {"name": "DMARC", "status": "missing", "detail": "No DMARC record."},
        ],
    }
    assert len(dns_check.safety_blockers(blocked)) == 2
    assert dns_check.safety_blockers({"managed": True, "findings": []}) == []


def test_domain_safety_fails_closed_when_dns_cannot_be_verified():
    account = dict(ACCOUNT, google_token="encrypted")
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(send_outreach.gmail, "get_sending_address", lambda account, capability="monitor": "owner@acme.test"))
        stack.enter_context(patched(send_outreach.dns_check, "check_domain", lambda domain: {
            "domain": domain,
            "managed": False,
            "findings": [
                {"name": "SPF", "status": "unknown", "detail": "DNS unavailable."},
                {"name": "DKIM", "status": "unknown", "detail": "DNS unavailable."},
                {"name": "DMARC", "status": "unknown", "detail": "DNS unavailable."},
            ],
            "summary": "Could not complete the DNS checks.",
        }))
        state = send_outreach.domain_safety(account)
    assert state["status"] == "blocked"
    assert state["send_blockers"]
    with pytest.raises(RuntimeError, match="domain-safety gate"):
        with patched(send_outreach, "domain_safety", lambda account: state):
            send_outreach.assert_domain_safe(account)


def test_export_thread_rows_include_a_gmail_handoff_url():
    import account_export

    rows = account_export._portable_rows(
        "tracked_threads", [{"thread_id": "abc/123", "email": "owner@example.com"}]
    )
    assert rows[0]["gmail_url"].endswith("abc%2F123")


def test_prepare_drafts_enforces_domain_safety_before_llm_work():
    account = dict(ACCOUNT, google_token="encrypted")
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(sheets, "require_full_header", lambda account: None))
        stack.enter_context(patched(send_outreach.bounces, "assert_sendable", lambda account: None))
        stack.enter_context(patched(send_outreach, "domain_safety", lambda account: {
            "status": "blocked",
            "send_blockers": ["SPF is missing."],
        }))
        with pytest.raises(RuntimeError, match="SPF is missing"):
            send_outreach.prepare_drafts(account)


def test_dns_safety_gate_blocks_authentication_failures_but_not_custom_dkim_warning():
    blocked = {
        "managed": False,
        "findings": [
            {"name": "SPF", "status": "warning", "detail": "Google is not authorised."},
            {"name": "DKIM", "status": "warning", "detail": "Custom selector not found."},
            {"name": "DMARC", "status": "missing", "detail": "No DMARC record."},
        ],
    }
    assert len(dns_check.safety_blockers(blocked)) == 2
    assert dns_check.safety_blockers({"managed": True, "findings": []}) == []


def test_domain_safety_fails_closed_when_dns_cannot_be_verified():
    account = dict(ACCOUNT, google_token="encrypted")
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(send_outreach.gmail, "get_sending_address", lambda account, capability="monitor": "owner@acme.test"))
        stack.enter_context(patched(send_outreach.dns_check, "check_domain", lambda domain: {
            "domain": domain,
            "managed": False,
            "findings": [
                {"name": "SPF", "status": "unknown", "detail": "DNS unavailable."},
                {"name": "DKIM", "status": "unknown", "detail": "DNS unavailable."},
                {"name": "DMARC", "status": "unknown", "detail": "DNS unavailable."},
            ],
            "summary": "Could not complete the DNS checks.",
        }))
        state = send_outreach.domain_safety(account)
    assert state["status"] == "blocked"
    assert state["send_blockers"]
    with pytest.raises(RuntimeError, match="domain-safety gate"):
        with patched(send_outreach, "domain_safety", lambda account: state):
            send_outreach.assert_domain_safe(account)


def test_prepare_drafts_enforces_domain_safety_before_llm_work():
    account = dict(ACCOUNT, google_token="encrypted")
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(sheets, "require_full_header", lambda account: None))
        stack.enter_context(patched(send_outreach.bounces, "assert_sendable", lambda account: None))
        stack.enter_context(patched(send_outreach, "domain_safety", lambda account: {
            "status": "blocked",
            "send_blockers": ["SPF is missing."],
        }))
        with pytest.raises(RuntimeError, match="SPF is missing"):
            send_outreach.prepare_drafts(account)


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
        result = gmail.get_latest_reply_with_history(
            ACCOUNT, "t1", "john@x.com", OWN, thread=thread
        )
    assert found["code"] == "5.1.1"
    assert result is None, "the newest message is the daemon's, not the contact's"


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


def _schema_snapshot(*, missing=(), row_index_nullable=True,
                     missing_indexes=(), missing_rls=(), missing_constraints=()):
    missing = set(missing)
    return {
        "version": accounts_db.schema_contract.BASELINE_MIGRATION,
        "columns": {
            f"{requirement.table}.{requirement.column}":
                f"{requirement.table}.{requirement.column}" not in missing
            for requirement in accounts_db.schema_contract.REQUIREMENTS
        },
        "nullable": {"reviews.row_index": row_index_nullable},
        "indexes": {
            name: name not in set(missing_indexes)
            for name in accounts_db.schema_contract.INDEX_REQUIREMENTS
        },
        "rls": {
            table: table not in set(missing_rls)
            for table in accounts_db.schema_contract.RLS_TABLES
        },
        "constraints": {
            name: name not in set(missing_constraints)
            for name in accounts_db.schema_contract.CONSTRAINT_REQUIREMENTS
        },
    }


def _healthy_outbox_snapshot():
    return {
        "version": accounts_db.schema_contract.OUTBOX_MIGRATION,
        "table": True,
        "rls": True,
        "ready_index": True,
        "dedupe_constraint": True,
        "status_constraint": True,
        "event_constraint": True,
        "source_constraint": True,
        "rpcs": True,
        "service_role_execute": True,
        "client_execute_revoked": True,
    }


def _healthy_atomic_send_snapshot():
    return {
        "version": accounts_db.schema_contract.ATOMIC_SEND_MIGRATION,
        "table": True,
        "rls": True,
        "client_table_access_revoked": True,
        "operation_unique": True,
        "status_constraint": True,
        "active_index": True,
        "active_row_index": True,
        "recipient_index": True,
        "reservation_rpc": True,
        "duplicate_check_rpc": True,
        "stripe_rpc": True,
        "service_role_execute": True,
        "client_execute_revoked": True,
    }


def _healthy_promise_ledger_snapshot():
    return {
        "version": accounts_db.schema_contract.PROMISE_LEDGER_MIGRATION,
        "timezone_column": True,
        "events_table": True,
        "events_rls": True,
        "events_index": True,
        "events_unique": True,
        "events_service_role_select": True,
        "transition_rpc": True,
        "due_rpc": True,
        "service_role_execute": True,
        "client_execute_revoked": True,
    }


def test_schema_check_names_the_missing_column_and_its_consequence():
    """The window this closes: bounce_ack_count is only needed by an account
    already paused for a bad bounce rate, so without a boot-time warning the
    first person to discover it is a customer having a bad day whose only exit is
    deleting sheet rows -- the remediation the pause message stopped giving."""
    with patched(
        accounts_db.schema_contract, "_contract_snapshot",
        lambda: _schema_snapshot(missing={"accounts.bounce_ack_count"}),
    ), patched(
        accounts_db.schema_contract, "_outbox_snapshot", _healthy_outbox_snapshot,
    ), patched(
        accounts_db.schema_contract, "_atomic_send_snapshot", _healthy_atomic_send_snapshot,
    ), patched(
        accounts_db.schema_contract, "_promise_ledger_snapshot", _healthy_promise_ledger_snapshot,
    ):
        warnings = accounts_db.check_schema()
    assert len(warnings) == 1, warnings
    assert "bounce_ack_count" in warnings[0]
    assert "20260827000000_sendkeep_baseline" in warnings[0]
    assert "bounce pauses" in warnings[0], "say what breaks, not just what is absent"


def test_schema_contract_uses_one_protected_rpc_snapshot():
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"version": accounts_db.schema_contract.BASELINE_MIGRATION}

    with patched(
        accounts_db.schema_contract.requests,
        "post",
        lambda *args, **kwargs: calls.append((args, kwargs)) or Response(),
    ):
        snapshot = accounts_db.schema_contract._contract_snapshot(timeout_seconds=3)

    assert snapshot["version"] == accounts_db.schema_contract.BASELINE_MIGRATION
    assert calls[0][0][0].endswith("/rest/v1/rpc/sendkeep_schema_contract")
    assert calls[0][1]["json"] == {}
    assert calls[0][1]["timeout"] == 3
    assert calls[0][1]["headers"]["Authorization"].startswith("Bearer ")


def test_schema_check_names_degraded_classification_when_missing():
    """add_review is the insert path for every review, including a normal
    on-sheet reply -- unlike bounce_ack_count, whose absence only breaks one
    button, a missing degraded_classification column fails every insert and
    stops reply detection entirely. It has to be onboarded through the boot
    check like reviews' other two columns, not left to a manual README step."""
    with patched(
        accounts_db.schema_contract, "_contract_snapshot",
        lambda: _schema_snapshot(missing={"reviews.degraded_classification"}),
    ), patched(
        accounts_db.schema_contract, "_outbox_snapshot", _healthy_outbox_snapshot,
    ), patched(
        accounts_db.schema_contract, "_atomic_send_snapshot", _healthy_atomic_send_snapshot,
    ), patched(
        accounts_db.schema_contract, "_promise_ledger_snapshot", _healthy_promise_ledger_snapshot,
    ):
        warnings = accounts_db.check_schema()
    assert len(warnings) == 1, warnings
    assert "degraded_classification" in warnings[0]
    assert "20260827000000_sendkeep_baseline" in warnings[0]


def test_schema_check_detects_the_live_row_index_nullability_regression():
    with patched(
        accounts_db.schema_contract, "_contract_snapshot",
        lambda: _schema_snapshot(row_index_nullable=False),
    ), patched(
        accounts_db.schema_contract, "_outbox_snapshot", _healthy_outbox_snapshot,
    ), patched(
        accounts_db.schema_contract, "_atomic_send_snapshot", _healthy_atomic_send_snapshot,
    ), patched(
        accounts_db.schema_contract, "_promise_ledger_snapshot", _healthy_promise_ledger_snapshot,
    ):
        warnings = accounts_db.check_schema()
    assert len(warnings) == 1
    assert "reviews.row_index must be nullable" in warnings[0]


def test_schema_check_detects_missing_dedupe_index():
    with patched(
        accounts_db.schema_contract, "_contract_snapshot",
        lambda: _schema_snapshot(missing_indexes={"worker_events_run_dedupe_unique_idx"}),
    ), patched(
        accounts_db.schema_contract, "_outbox_snapshot", _healthy_outbox_snapshot,
    ), patched(
        accounts_db.schema_contract, "_atomic_send_snapshot", _healthy_atomic_send_snapshot,
    ), patched(
        accounts_db.schema_contract, "_promise_ledger_snapshot", _healthy_promise_ledger_snapshot,
    ):
        warnings = accounts_db.check_schema()
    assert len(warnings) == 1
    assert "worker_events_run_dedupe_unique_idx" in warnings[0]
    assert "idempotent" in warnings[0]


def test_schema_check_detects_missing_rls_and_constraint_controls():
    with patched(
        accounts_db.schema_contract, "_contract_snapshot",
        lambda: _schema_snapshot(
            missing_rls={"worker_events"},
            missing_constraints={"commitments_status_check"},
        ),
    ), patched(
        accounts_db.schema_contract, "_outbox_snapshot", _healthy_outbox_snapshot,
    ), patched(
        accounts_db.schema_contract, "_atomic_send_snapshot", _healthy_atomic_send_snapshot,
    ), patched(
        accounts_db.schema_contract, "_promise_ledger_snapshot", _healthy_promise_ledger_snapshot,
    ):
        warnings = accounts_db.check_schema()
    assert len(warnings) == 2
    assert any("worker_events" in warning and "RLS" in warning for warning in warnings)
    assert any("commitments_status_check" in warning for warning in warnings)


class _AccountsTable:
    """Minimal PostgREST-shaped fake supporting select/update/eq, enough for
    list_accounts_with_a_sheet and set_worker_heartbeat."""

    def __init__(self, rows):
        self.rows = rows
        self._eq = []
        self._mode = None
        self._values = None

    def table(self, name):
        return self

    def select(self, cols):
        self._mode = "select"
        return self

    def update(self, values):
        self._mode = "update"
        self._values = values
        return self

    def eq(self, col, val):
        self._eq.append((col, val))
        return self

    def execute(self):
        eq = self._eq
        self._eq = []
        matched = [r for r in self.rows if all(r.get(c) == v for c, v in eq)]
        if self._mode == "update":
            for r in matched:
                r.update(self._values)
        return type("R", (), {"data": matched})()


def test_list_accounts_with_a_sheet_excludes_no_sheet_accounts():
    fake = _AccountsTable([
        {"id": "a1", "google_sheet_id": "sheet-1", "google_token": "tok"},
        {"id": "a2", "google_sheet_id": "", "google_token": "tok"},
        {"id": "a3", "google_sheet_id": None, "google_token": "tok"},
    ])
    with patched(accounts_db, "_get_client", lambda: fake):
        result = accounts_db.list_accounts_with_a_sheet()
    assert [a["id"] for a in result] == ["a1"]


def test_list_accounts_with_a_sheet_includes_accounts_with_no_token():
    """Regression test for the finding that shaped this function: a dead
    Google token must not remove an account from the watcher's list, or a
    stale worker_heartbeat_at becomes indistinguishable from the worker
    itself being down. google_token is checked by the caller per account,
    not filtered out here."""
    fake = _AccountsTable([{"id": "a1", "google_sheet_id": "sheet-1", "google_token": None}])
    with patched(accounts_db, "_get_client", lambda: fake):
        result = accounts_db.list_accounts_with_a_sheet()
    assert [a["id"] for a in result] == ["a1"]


def test_list_accounts_for_worker_includes_active_tracked_threads_without_a_sheet():
    class WorkerTable:
        def __init__(self):
            self.accounts = [
                {"id": "sheet-account", "google_sheet_id": "sheet-1"},
                {"id": "thread-account", "google_sheet_id": ""},
                {"id": "idle-account", "google_sheet_id": None},
            ]
            self.tracked = [{"account_id": "thread-account", "status": "active"}]
            self.current = "accounts"
            self.filters = []

        def table(self, name):
            self.current = "tracked" if name == "tracked_threads" else "accounts"
            return self

        def select(self, cols): return self

        def eq(self, col, value):
            self.filters.append((col, value))
            return self

        def execute(self):
            rows = self.accounts if self.current == "accounts" else self.tracked
            rows = [
                row for row in rows
                if all(row.get(col) == value for col, value in self.filters)
            ]
            self.filters = []
            return type("R", (), {"data": rows})()

    previous = os.environ.get("TRACKED_THREADS_ENABLED")
    try:
        os.environ["TRACKED_THREADS_ENABLED"] = "1"
        fake = WorkerTable()
        with patched(accounts_db, "_get_client", lambda: fake):
            result = accounts_db.list_accounts_for_worker()
    finally:
        if previous is None:
            os.environ.pop("TRACKED_THREADS_ENABLED", None)
        else:
            os.environ["TRACKED_THREADS_ENABLED"] = previous
    assert [account["id"] for account in result] == ["sheet-account", "thread-account"]


def test_list_accounts_for_worker_includes_connected_gmail_without_a_sheet():
    fake = _AccountsTable([
        {"id": "gmail-only", "google_sheet_id": "", "google_token": "tok"},
        {"id": "disconnected", "google_sheet_id": "", "google_token": None},
        {"id": "sheet-account", "google_sheet_id": "sheet-1", "google_token": "tok"},
    ])
    previous_discovery = os.environ.get("GMAIL_THREAD_DISCOVERY_ENABLED")
    previous_tracking = os.environ.get("TRACKED_THREADS_ENABLED")
    try:
        os.environ["GMAIL_THREAD_DISCOVERY_ENABLED"] = "1"
        os.environ["TRACKED_THREADS_ENABLED"] = "1"
        with patched(accounts_db, "_get_client", lambda: fake):
            result = accounts_db.list_accounts_for_worker()
    finally:
        if previous_discovery is None:
            os.environ.pop("GMAIL_THREAD_DISCOVERY_ENABLED", None)
        else:
            os.environ["GMAIL_THREAD_DISCOVERY_ENABLED"] = previous_discovery
        if previous_tracking is None:
            os.environ.pop("TRACKED_THREADS_ENABLED", None)
        else:
            os.environ["TRACKED_THREADS_ENABLED"] = previous_tracking
    assert [account["id"] for account in result] == ["gmail-only", "sheet-account"]


def test_set_worker_heartbeat_records_and_clears_last_error():
    fake = _AccountsTable([{"id": "a1", "worker_heartbeat_at": None, "last_error": None}])
    with patched(accounts_db, "_get_client", lambda: fake):
        accounts_db.set_worker_heartbeat("a1", error="Google disconnected — reconnect in Settings")
        assert fake.rows[0]["last_error"] == "Google disconnected — reconnect in Settings"
        assert fake.rows[0]["worker_heartbeat_at"], "heartbeat must be set even when there's an error"

        accounts_db.set_worker_heartbeat("a1", error=None)
        assert fake.rows[0]["last_error"] is None, "a later success must clear the earlier error"


def test_set_worker_heartbeat_is_best_effort():
    """Matches usage.record's own stated convention: metering (and this,
    which is diagnostic in the same spirit) must never break the action
    being metered/observed. Most calls to this happen on the failure path,
    where something has already gone wrong -- a second failure here must
    not replace the real error or crash the caller's loop."""
    def boom():
        raise RuntimeError("supabase down")
    with patched(accounts_db, "_get_client", boom):
        accounts_db.set_worker_heartbeat("a1", error="whatever")  # must not raise


def test_schema_check_is_silent_on_a_migrated_database():
    with patched(
        accounts_db.schema_contract, "_contract_snapshot", _schema_snapshot
    ), patched(
        accounts_db.schema_contract, "_outbox_snapshot", _healthy_outbox_snapshot
    ), patched(
        accounts_db.schema_contract, "_atomic_send_snapshot", _healthy_atomic_send_snapshot
    ), patched(
        accounts_db.schema_contract, "_promise_ledger_snapshot", _healthy_promise_ledger_snapshot
    ):
        assert accounts_db.check_schema() == []


def test_schema_check_does_not_blame_a_migration_for_an_unreachable_database():
    """A network failure would otherwise report every column as missing and send
    someone off to run SQL they do not need."""
    with patched(
        accounts_db.schema_contract, "_contract_snapshot",
        lambda: (_ for _ in ()).throw(RuntimeError("connection refused")),
    ):
        warnings = accounts_db.check_schema()
    assert len(warnings) == 1
    assert "Could not verify" in warnings[0]
    assert "Apply Supabase migration" not in warnings[0]


def test_a_degraded_control_warns_locally_but_never_hides_other_probes():
    """Local mode stays repairable, and one failed probe cannot hide another."""
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


def test_production_refuses_to_boot_with_an_incompatible_schema():
    import server
    previous = os.environ.get("APP_ENV")
    try:
        os.environ["APP_ENV"] = "production"
        with patched(
            server.accounts_db, "check_schema",
            lambda: ["reviews.sent_at is missing"],
        ), patched(server.ratelimit, "enforcement_warnings", lambda: []):
            with pytest.raises(RuntimeError, match="schema is incompatible"):
                server.report_degraded_controls()
    finally:
        if previous is None:
            os.environ.pop("APP_ENV", None)
        else:
            os.environ["APP_ENV"] = previous


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
        rows = _bounce_rows(40, 4)
        stack.enter_context(patched(sheets, "get_all_rows", lambda account: rows))
        stack.enter_context(patched(drafts_db, "require_send_log", lambda: None))
        stack.enter_context(patched(drafts_db, "list_pending_drafts", lambda aid: [{
            "id": 1, "row_index": rows[0][0], "email": rows[0][1][sheets.COL_EMAIL],
            "subject": "S", "body": "b",
        }]))
        stack.enter_context(patched(gmail, "send_email", lambda *a, **k: sent.append(a) or "t"))
        result = send_outreach.send_all_prepared(ACCOUNT)
    assert sent == [], "no email may leave while sending is paused"
    assert result["sent"] == 0 and len(result["failed"]) == 1


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
    assert plans.daily_send_limit_for({"plan": "pilot"}) == 50
    assert plans.daily_send_limit_for({"plan": "team"}) == 50
    assert plans.daily_send_limit_for({}) == 25, "unmigrated row reads as trial"


def test_an_explicit_deployment_limit_can_lower_but_not_raise_the_plan_cap():
    """Self-hosted installs may choose a stricter cap, never a looser one."""
    with patched(config, "DAILY_SEND_LIMIT_OVERRIDE", 5):
        assert plans.daily_send_limit_for({"plan": "team"}) == 5
    with patched(config, "DAILY_SEND_LIMIT_OVERRIDE", 100):
        assert plans.daily_send_limit_for({"plan": "team"}) == 50


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
            assert "Trial" in str(e) and "Upgrade to Inbox" in str(e), \
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

    assert plans.PLANS["trial"].daily_send_limit == 25
    assert plans.PLANS["trial"].lead_allowance == 50
    assert plans.PLANS["trial"].lead_window == "lifetime", \
        "the page writes the trial's leads without a /mo suffix"

    assert plans.PLANS["pilot"].lead_allowance == 500
    assert plans.PLANS["pilot"].lead_window == "month"

    assert "One follow-up reminder per contact" in page
    assert plans.PLANS["pilot"].monthly_replies is None

    assert "Capped first-touch trial" in page
    assert "Gmail Sent-thread discovery" in page
    assert "sourced leads" not in page

    assert "$29/mo" in page
    assert plans.PLANS["pilot"].price_monthly_usd == 29
    assert plans.PLANS["team"].price_monthly_usd == 49
    assert "$49/inbox/mo" in page
    assert "Agency pilot" in page


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
        "id": "evt_upgrade", "created": 100,
        "type": "checkout.session.completed",
        "data": {"object": {
            "payment_status": "paid",
            "client_reference_id": "acct-real",
            "subscription": "sub_real",
            "customer_email": "victim@example.com",
            "metadata": {"account_id": "acct-real", "plan": "pilot"},
        }},
    })
    assert change["account_id"] == "acct-real" and change["plan"] == "pilot"
    assert change["subscription_id"] == "sub_real"


def test_an_unpaid_checkout_session_grants_nothing():
    """Reaching the confirmation page is not paying."""
    import billing

    for status in ("unpaid", "no_payment_required", "paid"):
        change = billing.plan_change_from_event({
            "id": f"evt_{status}", "created": 100,
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
            assert change["account_id"] == "acct-1" and change["plan"] == "team"


def test_a_forged_plan_name_cannot_invent_a_tier():
    """The plan travels in metadata we set, but the event is still parsed
    defensively -- an unknown tier grants nothing rather than falling through to
    something permissive."""
    import billing

    assert billing.plan_change_from_event({
        "id": "evt_bad_plan", "created": 100,
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
        "id": "evt_cancel", "created": 101,
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_9", "metadata": {"account_id": "acct-9"}}},
    })["plan"] == "trial"


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


def test_paid_checkout_is_one_managed_inbox_per_account():
    """The Agency price is per isolated inbox, not a seat bundle or an
    unbounded quantity a browser can choose."""
    import billing

    captured = {}

    def fake_post(path, form):
        captured["path"] = path
        captured["form"] = form
        return {"url": "https://checkout.stripe.test/session"}

    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(billing, "STRIPE_SECRET_KEY", "sk_test_x"))
        stack.enter_context(patched(billing, "PUBLIC_BASE_URL", "https://sendkeep.test"))
        stack.enter_context(patched(billing, "price_id_for", lambda plan, yearly=False: "price_team"))
        stack.enter_context(patched(billing, "_post", fake_post))
        url = billing.create_checkout_session(
            {"id": "acct-agency", "email": "owner@example.com"}, "team"
        )

    assert url == "https://checkout.stripe.test/session"
    assert captured["path"] == "checkout/sessions"
    fields = dict(captured["form"])
    assert fields["line_items[0][quantity]"] == "1"
    assert fields["client_reference_id"] == "acct-agency"
    assert fields["metadata[plan]"] == "team"
    assert fields["line_items[0][price]"] == "price_team"


# ------------------------------------------------------ single follow-up loop

def test_follow_up_due_uses_business_days_and_never_zero_days():
    friday = datetime(2026, 8, 14, 9, 30, tzinfo=timezone.utc)
    assert drafts_db._follow_up_due(friday, 1) == datetime(
        2026, 8, 17, 9, 30, tzinfo=timezone.utc
    )
    assert drafts_db._follow_up_due(friday, 0) == datetime(
        2026, 8, 17, 9, 30, tzinfo=timezone.utc
    )
    assert drafts_db._follow_up_due(friday, 3) == datetime(
        2026, 8, 19, 9, 30, tzinfo=timezone.utc
    )


class _PagedFollowUps:
    def __init__(self, rows):
        self.rows = rows
        self.ranges = []
        self._account_id = None
        self._statuses = None
        self._range = None

    def table(self, name): return self
    def select(self, columns): return self
    def eq(self, column, value):
        if column == "account_id": self._account_id = value
        return self
    def in_(self, column, values):
        if column == "follow_up_status": self._statuses = set(values)
        return self
    def order(self, column): return self
    def range(self, start, end):
        self._range = (start, end)
        self.ranges.append(self._range)
        return self
    def execute(self):
        matched = [
            row for row in self.rows
            if row.get("account_id") == self._account_id
            and row.get("follow_up_status") in self._statuses
        ]
        start, end = self._range
        return type("R", (), {"data": matched[start:end + 1]})()


def test_active_follow_ups_are_paginated_without_dropping_the_tail():
    fake = _PagedFollowUps([
        {"id": 1, "account_id": "a1", "follow_up_status": "waiting"},
        {"id": 2, "account_id": "a1", "follow_up_status": "queued"},
        {"id": 3, "account_id": "a1", "follow_up_status": "sent"},
        {"id": 4, "account_id": "a1", "follow_up_status": "cancelled"},
        {"id": 5, "account_id": "other", "follow_up_status": "waiting"},
    ])
    with patched(drafts_db, "_get_client", lambda: fake), \
         patched(drafts_db, "FOLLOW_UP_PAGE_SIZE", 2):
        rows = drafts_db.list_active_follow_ups("a1")
    assert [row["id"] for row in rows] == [1, 2, 3]
    assert fake.ranges == [(0, 1), (2, 3), (3, 4)]


def test_due_follow_up_enters_manual_review_once_without_sending():
    calls = {"rows": [(2, _row(status="Sent", thread="t1", name="John",
                                  email="john@x.com", body="original"))]}
    source = {
        "id": 44, "thread_id": "t1", "email": "john@x.com", "body": "original",
        "follow_up_status": "waiting", "follow_up_due_at": "2026-08-01T00:00:00+00:00",
    }
    targets = _watch_env(calls, {"t1": None})
    with contextlib.ExitStack() as stack:
        for target in targets:
            stack.enter_context(patched(*target))
        stack.enter_context(patched(drafts_db, "list_active_follow_ups", lambda aid: [source]))
        stack.enter_context(patched(drafts_db, "mark_follow_up_queued",
                                    lambda aid, did: calls.setdefault("queued", []).append(did) or True))
        stack.enter_context(patched(suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(auth, "unsubscribe_url", lambda aid, email: "https://x.test/u"))
        stack.enter_context(patched(agent, "draft_follow_up", lambda *a, **k: "Useful reminder\n\nUnsubscribe: https://x.test/u"))
        stack.enter_context(patched(agent, "follow_up_problems", lambda *a, **k: []))
        stack.enter_context(patched(reviews_db, "add_follow_up",
                                    lambda aid, did, *a, **k: calls.setdefault("follow_up", []).append(did) or 91))
        result = watch_replies.check_for_replies(ACCOUNT)
    assert calls["follow_up"] == [44]
    assert calls["queued"] == [44]
    assert result["reviews"] == [{"id": 91, "status": "pending"}]
    assert "send_reply" not in calls, "the watcher queues; only operator approval may send"


def test_worker_reconciles_fenced_follow_up_without_operator_gmail_inspection():
    source = {
        "id": 44, "thread_id": "t1", "email": "john@x.com",
        "follow_up_status": "sending",
    }
    review = {
        "id": 91, "status": "pending", "email": "john@x.com",
        "draft_reply": "A useful reminder",
    }
    order = []
    errors = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(drafts_db, "list_follow_ups_needing_reconciliation", lambda aid: [source]))
        stack.enter_context(patched(reviews_db, "find_follow_up", lambda aid, did: review))
        stack.enter_context(patched(gmail, "get_thread", lambda *a: {"messages": []}))
        stack.enter_context(patched(gmail, "find_sent_reply", lambda *a, **k: {"id": "sent"}))
        stack.enter_context(patched(drafts_db, "mark_follow_up_sent", lambda aid, did: order.append("source") or True))
        stack.enter_context(patched(reviews_db, "mark_sent", lambda aid, rid, body: order.append("review")))
        watch_replies._reconcile_follow_up_sends(ACCOUNT, errors)
    assert errors == []
    assert order == ["source", "review"]


def test_follow_up_send_rechecks_thread_and_cancels_on_last_second_reply():
    import server
    review = dict(_PENDING_REVIEW, id=90, kind="follow_up", source_draft_id=44)
    calls = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: review))
        stack.enter_context(patched(server.drafts_db, "get_draft",
                                    lambda aid, did: {"id": 44, "follow_up_status": "queued"}))
        stack.enter_context(patched(server.drafts_db, "claim_follow_up_send", lambda aid, did: True))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(server.gmail, "get_thread", lambda *a: {"messages": []}))
        stack.enter_context(patched(server.gmail, "get_own_addresses", lambda *a: ({"me@x.com"}, False)))
        stack.enter_context(patched(server.gmail, "get_latest_reply_with_history", lambda *a, **k: {"text": "yes"}))
        stack.enter_context(patched(
            server.drafts_db, "mark_claimed_follow_up_replied",
            lambda aid, did: calls.append("cancel") or True,
        ))
        stack.enter_context(patched(server.reviews_db, "dismiss", lambda aid, rid: calls.append("dismiss")))
        stack.enter_context(patched(server.gmail, "send_reply",
                                    lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not send"))))
        try:
            server.send_reply(_srv_req(), 90, server.SendReplyBody(body="reminder"))
            raise AssertionError("expected a fail-closed 409")
        except server.HTTPException as e:
            assert e.status_code == 409
    assert calls == ["cancel", "dismiss"]


def test_approved_follow_up_sends_once_with_opt_out_and_records_source_first():
    import server
    review = dict(_PENDING_REVIEW, id=90, kind="follow_up", source_draft_id=44)
    order = []
    sent = {}
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server, "_assert_send_capacity", lambda account: None))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: review))
        stack.enter_context(patched(server.drafts_db, "get_draft",
                                    lambda aid, did: {"id": 44, "follow_up_status": "queued"}))
        stack.enter_context(patched(server.drafts_db, "claim_follow_up_send", lambda aid, did: True))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(server.gmail, "get_thread", lambda *a: {"messages": []}))
        stack.enter_context(patched(server.gmail, "get_own_addresses", lambda *a: ({"me@x.com"}, False)))
        stack.enter_context(patched(server.gmail, "get_latest_reply_with_history", lambda *a, **k: None))
        stack.enter_context(patched(server.gmail, "find_bounce", lambda *a, **k: None))
        stack.enter_context(patched(server.auth, "unsubscribe_url", lambda aid, email: "https://x.test/u"))
        stack.enter_context(patched(server.reviews_db, "update_claimed_body", lambda *args: True))
        stack.enter_context(patched(server.send_capacity_db, "reserve", lambda *args: True))
        stack.enter_context(patched(server.send_capacity_db, "complete", lambda *args: True))
        stack.enter_context(patched(server.gmail, "send_reply",
                                    lambda a, tid, to, body, unsubscribe_url="": sent.update(body=body, header=unsubscribe_url)))
        stack.enter_context(patched(server.drafts_db, "mark_follow_up_sent", lambda aid, did: order.append("source") or True))
        stack.enter_context(patched(server.reviews_db, "mark_sent", lambda aid, rid, body: order.append("review")))
        result = server.send_reply(_srv_req(), 90, server.SendReplyBody(body="A useful reminder"))
    assert result == {"ok": True}
    assert order == ["source", "review"]
    assert sent["header"] == "https://x.test/u"
    assert "https://x.test/u" in sent["body"]


def test_follow_up_atomic_claim_prevents_a_second_tab_from_sending():
    import server
    review = dict(_PENDING_REVIEW, id=90, kind="follow_up", source_draft_id=44)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: review))
        stack.enter_context(patched(server.drafts_db, "get_draft",
                                    lambda aid, did: {"id": 44, "follow_up_status": "queued"}))
        stack.enter_context(patched(server.drafts_db, "claim_follow_up_send", lambda aid, did: False))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(server.gmail, "get_thread", lambda *a: {"messages": []}))
        stack.enter_context(patched(server.gmail, "get_own_addresses", lambda *a: ({"me@x.com"}, False)))
        stack.enter_context(patched(server.gmail, "get_latest_reply_with_history", lambda *a, **k: None))
        stack.enter_context(patched(server.gmail, "find_bounce", lambda *a, **k: None))
        stack.enter_context(patched(server.auth, "unsubscribe_url", lambda aid, email: "https://x.test/u"))
        stack.enter_context(patched(server.reviews_db, "dismiss", lambda *a: None))
        stack.enter_context(patched(server.gmail, "send_reply",
                                    lambda *a, **k: (_ for _ in ()).throw(AssertionError("lost claim must not send"))))
        try:
            server.send_reply(_srv_req(), 90, server.SendReplyBody(body="reminder"))
            raise AssertionError("expected 409")
        except server.HTTPException as e:
            assert e.status_code == 409


def test_lost_claim_does_not_dismiss_a_sending_follow_up():
    """The loser of the claim race must not dismiss the review when the winner
    is mid-send. If the winner's Gmail attempt then fails, the source stays in
    `sending` (delivery uncertain) and the dismissed card would leave the
    follow-up invisible with no reset path. The loser surfaces the in-flight
    state instead and lets the winner's own outcome decide."""
    import server
    review = dict(_PENDING_REVIEW, id=90, kind="follow_up", source_draft_id=44)
    dismissed = []
    reads = {"n": 0}
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: review))
        def draft_status(aid, did):
            reads["n"] += 1
            # First read: queued (so we pass the initial check and attempt the
            # claim). Re-read after the failed claim: sending (winner mid-send).
            return {"id": 44, "follow_up_status": "sending" if reads["n"] > 1 else "queued"}
        stack.enter_context(patched(server.drafts_db, "get_draft", draft_status))
        stack.enter_context(patched(server.drafts_db, "claim_follow_up_send", lambda aid, did: False))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(server.reviews_db, "dismiss",
                                    lambda aid, rid: dismissed.append(rid)))
        try:
            server.send_reply(_srv_req(), 90, server.SendReplyBody(body="reminder"))
            raise AssertionError("expected 409")
        except server.HTTPException as e:
            assert e.status_code == 409
            assert "another window" in e.detail
    assert dismissed == [], "an in-flight send must not be hidden behind a dismissed review"


def test_blank_reply_body_is_rejected_422_before_any_send():
    import server
    review = dict(_PENDING_REVIEW, id=90)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: review))
        stack.enter_context(patched(server.gmail, "send_reply",
                                    lambda *a, **k: (_ for _ in ()).throw(AssertionError("blank body must not reach Gmail"))))
        for body in ("", "   ", "\n\t "):
            try:
                server.send_reply(_srv_req(), 90, server.SendReplyBody(body=body))
                raise AssertionError(f"blank body {body!r} must be rejected")
            except server.HTTPException as e:
                assert e.status_code == 422


def test_blank_follow_up_body_is_rejected_422_after_status_check():
    import server
    review = dict(_PENDING_REVIEW, id=90, kind="follow_up", source_draft_id=44)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: review))
        stack.enter_context(patched(server.drafts_db, "get_draft",
                                    lambda aid, did: {"id": 44, "follow_up_status": "queued"}))
        stack.enter_context(patched(server.drafts_db, "claim_follow_up_send", lambda aid, did: True))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(server.gmail, "get_thread", lambda *a: {"messages": []}))
        stack.enter_context(patched(server.gmail, "get_own_addresses", lambda *a: ({"me@x.com"}, False)))
        stack.enter_context(patched(server.gmail, "get_latest_reply_with_history", lambda *a, **k: None))
        stack.enter_context(patched(server.gmail, "find_bounce", lambda *a, **k: None))
        stack.enter_context(patched(server.auth, "unsubscribe_url", lambda aid, email: "https://x.test/u"))
        stack.enter_context(patched(server.gmail, "send_reply",
                                    lambda *a, **k: (_ for _ in ()).throw(AssertionError("blank follow-up must not reach Gmail"))))
        try:
            server.send_reply(_srv_req(), 90, server.SendReplyBody(body="   "))
            raise AssertionError("expected 422")
        except server.HTTPException as e:
            assert e.status_code == 422


def test_follow_up_gmail_failure_is_delivery_uncertain_not_resendable():
    """A Gmail exception after the claim must NOT release the source back to
    queued -- the message may have left. It stays `sending` (uncertain) and
    the operator gets told to check Gmail, never that it can be resent."""
    import server
    review = dict(_PENDING_REVIEW, id=90, kind="follow_up", source_draft_id=44)
    calls = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server, "_assert_send_capacity", lambda account: None))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: review))
        stack.enter_context(patched(server.drafts_db, "get_draft",
                                    lambda aid, did: {"id": 44, "follow_up_status": "queued"}))
        stack.enter_context(patched(server.drafts_db, "claim_follow_up_send", lambda aid, did: True))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(server.gmail, "get_thread", lambda *a: {"messages": []}))
        stack.enter_context(patched(server.gmail, "get_own_addresses", lambda *a: ({"me@x.com"}, False)))
        stack.enter_context(patched(server.gmail, "get_latest_reply_with_history", lambda *a, **k: None))
        stack.enter_context(patched(server.gmail, "find_bounce", lambda *a, **k: None))
        stack.enter_context(patched(server.auth, "unsubscribe_url", lambda aid, email: "https://x.test/u"))
        stack.enter_context(patched(server.reviews_db, "update_claimed_body", lambda *args: True))
        stack.enter_context(patched(server.send_capacity_db, "reserve", lambda *args: True))
        stack.enter_context(patched(server.drafts_db, "release_follow_up_send",
                                    lambda aid, did: calls.append("released")))
        stack.enter_context(patched(server.drafts_db, "mark_follow_up_sent",
                                    lambda aid, did: calls.append("marked_sent")))
        stack.enter_context(patched(server.reviews_db, "mark_sent",
                                    lambda aid, rid, body: calls.append("review_sent")))
        stack.enter_context(patched(server.gmail, "send_reply",
                                    lambda *a, **k: (_ for _ in ()).throw(OSError("connection reset"))))
        try:
            server.send_reply(_srv_req(), 90, server.SendReplyBody(body="reminder"))
            raise AssertionError("expected 502")
        except send_outreach.DeliveryUncertain as e:
            assert e.code == "send_uncertain" and e.retryable is False
    assert "released" not in calls, "a possibly-sent follow-up must not become resendable"
    assert calls == [], "no sent/review write may happen when Gmail failed"


def test_follow_up_preflight_failure_releases_claim_because_send_never_started():
    """A failed Gmail thread read is not an ambiguous send. The claim must go
    back to queued so a transient read outage does not freeze the follow-up."""
    import server
    review = dict(_PENDING_REVIEW, id=90, kind="follow_up", source_draft_id=44)
    calls = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server, "_assert_send_capacity", lambda account: None))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: review))
        stack.enter_context(patched(server.drafts_db, "get_draft",
                                    lambda aid, did: {"id": 44, "follow_up_status": "queued"}))
        stack.enter_context(patched(server.drafts_db, "claim_follow_up_send", lambda aid, did: True))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(server.gmail, "get_thread",
                                    lambda *a: (_ for _ in ()).throw(OSError("read timeout"))))
        stack.enter_context(patched(server.drafts_db, "release_follow_up_send",
                                    lambda aid, did: calls.append("released") or True))
        stack.enter_context(patched(server.gmail, "send_reply",
                                    lambda *a, **k: (_ for _ in ()).throw(AssertionError("preflight failed"))))
        try:
            server.send_reply(_srv_req(), 90, server.SendReplyBody(body="reminder"))
            raise AssertionError("expected 502")
        except server.HTTPException as e:
            assert e.status_code == 502
            assert "nothing was sent" in e.detail
    assert calls == ["released"]


def test_sending_row_send_attempt_surfaces_uncertain_not_dismissed():
    """A stranded `sending` row (claim won, Gmail outcome unknown) must not be
    silently dismissed as cancelled -- the operator needs to check Gmail."""
    import server
    review = dict(_PENDING_REVIEW, id=90, kind="follow_up", source_draft_id=44)
    dismissed = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: review))
        stack.enter_context(patched(server.drafts_db, "get_draft",
                                    lambda aid, did: {"id": 44, "follow_up_status": "sending"}))
        stack.enter_context(patched(server.reviews_db, "dismiss",
                                    lambda aid, rid: dismissed.append(rid)))
        try:
            server.send_reply(_srv_req(), 90, server.SendReplyBody(body="reminder"))
            raise AssertionError("expected 409")
        except server.HTTPException as e:
            assert e.status_code == 409
            assert "verified" in e.detail
    assert dismissed == [], "an uncertain send must not be dismissed as handled"


def test_uncertain_follow_up_can_be_reconciled_as_sent_source_first():
    import server
    source = {"id": 44, "follow_up_status": "sending"}
    review = dict(_PENDING_REVIEW, id=90, kind="follow_up", source_draft_id=44,
                  draft_reply="the exact attempted body")
    order = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: source))
        stack.enter_context(patched(server.reviews_db, "find_follow_up", lambda aid, did: review))
        stack.enter_context(patched(server.drafts_db, "mark_follow_up_sent",
                                    lambda aid, did: order.append("source") or True))
        stack.enter_context(patched(server.reviews_db, "mark_sent",
                                    lambda aid, rid, body: order.append(("review", body))))
        result = server.reconcile_follow_up(
            _srv_req(), 44, server.ReconcileFollowUpBody(outcome="sent")
        )
    assert result == {"ok": True, "status": "sent"}
    assert order == ["source", ("review", "the exact attempted body")]


def test_reconcile_sent_is_idempotent_when_only_review_cleanup_failed():
    """The source may already be the durable `sent` fence when the secondary
    review update failed. Repeating the sent decision finishes cleanup without
    reopening Gmail or attempting the source transition again."""
    import server
    source = {"id": 44, "follow_up_status": "sent"}
    review = dict(_PENDING_REVIEW, id=90, kind="follow_up", source_draft_id=44,
                  draft_reply="attempted body")
    closed = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: source))
        stack.enter_context(patched(server.reviews_db, "find_follow_up", lambda aid, did: review))
        stack.enter_context(patched(
            server.drafts_db, "mark_follow_up_sent",
            lambda *a: (_ for _ in ()).throw(AssertionError("sent source must not transition twice")),
        ))
        stack.enter_context(patched(server.reviews_db, "mark_sent",
                                    lambda aid, rid, body: closed.append((rid, body))))
        result = server.reconcile_follow_up(
            _srv_req(), 44, server.ReconcileFollowUpBody(outcome="sent")
        )
    assert result == {"ok": True, "status": "sent"}
    assert closed == [(90, "attempted body")]


def test_uncertain_follow_up_can_return_to_queue_only_after_explicit_retry_choice():
    import server
    source = {"id": 44, "follow_up_status": "sending"}
    review = dict(_PENDING_REVIEW, id=90, kind="follow_up", source_draft_id=44)
    released = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: source))
        stack.enter_context(patched(server.reviews_db, "find_follow_up", lambda aid, did: review))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(server.drafts_db, "release_follow_up_send",
                                    lambda aid, did: released.append(did) or True))
        result = server.reconcile_follow_up(
            _srv_req(), 44, server.ReconcileFollowUpBody(outcome="retry")
        )
    assert result == {"ok": True, "status": "queued"}
    assert released == [44]


def test_uncertain_follow_up_retry_cancels_if_contact_opted_out():
    import server
    source = {"id": 44, "follow_up_status": "sending"}
    review = dict(_PENDING_REVIEW, id=90, kind="follow_up", source_draft_id=44)
    calls = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server.drafts_db, "get_draft", lambda aid, did: source))
        stack.enter_context(patched(server.reviews_db, "find_follow_up", lambda aid, did: review))
        stack.enter_context(patched(server.suppressions_db, "is_suppressed", lambda aid, email: True))
        stack.enter_context(patched(server.drafts_db, "cancel_claimed_follow_up",
                                    lambda aid, did, why: calls.append(("cancel", why)) or True))
        stack.enter_context(patched(server.reviews_db, "dismiss",
                                    lambda aid, rid: calls.append(("dismiss", rid))))
        stack.enter_context(patched(server.drafts_db, "release_follow_up_send",
                                    lambda *a: (_ for _ in ()).throw(AssertionError("opted-out mail must not be re-queued"))))
        try:
            server.reconcile_follow_up(
                _srv_req(), 44, server.ReconcileFollowUpBody(outcome="retry")
            )
            raise AssertionError("expected 409")
        except server.HTTPException as e:
            assert e.status_code == 409 and "opted out" in e.detail
    assert calls == [("cancel", "opt_out"), ("dismiss", 90)]


def test_follow_up_cannot_be_dismissed_after_another_request_claims_send():
    import server
    review = dict(_PENDING_REVIEW, id=90, kind="follow_up", source_draft_id=44)
    dismissed = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(server, "_account", lambda r: dict(ACCOUNT)))
        stack.enter_context(patched(server, "_assert_send_capacity", lambda account: None))
        stack.enter_context(patched(server.reviews_db, "get_review", lambda aid, rid: review))
        stack.enter_context(patched(server.drafts_db, "cancel_follow_up", lambda *a: False))
        stack.enter_context(patched(server.reviews_db, "dismiss",
                                    lambda aid, rid: dismissed.append(rid)))
        try:
            server.dismiss_reply(_srv_req(), 90)
            raise AssertionError("expected 409")
        except server.HTTPException as e:
            assert e.status_code == 409
    assert dismissed == []


def test_dismissed_follow_up_review_is_reactivated_on_retry():
    """The partial unique index forbids a second review per source, so a retry
    after a transient queue failure must resurrect the dismissed review, not
    leave the source queued with nothing visible to approve."""
    fake = _ReviewsTable(rows=[
        {"id": 91, "account_id": "acct-1", "source_draft_id": 44, "kind": "follow_up",
         "status": "dismissed", "draft_reply": "", "original_draft_reply": None},
    ])
    with patched(reviews_db, "_get_client", lambda: fake):
        rid = reviews_db.add_follow_up(
            "acct-1", 44, 2, "John", "john@x.com", "t1", "Reminder",
            validator_problems=None,
        )
    assert rid == 91, "the unique index means one review per source -- reuse the dismissed one"
    row = fake.rows[0]
    assert row["status"] == "pending"
    assert row["draft_reply"] == "Reminder"
    assert len(fake.rows) == 1, "must reactivate, not insert a second row"


def test_pending_follow_up_review_is_reused_not_duplicated():
    fake = _ReviewsTable(rows=[
        {"id": 91, "account_id": "acct-1", "source_draft_id": 44, "kind": "follow_up",
         "status": "pending", "draft_reply": "old", "original_draft_reply": "old"},
    ])
    with patched(reviews_db, "_get_client", lambda: fake):
        rid = reviews_db.add_follow_up("acct-1", 44, 2, "John", "john@x.com", "t1", "new")
    assert rid == 91
    assert fake.rows[0]["status"] == "pending"
    assert fake.rows[0]["draft_reply"] == "old", "an already-pending review must not be overwritten"


def test_due_follow_up_is_found_even_when_the_sheet_row_is_missing():
    """The database is the durable scheduler. Deleting the mirrored Sheet row
    must not silently strand a due follow-up."""
    calls = {"rows": []}
    source = {
        "id": 44, "row_index": 2, "thread_id": "t1", "name": "John",
        "email": "john@x.com", "company": "Acme", "body": "original",
        "follow_up_status": "waiting",
        "follow_up_due_at": "2026-08-01T00:00:00+00:00",
    }
    with contextlib.ExitStack() as stack:
        for target in _watch_env(calls, {"t1": None}):
            stack.enter_context(patched(*target))
        stack.enter_context(patched(drafts_db, "list_active_follow_ups", lambda aid: [source]))
        stack.enter_context(patched(drafts_db, "mark_follow_up_queued", lambda aid, did: True))
        stack.enter_context(patched(suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(auth, "unsubscribe_url", lambda aid, email: "https://x.test/u"))
        stack.enter_context(patched(agent, "draft_follow_up", lambda *a, **k: "Useful reminder"))
        stack.enter_context(patched(agent, "follow_up_problems", lambda *a, **k: []))
        stack.enter_context(patched(
            reviews_db, "add_follow_up",
            lambda aid, did, *a, **k: calls.setdefault("follow_up", []).append(did) or 91,
        ))
        result = watch_replies.check_for_replies(ACCOUNT)
    assert calls["follow_up"] == [44]
    assert calls["thread_reads"] == ["t1"]
    assert result["reviews"] == [{"id": 91, "status": "pending"}]
    assert "header_gate" not in calls, "a missing optional mirror must not invoke its write gate"


def test_zero_row_queue_transition_hides_the_unsendable_review():
    """A conditional update that matched no source row is a lost race, not a
    successful queue. The review must not be exposed unless the source is
    already queued by the competing worker."""
    calls = {"rows": [(2, _row(status="Sent", thread="t1", email="john@x.com"))]}
    source = {
        "id": 44, "thread_id": "t1", "email": "john@x.com", "body": "original",
        "follow_up_status": "waiting", "follow_up_due_at": "2026-08-01T00:00:00+00:00",
    }
    with contextlib.ExitStack() as stack:
        for target in _watch_env(calls, {"t1": None}):
            stack.enter_context(patched(*target))
        stack.enter_context(patched(drafts_db, "list_active_follow_ups", lambda aid: [source]))
        stack.enter_context(patched(drafts_db, "mark_follow_up_queued", lambda aid, did: False))
        stack.enter_context(patched(
            drafts_db, "get_draft",
            lambda aid, did: {"id": did, "follow_up_status": "cancelled"},
        ))
        stack.enter_context(patched(suppressions_db, "is_suppressed", lambda aid, email: False))
        stack.enter_context(patched(auth, "unsubscribe_url", lambda aid, email: "https://x.test/u"))
        stack.enter_context(patched(agent, "draft_follow_up", lambda *a, **k: "Useful reminder"))
        stack.enter_context(patched(agent, "follow_up_problems", lambda *a, **k: []))
        stack.enter_context(patched(reviews_db, "add_follow_up", lambda *a, **k: 91))
        stack.enter_context(patched(
            reviews_db, "dismiss_follow_up_for_source",
            lambda aid, did: calls.setdefault("dismissed", []).append(did),
        ))
        result = watch_replies.check_for_replies(ACCOUNT)
    assert calls["dismissed"] == [44]
    assert result["reviews"] == []


# ---------------------------------------------------------------------- runner

# ------------------------------------------- draft-quality hardening (2026-08-23)

def test_enforce_signature_replaces_a_hallucinated_signoff():
    """The model signed a live inbox Long / Alex / Team / Sam across four turns
    with Alex and Sam invented from thread context. Identity is never sampled:
    a bare trailing name line that disagrees with settings gets replaced."""
    body = "Sounds good, grab a time here.\n\nAlex"
    out = agent._enforce_signature(body, "Oanh")
    assert out.endswith("Oanh") and "Alex" not in out, out
    # Configured name already correct: untouched.
    assert agent._enforce_signature("See you then.\n\nOanh", "Oanh") == "See you then.\n\nOanh"


def test_enforce_signature_leaves_real_sentences_alone():
    """A closing sentence is prose, not a signature -- it must survive even
    when its words happen to look name-shaped."""
    body = "Thanks for the quick reply. I just booked the slot. Looking forward to our chat"
    assert agent._enforce_signature(body, "Oanh") == body
    # A one-line body IS the message, never a sign-off.
    assert agent._enforce_signature("hi", "Oanh") == "hi"


def test_enforce_signature_pins_the_name_line_above_a_company_signoff():
    """First-touch emails end 'Name\\nCompany'. The company line is a bare
    single word and once got rewritten INTO the sender name, destroying it."""
    body = "Grab a time here.\n\nAlex\nLedgerline"
    out = agent._enforce_signature(body, "Oanh", "Ledgerline")
    assert out == "Grab a time here.\n\nOanh\nLedgerline", out
    # Company already correct but name drifted -- same fix.
    out2 = agent._enforce_signature("See you then.\n\nSam\nLedgerline", "Oanh", "Ledgerline")
    assert out2 == "See you then.\n\nOanh\nLedgerline", out2


def test_validate_reply_flags_invented_statistics_but_allows_configured_ones():
    invented = "We cut onboarding time by up to 40% for teams like yours. Worth a chat? Oanh"
    problems = agent._validate_reply(invented, "", allowed_claims_context="help teams automate invoicing")
    assert any("invented statistic" in p and "40%" in p for p in problems), problems

    allowed_ctx = "customers report a 30% faster close"
    disclosed = "Teams typically see a 30% faster close. Worth a chat? Oanh"
    assert agent._validate_reply(disclosed, "", allowed_claims_context=allowed_ctx) == []


def test_validate_outreach_flags_the_live_fabricated_stat():
    subject = "Cutting your project timelines"
    body = (
        "Hi John, saw you're scaling delivery at Acme. Our solutions reduce "
        "project completion times by up to 30%. Grab a time here: "
        "https://cal.com/oanh/15min\n\nOanh"
    )
    problems = agent._validate_outreach(subject, body, "https://cal.com/oanh/15min", "")
    assert any("30%" in p for p in problems), problems


def test_add_review_returns_existing_row_when_insert_hits_the_unique_index():
    """Two concurrent check jobs can both pass check-then-insert; Postgres
    rejects the loser with 23505 and add_review must hand back the winner --
    a race error must never cost the notification, or duplicate the review."""
    class _InsertBuilder:
        def __init__(self, store):
            self.store = store

        def insert(self, payload):
            self.payload = payload
            return self

        def execute(self):
            if getattr(self.store, "armed", False):
                self.store.armed = False
                raise Exception('(code = 23505, message = "duplicate key value '
                                'violates unique constraint reviews_thread_message_idx")')
            self.store.data = [{"id": 4242}]
            return self

    class _SelectBuilder:
        def __init__(self, result):
            self.result = result
            # find_review_id reads .data on the execute() result; these same
            # builders double as both query builder and response.
            self.data = []

        def select(self, *_a, **_k):
            return self

        def eq(self, *_a, **_k):
            return self

        def is_(self, *_a, **_k):
            return self

        def limit(self, *_a):
            return self

        def execute(self):
            if self.result.selects:
                self.data = self.result.selects.pop(0)
            return self

    class Store:
        armed = True
        selects = [[], [{"id": 4242}]]  # first lookup misses, race lookup finds the winner

    store = Store()
    original_client = reviews_db._client
    builders = iter([_SelectBuilder(store), _InsertBuilder(store), _SelectBuilder(store)])

    class FakeClient:
        def table(self, *_a):
            return next(builders)

    reviews_db._client = FakeClient()
    try:
        out = reviews_db.add_review(
            "acct-1", 3, "John", "john@acme.test", "thread-1",
            None, "Great! Here's the link.", gmail_message_id="msg-9",
        )
    finally:
        reviews_db._client = original_client
    assert out == 4242, f"the pre-existing winner is returned: {out}"


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
