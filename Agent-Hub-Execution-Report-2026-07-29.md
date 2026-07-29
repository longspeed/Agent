# Agent Hub — Execution Report

**Date:** 2026-07-29
**Branch:** `main` (carries an unreviewed diff forward — see "Still open")
**Scope:** finish Phase 0 of the daily-use build (reply-detection correctness) before starting Phase 1, per `MASTER-DAILY.md`'s own sequencing rule
**Result:** 9 files changed, tests 242 → 273 passing, tree dirty (uncommitted), three real defects found and closed along the way

**Revision note:** this report originally went out with 271 passing tests and
a "Still open" list that silently dropped a risk carried over from the last
report. A review of the report itself (not just the code) caught that, plus
two unfinished decisions I'd left implicit in the code. All three are
addressed below — two fixed in code (target_email consistency, the
address-caching decision), one disclosed because it can't be fixed from here
(the unreviewed diff already on `main`). The "Blast radius" section also
needed rewriting honestly rather than left as originally drafted — see that
section.

---

## What I was asked to do

You asked me to plan Phase 1 (the background watcher). Checking Phase 1's
prerequisite first — `MASTER-DAILY.md`: *"Do not start phase N+1 before phase
N meets its definition of done"* — showed Phase 0 wasn't done:
`gmail.get_latest_reply_with_history` still only inspected `messages[-1]`, and
`reviews_db.py` said outright *"Only 'pending' exists today."* You chose to
finish Phase 0 first. This report covers that work, not Phase 1.

The plan went through four rounds of review before you approved it. Each round
found something real; none was cosmetic. That history is folded into "What
changed" below rather than kept separate, since the reviewed version is the
one that shipped.

---

## The problem

Phase 0's done-when: *"a reply is detected regardless of what else has landed
on the thread since, and an off-sheet reply reaches the user as a flagged item
instead of vanishing."* Two silent drop paths were still open:

1. **Operator replies manually from Gmail before a poll runs.** The newest
   message is then *ours*, not the contact's — `messages[-1]`'s address check
   fails, and the contact's actual reply is buried forever. The existing test
   `test_history_latest_is_ours_returns_none` asserted this as correct
   behavior; that was the bug, encoded as a passing test.
2. **A reply arrives from an address not on the sheet** — an assistant, an
   alias, a forwarded thread. Same failure: address doesn't match, message
   vanishes, no error, no card, nothing.

The eng review (TODOS.md, 2026-07-28) had already decided the fix shape:
dedupe-driven, keyed on the Gmail message id, with sender classification
(`daemon > auto-responder > on-sheet > off-sheet`) run *before* candidacy —
otherwise a bounce message misread as a reply candidate makes `find_bounce`
unreachable and bounce suppression dies silently. This work implements that
decision.

---

## What changed

### `gmail.py` — the actual fix

`get_own_addresses(account)` returns every address this account may
legitimately send from — the primary Gmail address plus every **verified**
Send-As alias — not just one address. Matching against only the primary would
flag the operator's own alias replies as an unconfirmed stranger. If the
Send-As listing call itself fails, it falls back to just the primary and
returns `degraded=True` rather than pretending the lookup succeeded.

`get_latest_reply_with_history` now scans every message on the thread and
classifies each one — `ours`, `daemon`, `auto_responder`, `on_sheet`,
`off_sheet` — via a shared `_classify_message` helper, in that precedence.
It returns the newest `on_sheet`/`off_sheet` message not preceded by our own
noise, plus whether a message from us postdates it (`answered_elsewhere`).

`get_history_before` was added for a specific reason: an off-sheet reply
defers drafting to confirm time (see below), so the conversation history has
to be rebuildable after the fact from just the stored message id. It shares
`_classify_message` with the detection-time function rather than re-deriving
"from the contact" by a separate rule — two functions computing the same
judgment independently is exactly the kind of seam that silently drifts.

**Fixed during report review, not the original pass:** sharing
`_classify_message` closed the classification seam, but which *email* gets
passed as the target to each function was still unstated, and my first
consistency test used the same email for both calls — which passes trivially
and can't catch the two functions disagreeing. They *would* have disagreed:
`get_latest_reply_with_history`'s inline history was labelling
`from_contact` against `contact_email` (the sheet's address) even when the
candidate itself was off-sheet, while `get_history_before` at confirm time is
called with `review["email"]` — the *observed* off-sheet sender, a different
string. An off-sheet sender's own earlier message on the same thread would
have been mislabelled "not from them" in the history built at detection time,
correctly labelled at confirm time, and nothing would have caught the
difference because the only test exercising both functions used an on-sheet
thread where the two targets happen to be identical.

Fixed by labelling history against the **candidate's own observed address**,
not `contact_email`, in both functions — consistent by construction now, not
just by two implementations happening to agree. Added the test that actually
exercises the divergent case: an off-sheet thread, same sender writing twice,
target passed to `get_history_before` is the observed address exactly as
`_confirm_sender_job` passes it, not the sheet's contact_email.

Also decided, not left implicit: `get_latest_reply` (backing
`check_single_reply`, a per-row on-demand check, never a loop) still calls
`get_own_addresses` fresh on every invocation rather than caching it the way
`check_for_replies` fetches it once per cycle. Left as-is, with a comment
explaining why: this is a one-shot interactive action where the extra
Gmail call doesn't compound, not the polling loop where it would. If a future
caller ever iterates rows through this function, the comment says to hoist
the lookup out the way `check_for_replies` already does.

### `watch_replies.py` — wiring, and where trust matters

- `answered_elsewhere` → queued visible, empty draft, no LLM call. Nothing to
  send; worth knowing.
- **Off-sheet reply → queued `flagged`, no draft attempted.** This is the one
  deliberate architectural choice in the whole change: an unconfirmed sender
  is the least-trusted input this system sees, and every other path here
  checks *before* generating (the outreach-send path filters suppressed
  addresses before drafting). Drafting an off-sheet reply immediately would
  run untrusted input through the LLM first and check second. Drafting now
  happens only after a human confirms the sender is worth replying to.
- On-sheet path: unchanged, plus `plans.check(UNIT_DRAFT_REPLY)` now runs
  before drafting, with its own exception branch so `QuotaExceeded`'s rich,
  plan-specific message reaches the row_error verbatim instead of getting
  folded into the generic "drafting failed" wrapper text.

### `reviews_db.py` / `accounts_db.py`

`add_review` gained `status` and `degraded_classification` parameters, and a
structural fix found while tracing what a failed draft writes: it was storing
`original_draft_reply=""` on any drafting failure — which Phase 6's (not yet
built) edit-diff learning would read as "the model's output should be
replaced wholesale" instead of "the model wrote nothing." Now `None`. New
`confirm_sender(account_id, review_id, draft_reply)` promotes a `flagged`
review to `pending`, scoped so it can never resurrect a dismissed or sent one,
and returns whether a row actually matched — the caller has to check this,
because a review dismissed mid-confirm is a real outcome, not corruption.

`degraded_classification` is a real column, onboarded through
`accounts_db._MIGRATED_COLUMNS` (the boot-check warning) rather than a manual
README migration step. This mattered: `add_review` is the insert path for
*every* review, so a missing column here doesn't degrade one feature, it
stops reply detection outright.

### `server.py`

New `POST /api/outreach/replies/{id}/confirm-sender`, and it runs as a
background job, not a synchronous handler — matching this file's own stated
convention that long-running agent actions don't block a request. The job:
re-checks the review is still `flagged` before spending a quota check or an
LLM call (closes a double-click wasting both); rebuilds history and re-reads
the sheet row for company; drafts, with quota/drafting failures degrading to
an empty draft plus a warning rather than failing the confirmation itself;
writes the draft via `confirm_sender` and actually reads its return value —
if the review was dismissed mid-flight, the freshly generated draft is
discarded along with the write, and the response says so instead of claiming
success.

Also: `send_reply` never checked `suppressions_db` at all — found while
tracing what address a reply to a confirmed off-sheet sender should route to.
An unsubscribed contact could receive a reply with zero code path to stop it.
Now a 409, matching the outreach-send path's existing guard.

### `static/outreach.html`

Review cards now branch on status: `flagged` shows who actually wrote and a
"Confirm it's them" button (which polls the new job like the existing
check-replies button does); `answered_elsewhere` hides the draft/send controls
entirely with a short explanatory note; either can also carry a
"sender verification was incomplete" note when `degraded_classification` is
set.

---

## Blast radius: predicted vs actual — and a repeated mistake

The plan asserted, not just predicted, that updating `_watch_env`'s fake in
one place would "keep all ~12 of those tests passing unchanged." That claim
was only true *after* the fixture edit. Stated as a blast-radius prediction,
it should have counted those 12 as tests-that-break-without-the-fix, then
noted the fix removes the break — instead it stated the post-fix outcome as
if it were the prediction, which is the exact conflation named in the
previous execution report: *"I conflated 'tests that need editing' with
'tests that fail.'"*

Actual, on first run: **16 failures** — the 7 direct-caller tests the plan
expected, plus the entire 9-test `_watch_env`-based suite the plan had just
claimed would stay green, because the fake's own call signature also needed
the new `own_addresses` parameter.

This is the same mechanism as last time, described in that report as a
lesson learned, and applied zero times since: I know the fixture needs
editing, so I stop counting its dependents as at-risk, when "I'm editing this
anyway" is exactly the reasoning that hides a break. The mechanical fix named
last time and still not adopted: enumerate every test from the caller list
first, and only subtract a test from the failure count once the replacement
behavior is verified against it — not once a fix is merely planned.

Smaller and caught before it shipped: while adding the target_email
consistency test above, I first wrote the expected `from_contact` sequence
by hand — `[True, False, True]` — and asserted it without tracing the
classifier against the actual fixture. It was transposed; the real sequence
is `[False, True, False]`. The test failed on the first run, which is the
system working, but it's the same root behavior at a smaller scale: writing
down what I expect to be true and treating that as verification. Caught here
because a failing test forces the check; the fixture-signature miss above
had no equivalent forcing function until the suite actually ran.

All 16 (plus the 1 self-caught) were mechanical, not conceptual — nothing
indicated the classification logic itself was wrong. Fixed by updating the
direct tests to the new dict return, renaming the one test that had encoded
the bug as correct behavior, updating `_watch_env`'s fake in one place to
accept the new parameter, and correcting the transposed assertion by tracing
the classifier by hand against the fixture instead of guessing.

---

## Tests

**242 → 273 passing** (+31). `python -m py_compile server.py
outreach-agent/*.py tests/*.py` clean throughout.

New coverage, beyond the direct consequences of the rewrite:

- `get_own_addresses` includes verified aliases, excludes unverified ones, and
  degrades to just the primary (with `degraded=True`) when the Send-As call
  fails.
- A reply from a Send-As alias is `ours`, not flagged as a stranger. An
  auto-responder and a daemon message are both excluded from candidacy
  entirely — the daemon case specifically protects `find_bounce`'s
  reachability.
- `get_history_before` produces output identical to the `history` field
  `get_latest_reply_with_history` builds inline — on an on-sheet thread
  (target and contact_email identical, the weaker check) **and on an
  off-sheet thread with the sender's own target address passed in**, which is
  the case that would actually catch the divergence described above.
- An off-sheet sender's own earlier message on the same thread reads as
  `from_contact=True` in the history, not mislabelled against the sheet's
  contact address.
- **One integration test runs the real classifier through
  `check_for_replies`**, not the usual fully-mocked fake — every other new
  test exercises either `gmail.py` directly or `check_for_replies` against
  the mock, never both together, which is exactly the seam where a stray
  string mismatch (`"on-sheet"` vs `"on_sheet"`) would pass on both sides and
  only break in production.
- `confirm_sender`'s three failure/success paths, including the discarded
  draft when a review is dismissed mid-confirmation, and that a sheet/thread
  read failure propagates as a real job error while a drafting failure does
  not.
- `send_reply`'s new suppression check, and the `QuotaExceeded` message
  surviving verbatim into both the row_error and the confirm-sender warning.

---

## State now

**Branch:** `main`, 9 files modified, uncommitted.
**Health:** 273/273 tests, `py_compile` clean.

### Still open

- **`d755f22`'s +4,280 lines are on `main`, unreviewed, right now.** The
  2026-07-28 report flagged this explicitly: *"/review on that diff is still
  the highest-value thing before this lands on main… Nothing has been pushed.
  Nothing has been merged."* Earlier in this session I fast-forwarded `main`
  to the feature branch tip and pushed both, at your direction, without a
  `/review` pass on that diff ever happening. That was a reasonable choice at
  the time — but this report's first draft opened with `Branch: main` and
  said nothing about it in "Still open," which is how the single largest
  named risk in this project quietly stopped appearing in the risk section.
  It is not resolved. `/review` (or equivalent) on `d755f22` is still the
  highest-value next step before building further on top of it.
- `monthly_replies` is `None` for every plan tier. The quota gate is now
  structurally wired (it wasn't called anywhere before this), but it's a
  no-op until someone picks real numbers — a pricing decision, not a build
  one. Logged in TODOS.md rather than guessed at here.
- The `degraded_classification` boot-check migration hasn't been run against
  any real database yet — it's additive and default-safe, but this only
  covers the code side.
- Nothing from this session is committed yet — waiting on your go-ahead.

### Resolved during this revision, not left open

- The `target_email` inconsistency between `get_latest_reply_with_history`
  and `get_history_before` — fixed in `gmail.py`, tested against the actual
  divergent case (see "What changed" and "Tests" above).
- The per-call `get_own_addresses` in `get_latest_reply` — decided and
  documented in place (one-shot interactive caller, not the polling loop;
  comment states the condition under which that would need to change).

### Next

Phase 1 — the background watcher — is unblocked. `watch_replies.main()`
still takes one account from `sys.argv`; turning it into one process walking
every connected account, on a real host, is the next piece, and it now sits
on a detector that doesn't silently drop replies out from under it.
