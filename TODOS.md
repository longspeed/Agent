# TODOS

## AI draft-quality fixes from the live /outreach quality audit (2026-08-23)

Graded every historical review + first-touch draft on this account
(10 reviews, 3 drafts). Mechanics pass; grounding fails. Four surgical
fixes, all worked in the same pass:

### G1. Exclude own-address messages from reply detection
**What:** In `outreach-agent/agent.py`'s reply discovery, skip any thread
message whose `From` is one of the account's own send addresses (the
connected Gmail address). Today the account's own outbound messages are
detected as "customer replies," and the model then drafts answers to
itself — observed as a 4-turn self-conversation loop (reviews rows 6-10)
that only stopped via manual dismiss.
**Why:** Capture fidelity is the dominant term of the product's value
equation; a tool that converses with itself destroys trust in one glance.
**Priority:** P0

### G2. Pin sender name/signature server-side
**What:** Reply-draft generation must never let the model choose the
sign-off. Inject the account's configured sender name (Settings) into the
prompt AND strip/replace model-generated signatures server-side before
persisting. Observed sign-offs drifted across Long / Alex / Team / Sam —
Alex and Sam are hallucinated personas that exist nowhere in settings.
**Why:** A wrong sender name on a prospect-facing email is unrecoverable;
identity must be deterministic, not sampled.
**Priority:** P0

### G3. Dedupe review creation per thread+message
**What:** Reviews rows 2 and 3 were created for the same thread 5 seconds
apart with different drafts (`gmail_message_id` stored as None, so
`find_review_id` can't match). Enforce uniqueness on
(account_id, thread_id, newest message id): populate `gmail_message_id`
at detection time and make `find_review_id` treat it as required when
present; add a short re-check window against in-flight inserts.
Also: two sheet rows sharing one contact email (observed: pessiskibi@
gmail.com used for both "Jonh" and "Messi") cross-wire threads — scope
review lookup by thread_id first, email second.
**Why:** Double drafts mean double sends waiting to happen.
**Priority:** P0

### G4. Flag fabricated stats in the draft validator
**What:** Extend the draft validator's problem list to catch unsourced
quantitative claims ("reduce … by up to 30%", "% increase", "X% of
customers") unless the stat comes verbatim from the operator's own
settings/research note. Observed live: "Our team at Apple has developed
solutions that can reduce project completion times by up to 30%." Add the
pattern to whatever produces `validator_problems` so the review card shows
"Check before sending."
**Why:** Fabricated numbers are both an ethics failure and the classic
spam tell the validator exists to catch.
**Priority:** P1

**Verification bar:** rerun the quality audit script after the fixes — no
self-replies, single persona signature, no duplicate reviews, validator
flags invented stats. Then run the test suite.

## Deferred from the multi-tenant hotfix pass (2026-07-16, `/plan-ceo-review`).
None of these block a single real customer from using the app end-to-end —
they matter once there's more than one customer, or once sending volume grows.

## Deferred from the trust/messaging bug-fix review (2026-08-05, `/plan-ceo-review`)

Filed while reviewing a live QA report (autopilot-vs-approval messaging, false
"verified emails" claims, a Prepare-drafts UX dead end, locale-naive
formatting, the Replied? column, and an exact-string meeting-purpose guard).
**Resolution:** implementation landed and passed `/plan-eng-review` on
2026-08-05 (T1-T13 from the CEO review, plus fixes found during the eng
pass — see below). Mode was HOLD SCOPE; these two are the parts explicitly
deferred from that plan rather than included in it.

### Extend the legal-copy review to static/landing/'s marketing claims
**What:** `static/landing/`'s pricing tiers and feature bullets ("verified
emails," lead-verification claims) need the same lawyer pass already planned
for `/privacy`, `/terms`, `/security` etc.
**Why:** This review found a live, false "Verified emails only" / "never
touches an address it couldn't verify" claim on the public landing page —
automated verification (NeverBounce) was removed 2026-07-18; today
verification is manual-only. **Correcting that specific copy landed
2026-08-05** (turned out to already be fixed in `site/src/` source as of
commit `15a420c` — the deployed bundle was just 12+ days stale; a rebuild
shipped it), but the underlying question ("does every claim on this page
hold up") wasn't fully audited beyond what this review happened to check,
and the legal pages already have a planned review this page isn't currently
in scope for.
**Context:** `static/landing/` (compiled React bundle — edit the source under
whatever `app/` (or equivalent) directory builds it, not the compiled JS
directly); existing TODOS entry "Legal pages need a lawyer's review before
charging anyone."
**Effort:** XS (human) → XS (CC + gstack)
**Priority:** P2
**Depends on:** Pairs naturally with the existing legal-pages review; no
hard blocker.

### Merge static/index.html into static/landing/ once outreach/leads migrate to React
**What:** `static/index.html` (the signed-in home page at `/`) and
`static/landing/` (the logged-out marketing bundle) currently carry two
independently-maintained descriptions of the product. This review found they
had already drifted (one said "runs itself / autonomous agents," the other
said "no autopilot mode — the approval step is the product"); **`index.html`
was rewritten in place to match on 2026-08-05** — but the two-surface
structure itself still invites the same drift again.
**Why:** The root cause of this review's central finding wasn't a single
wrong sentence, it was that nothing would have caught the sentence going
wrong. Two sources of truth for "what does this product do" will drift again
unless they become one.
**Context:** `server.py`'s `require_auth` middleware (routes logged-out `/`
to `static/landing/`, logged-in `/` to `static/index.html`); `index.html`
is not pure marketing — it has a live `#app-nav` and real `#hero-stats` /
`#outreach-stats` / `#leads-stats` cards, so the merge needs to preserve that
functional dashboard shell, not just swap in landing's marketing copy.
**Effort:** Absorbed by the already-tracked React migration (see "Legacy
pages still load Tailwind from the CDN" below) — XS to note the dependency now.
**Priority:** P3
**Depends on:** The outreach/leads pages completing their React conversion first.

## Deferred from the trust/messaging fix's eng review (2026-08-05, `/plan-eng-review`)

### Settings shows a generic "Save failed" for a rejected-too-long meeting purpose
**What:** `SettingsPage.tsx`'s `handleSave()` doesn't distinguish a 422
(validation rejection — e.g. `meeting_purpose` over its new 500-char cap)
from any other failed save; both show "Save failed — try again."
**Why:** A user who somehow pastes 500+ characters gets no clue why saving
failed specifically.
**Pros:** Clearer error message for an edge case.
**Cons:** Realistic likelihood of hitting this is near zero — the field is
described as "one sentence, used in every email," and 500 characters is
generous for that.
**Context:** `app/src/SettingsPage.tsx` `handleSave()`; `server.py`'s
`SettingsBody.meeting_purpose` (`Field(max_length=500)`, added 2026-08-05).
**Effort:** XS (human) → XS (CC + gstack)
**Priority:** P3
**Depends on:** Nothing blocking.

## Guard audit: where each safety check gets its inputs (2026-07-26)

The same bug was found four times at four altitudes, and every instance was the
same shape: **a gating predicate reading a value the operator can edit.** The
lead sheet is a Google Sheet the customer owns, so any threshold measured against
it can be cleared by editing it — and in a conjunctive guard, the weakest term
decides. Hardening one term is worthless if an earlier one can be driven false,
because the hardened code never runs.

The five: the acknowledgement watermark (compared against "Bounced" rows), the
bounce numerator (same rows, and it short-circuited the watermark before it was
reached), `remaining -= 1` on the send path, the daily cap's starting value, and
the bounce **denominator** — which moved the rate in the *other* direction, since
pasting rows that carry a status and a date dilutes it.

The accidental cases matter more than the adversarial ones. Nobody has to be
trying: Google Sheets applying a date format to the SentAt column for readability
resets the daily allowance, and marking rows "Sent" to exclude them from a
campaign (a blank status is what makes a row eligible, so this is the natural
gesture) diluted the bounce rate to nothing.

**Known consequence of the fix:** `outreach_drafts` only became a complete send
log on 2026-07-26, so accounts whose sending predates it start with an empty
denominator and the bounce pause cannot fire until `MIN_SENDS_FOR_PAUSE` sends
have flowed through it. `stats()` reports `armed` and `min_sends_to_arm` so this
is answerable rather than silent. Hard-bounce *suppression* is unaffected — it
never depended on the rate.

When touching any guard, trace **every** value in its predicate to a source, not
just the one a bug report named:

| Guard | Inputs and their sources | Status |
| --- | --- | --- |
| Bounce pause | numerator ← `suppressions.created_at`, denominator ← `outreach_drafts.sent_at`, watermark ← `accounts.bounce_ack_count` | Hard. No sheet input to the verdict; the sheet is read only for the discrepancy message. |
| Daily send cap | `sent_today` ← `outreach_drafts.sent_at` (append-only), limit ← env | Hard. `sent_today` is a required argument with no default, by test. |
| Duplicate send | draft `status` ← `outreach_drafts` (written before the sheet), plus sheet status as a second layer | Hard, layered. |
| Reply drafting privacy | provider tier ← env | Hard. |
| Reply-check scope | row status ← sheet | **Soft, accepted.** Costs a missed reply, never an unwanted email. |
| Send consent gate | header row ← sheet | **Soft by design.** The header *is* the customer's consent signal; making it unforgeable would defeat its purpose. |
| Rate limits | hit timestamps ← process memory | **Soft, known.** Resets on restart; per-process. See the entry below — blocking at ~50 users. |

## Deferred from the product CEO review (2026-07-19, HOLD SCOPE)

### Google verification path (blocks billing)
**Status update 2026-08-23:** The combined `gmail.modify` contract described
below has been superseded. New connections authorize identity, Gmail
read-only monitoring, Gmail sending, and optional Sheets/Drive incrementally.
Legacy `gmail.modify` tokens remain usable only until those accounts reconnect.
Verification requirements must now be evaluated against the actual narrower
production grants, not the retired union scope.

**What:** Plan the exit from OAuth "Testing" status for the incremental
production scopes and confirm whether any remaining restricted scope requires
a third-party security assessment.
**Why:** Two consequences of Testing status are live today: (1) every
account's refresh token expires after 7 days — candidates' Google connections
self-destruct and it reads as product breakage (the reconnect flow works and
says "reconnect Google", but nobody warns them it's coming); (2) the $49
Pilot plan in `/api/plan` cannot actually be sold until verification is done,
and the CASA assessment is an unpriced cost sitting in front of it.
**Pros:** Unblocks real billing; kills the 7-day token death.
**Cons:** Verification is slow (weeks) and CASA costs real money; worth
researching whether narrower scopes could reduce the burden first.
**Context:** `outreach-agent/config.py` capability scope lists; `google_auth.py`
`get_credentials()` handles the expiry cleanly already. Validation week
mitigation: warn candidates, reconnect is one click (see VALIDATION-WEEK.md).
**Effort:** M (human, mostly waiting/paperwork) → M (CC can't compress Google)
**Priority:** P2 (bumped to P1 2026-08-06, reverted to P2 2026-08-07 when the
revamp plan deferred the filing to the 100-user ceiling)
**Depends on:** Comes before Billing (P3) can ship.
**Reconfirmed 2026-08-06 (`/plan-ceo-review`), corrected 2026-08-07:** A
niche-pivot review considered dropping to `gmail.send`-only to skip CASA
entirely. Turned out not to be free — `watch_replies.py`'s poll loop calls
`gmail.find_bounce()` off the same thread-read as reply detection, which
feeds `bounces.py`'s automated bounce-rate circuit breaker (`assert_sendable`
→ `pause_reason` → `SendingPaused`). That safety requirement still needs Gmail
read access, but it no longer justifies edit/delete permission. Monitoring now
uses `gmail.readonly`; sending is a separate `gmail.send` grant. The old
decision to keep `gmail.modify` is explicitly reversed.

### Background reply watcher — code done 2026-07-29, still blocked on the host
**What:** Server-side scheduler for reply checking, so detection doesn't
depend on someone having /outreach open in a browser tab.
**Why:** The homepage says the agent "watches your inbox" — today that's only
true while a tab is open (100s auto-poll; `watch_replies.py` as a daemon is a
manual script nobody runs). A prospect replying at night sits undetected.
**Pros:** Makes the core agent claim true 24/7.
**Cons:** Needs an always-on host first (currently a dev laptop + tunnel).
**Context:** `server.py` check_replies endpoint + `static/outreach.html`
auto-poll; `outreach-agent/watch_replies.py` `main()` already loops with
`schedule` — the missing piece is running it per-account somewhere durable.
**Effort:** M (human) → S (CC + gstack)
**Priority:** P2
**Depends on:** A real deployment (see Job persistence).

**Resolution:** `watch_replies.main()`/`run_all_accounts()` now walks every
account with a configured sheet in one process, sequentially ("stagger
accounts, don't fan out"), attributing usage per account via `usage.run_as`
and isolating one account's failure from the rest the same way
`check_for_replies` already isolates per row. `accounts_db.
list_accounts_with_a_sheet` deliberately filters on `google_sheet_id` alone,
not `google_token` — a dead token (which `google_auth.get_credentials`
already clears on `RefreshError`, and which happens to every account on a
7-day cycle until OAuth verification ships) would otherwise remove the
account from rotation entirely, freezing its `worker_heartbeat_at` at the
moment it broke and making "the worker is down" indistinguishable from
"this account needs a reconnect." A missing token is now a skip-with-reason
(`last_error = "Google disconnected — reconnect in Settings"`, heartbeat
still refreshed) instead of a silent exclusion — which also happens to be
the exact data a future reconnect-nudge notification needs.
Bundled in, since TODOS explicitly tied both to this landing: (d) below
(`sheets._verify_row_index`'s full-sheet re-read → a single-cell fast path),
and `accounts.worker_heartbeat_at`/`last_error` for observability without
reading process logs. A systemd unit template ships at
`outreach-agent/deploy/sendkeep-worker.service`. Cycle duration is measured
and a WARNING is logged if a cycle exceeds `CHECK_INTERVAL_MINUTES` — the
signal that the sequential single-process model itself needs to change
(sharding or real concurrency), not that the inter-account delay needs
shortening. Two heartbeat-integrity fixes made during review, not part of
the original ask: `set_worker_heartbeat` is best-effort (matches
`usage.record`'s own "metering must never break the action being metered"
convention — this write fires most often on the failure path, where
something's already gone wrong, and a second failure recording that must
not crash the loop); and `main()`'s single-account debug mode
(`python watch_replies.py you@company.com`) passes `write_heartbeat=False`
so a one-off manual run against one account can't overwrite
`worker_heartbeat_at` and forge the "the continuous worker is alive" signal
the column exists to give honestly. 15 new tests, 283/283 passing.
**Still blocks on the host**: this is one process meant to run continuously;
it still needs an always-on box to actually run on 24/7 (the dev laptop +
tunnel today). The code and the systemd unit are ready for that
conversation, not blocked on it.

### Job persistence
**What:** Persist background jobs (send batch, reply-check, lead search)
instead of the in-memory `JOBS` dict in `server.py`.
**Why:** Process restarts silently lose in-flight/recent job state — the
frontend sees a bare 404 with no way to tell "lost to a restart" from "bad
job id."
**Pros:** Removes a real failure mode; matters more as usage grows.
**Cons:** Real infra work (Redis or a Supabase jobs table) — not warranted
for a 1-3 user validation week.
**Context:** `server.py` `JOBS = {}`, `start_job()`, `_run_job()`.
**Effort:** M (human) → S (CC + gstack)
**Priority:** P2
**Depends on:** Nothing blocking.

### Split session-signing and token-encryption keys — done 2026-07-29
**What:** `APP_SECRET_KEY` currently signs session/OAuth-state tokens (HMAC)
*and* derives the Fernet key encrypting every stored Google token. Split into
two secrets.
**Why:** One secret's compromise means both session forgery and decryption of
every connected Gmail. Rotating it today also permanently breaks every stored
token.
**Pros:** Standard key-separation hygiene; makes rotation safe.
**Cons:** ~~Existing accounts must reconnect Google once when split.~~ See below.
**Context:** `outreach-agent/config.py`, `accounts_db.py` Fernet derivation.
**Effort:** S → S
**Priority:** P2 → **P1** (eng review 2026-07-28: a VPS holding this one secret
holds session forgery *and* decryption of every stored Gmail token, so the
split moves in front of the deploy rather than after it)
**Depends on:** Nothing blocking. **Blocks:** the VPS deploy.

> **How to do it, decided 2026-07-28.** Do **not** re-key the stored tokens.
> Introduce `SESSION_SIGNING_KEY` as the new secret and leave the Fernet
> derivation on the existing `APP_SECRET_KEY` value. That achieves the
> separation with **zero re-consent** — no stored token is ever re-encrypted.
>
> Rotating the token key instead would be a forced reconnect for every account
> delivered as an unhandled 500: `accounts_db.py:81` derives the Fernet key at
> import, and `google_auth.get_credentials` catches `RefreshError` but **not**
> `cryptography.fernet.InvalidToken`, which is what `decrypt_secret` raises
> when the key changes. On the headless worker that is an infinite
> `InvalidToken` loop with `consecutive_failures` climbing and no remedy.
> Add the `InvalidToken` catch alongside `RefreshError` regardless, so a
> future rotation degrades to "reconnect Google" instead of a 500.

**Resolution:** Implemented exactly as decided above.
`config.SESSION_SIGNING_KEY` defaults to `APP_SECRET_KEY` when unset, so no
deployment needs to act until it deliberately sets a distinct value.
`auth._sign` now signs with `SESSION_SIGNING_KEY`;
`auth.verify_unsubscribe_token` accepts a signature made under either
`SESSION_SIGNING_KEY` or `APP_SECRET_KEY` so links already sent survive a
future rotation, while sessions and OAuth state (both short-lived) do not
carry that backward-compatibility on purpose. `google_auth.get_credentials`
now catches `cryptography.fernet.InvalidToken` around the token decrypt,
clears the dead token, and raises the same "reconnect Google" `RuntimeError`
the `RefreshError` path already used. Two new tests
(`test_session_signing_key_split_preserves_unsubscribe_but_not_sessions`,
`test_get_credentials_clears_token_on_fernet_key_rotation`) — 242/242 passing.

### Reply-detection correctness: scan since last outgoing, not newest-only — done 2026-07-29
**What:** `gmail.get_latest_reply_with_history` only inspects `messages[-1]`.
Replace with "scan all messages after our last outgoing message" (or a
per-thread `historyId` cursor), and decide a policy for replies arriving
from an address other than the sheet's Email cell (assistant/alias).
**Why:** Two silent drop paths (eng review 2026-07-19, outside voice):
(a) operator replies manually from the Gmail UI before a poll runs → the
contact's earlier reply is buried and never reviewed; (b) contact replies
from a different address → dropped every poll, forever. Both lose booked-
meeting signal with zero operator indication.
**Pros:** Closes the only known ways to silently lose a reply; the cursor
also kills the ~900 Gmail calls/hour polling waste (same mechanism).
**Cons:** Real design work (unknown-address policy: auto-accept vs flagged
review); touches the most safety-critical read path.
**Context:** `outreach-agent/gmail.py` `get_latest_reply_with_history`;
week mitigation is procedural (VALIDATION-WEEK.md: reply from inside the
app only).
**Effort:** M (human) → S (CC + gstack)
**Priority:** P2
**Depends on:** Nothing blocking.

> **SUPERSEDED 2026-07-28.** Do not implement the approach described above.
> "Scan all messages after our last outgoing message" **does not fix drop path
> (a)**: when the message that landed since is the operator's own manual reply,
> the contact's reply is still behind the boundary. Any ordering-derived
> boundary has this hole. The `historyId` cursor is also mis-specified —
> Gmail's historyId is mailbox-level (`users.history.list` takes one
> `startHistoryId`), not per-thread.
>
> **The agreed rule is dedupe-driven:** the newest message from the contact
> that has no review row yet, keyed on the **Gmail message id** (not the body
> text — `find_review_id`'s exact-`customer_reply` match collides on short
> replies, cannot be uniquely indexed past ~2704 bytes, and sends multi-KB
> bodies through a PostgREST GET parameter). Sender classification runs
> **before** reply candidacy with precedence `daemon > auto-responder >
> on-sheet > off-sheet`, or `find_bounce` (gated on `if not reply_text`)
> becomes unreachable and bounce suppression dies silently. A contact message
> that one of our own messages postdates is queued `answered_elsewhere` —
> visible, no draft, no notification — or the fix trades a missed reply for
> double-replying a prospect.

**Resolution:** Implemented as decided above. `gmail._classify_message`
(shared by `get_latest_reply_with_history` and the new `get_history_before`,
so both agree on "from the contact" by construction) classifies each message
`ours > daemon > auto_responder > on_sheet/off_sheet` in that precedence,
using the full set from the new `gmail.get_own_addresses` (primary address
plus every verified Send-As alias — not just one address, or the operator's
own alias replies would read as an unconfirmed stranger). An off-sheet reply
is queued `status="flagged"`, drafted only after the operator confirms the
sender via the new `POST /api/outreach/replies/{id}/confirm-sender`
(deferred rather than drafted at detection: an unconfirmed sender is the
least-trusted input this system sees, and drafting it first would run
untrusted input through the LLM before any check, backwards from how every
other path here treats trust). `answered_elsewhere` is queued visible with
no draft. Two gaps found and closed while implementing, not part of the
original decision: `send_reply` never checked `suppressions_db` at all
(an unsubscribed contact could still receive a reply — now a 409, same as
the outreach-send path); `original_draft_reply` was written as `""` instead
of `NULL` on any drafting failure, which Phase 6's edit-diff learning would
have read as "replace the model's output wholesale" rather than "the model
wrote nothing." A `degraded_classification` column (boot-checked via the
protected schema contract, not a manual migration — `add_review` is
the insert path for every review, so a missing column here stops reply
detection entirely, not just one button) marks any review created while the
Send-As lookup itself failed and fell back to just the primary address. 29
new tests, including one that runs the real classifier through
`check_for_replies` rather than the usual full mock, specifically to catch a
`kind` string drifting between the two — 271/271 passing.

**Two loose ends closed during review, recorded here since the review that
found them predates this file's last edit:**
- `get_history_before`'s target email was unstated and the first
  consistency test used the same email for both functions, which couldn't
  have caught the two disagreeing. Fixed: both
  `get_latest_reply_with_history` and `get_history_before` now label
  history against the candidate's own observed From address (not
  `contact_email`), sharing that judgment rather than each re-deriving it.
  Regression test uses a genuinely divergent (off-sheet) target, not the
  on-sheet case where the two happen to be the same string.
- `get_latest_reply` (backing `check_single_reply`) calls
  `get_own_addresses` fresh every call rather than caching it the way
  `check_for_replies` fetches it once per cycle. Decided, not fixed: that
  endpoint is a one-shot per-row action, never a loop, so the extra Gmail
  call doesn't compound. Documented in a comment on `get_latest_reply`
  naming the condition under which this would need to change (a future
  caller that iterates rows through it).

**Follow-up, not done here:** `plans.check(account, usage.UNIT_DRAFT_REPLY)`
is now called before every reply draft (previously called nowhere), but
`plans.py`'s `monthly_replies` is `None` for every plan tier, so the gate is
structurally wired and currently a no-op. This was an accepted gap when only
a contact you had actually emailed could trigger a reply draft; after this
change, anyone who gets a message into a thread you own can, once an
operator confirms the sender. Deciding real `monthly_replies` numbers per
tier is a pricing decision, same class as the rest of `plans.py` — not made
here.
**Effort:** XS · **Priority:** P2
**Depends on:** Nothing blocking.

### Operational hygiene bundle
**What:** (a) structured logging — timestamp + account id + action + outcome
on send/reply paths instead of bare `print()`; (b) `JOBS` dict cleanup (drop
entries after N hours); (c) HTML-only replies reach the LLM/review UI as raw
markup — strip tags in `gmail._extract_body`'s fallback; ~~(d) narrow
`sheets._verify_row_index` to an email-column-only range read~~ — **done
2026-07-29** as part of the background watcher, see that entry above: a
fast path reads just the one email cell first and only falls back to
`get_all_rows` (the whole sheet) if that cell doesn't confirm the row is
still where it was — today every
guarded write re-reads the whole sheet (~2 Sheets calls per send; fine at
25-row scale, brushes read quota at 500-row sheets with concurrent polling).
**Why:** None bites at current scale; all four bite with the second tenant.
**Updated 2026-07-27 (CEO review):** (d) is promoted to **P2** and is now a
phase-1 dependency, not hygiene. The background watcher turns "every guarded
write re-reads the whole sheet" from occasional into continuous, per account,
every cycle. (a) is superseded for the worker specifically by the
`worker_events` entry below; it still stands for the send path in `server.py`.
**Pros:** Debuggability three weeks after the fact; less quota burn.
**Cons:** Pure hygiene, no user-visible change.
**Context:** `server.py`, `outreach-agent/watch_replies.py`, `gmail.py`,
`outreach-agent/sheets.py`.
**Effort:** M (human) → S (CC + gstack)
**Priority:** P3 overall for (a)-(c); (d) done, see above.
**Depends on:** Nothing; natural trigger for (a)-(c) is "before the second
real tenant."

### Sheet read pagination + table virtualization
**What:** Paginate Sheets reads and virtualize the campaigns/leads table
render instead of loading and rendering the whole sheet every call.
**Why:** `sheets.get_all_rows()` has no range limit; the frontend renders the
entire table in one `innerHTML` write.
**Pros:** Prevents slowdown as lead lists grow.
**Cons:** Irrelevant at today's scale.
**Context:** `outreach-agent/sheets.py`, `static/outreach.html`, `static/leads.html`.
**Effort:** M (human) → S (CC + gstack)
**Priority:** P3
**Depends on:** Real usage growth to justify it.

### Per-account send lock — done 2026-07-19
**What:** ~~Prevent concurrent `send_outreach.main()` runs for the same
account.~~ Landed from the stash into `server.py` (`_accounts_sending` guard),
plus per-row Sent marking in `send_outreach.py` so a crash mid-batch can't
cause re-sends either. Both duplication paths closed.

### Security hardening pass — done 2026-07-22
**What:** Five fixes from a backend review, all with tests
(`tests/test_outreach_agent.py`, +5 cases → 61 total):
1. **Rate limiter was a no-op behind the tunnel.** `/login` + `/signup`
   keyed on `request.client.host`, which is `127.0.0.1` for every visitor
   behind cloudflared — so the per-IP limits were global (one attacker's 10
   login attempts locked out every customer). Now keyed on
   `_client_ip()` → `CF-Connecting-IP` (authoritative at Cloudflare's edge),
   falling back to the socket peer for local/dev. `X-Forwarded-For` is
   deliberately not trusted (spoofable without a known proxy chain). This
   corrects the "rate limiting done 2026-07-17" claim below, which was only
   ever verified on localhost.
2. **Pre-registration account takeover via Google SSO.** `signup` never
   verified emails, and `link_or_create_google_account` silently merged a
   Google identity into any existing account with the same email. An attacker
   who pre-registered `victim@corp.com` with a password got the victim welded
   into that account on their first Google sign-in — attacker keeps the
   password, gains any Gmail token the victim later connects. Now the merge
   refuses a password-holding account (`AccountLinkBlocked` → `/login?
   google_error=account_exists`, "sign in with your password"); only
   password-less accounts auto-link.
3. **Job errors leaked internals.** `_run_job` returned `str(e)` for every
   exception. Now only `RuntimeError` (the app's user-safe message convention)
   is surfaced; anything else is logged server-side and returns a generic
   message.
4. **`ratelimit._hits` grew unbounded** — one-off IP keys never evicted. Added
   an opportunistic sweep (drops keys whose newest hit predates the largest
   window in use).
5. **`tavily_search` docstring lied** ("returns [] on failure" but raised).
   Now genuinely returns `[]` on a request/JSON failure (so one flaky query
   doesn't abort a 3-query lead search) and meters only on success; still
   raises loudly for the missing-key config error.
**Still open (follow-ups, NOT done here):** ~~Email verification on signup +
password reset flow~~ / ~~authenticated "link Google identity to my existing
account" flow~~ — both superseded 2026-08-06: password auth (and signup
itself) was deleted outright rather than hardened (see PLAN-WEEK-2026-08-05.md,
M1). There is no password to verify, reset, or link against anymore, so
neither follow-up applies. Items 1 and 2 in the numbered list above describe
`_client_ip`-keyed rate limiting and `AccountLinkBlocked`, both of which were
deleted in the same change — vestigial, not reverted; the takeover vector they
closed can't recur without a password path to take over.

### Auth hardening — closed 2026-08-06 (resolved by deletion, not built)
**What:** ~~Rate limiting on `/login` and `/signup`~~ **done 2026-07-17**
(proxy-IP correctness fixed 2026-07-22). Email verification on signup and
password reset were never built — instead, password auth itself was deleted
(PLAN-WEEK-2026-08-05.md, M1): Google SSO is now the only sign-in path, so
there is no unverified email to abuse and no password to lose or reset.
**Context:** `accounts_db.create_account`, `auth.hash_password`/`verify_password`,
and the `/signup`+`/login` POST handlers no longer exist.

### Session revocation — deferred by decision, not oversight
**What:** No server-side session store or `session_version` column; a stolen
session cookie stays valid until `auth.SESSION_TTL_SECONDS` expires (shortened
14 → 7 days 2026-08-06 alongside the password-auth deletion — see
PLAN-WEEK-2026-08-05.md, M5). There is no way to force-expire one account's
sessions early (e.g. "sign out everywhere").
**Why not built now:** `auth.verify_session_token` is a pure HMAC check with
zero I/O. Adding a revocation list/version would turn it into a database read
on every request through `require_auth` — i.e. on every authenticated
request the app serves. With the password vector gone, the main way a session
gets stolen (credential stuffing / leaked password) no longer applies, and
Testing-mode Google tokens already die on their own after 7 days, which
self-limits the exposure window without that cost.
**Revisit when:** the app leaves Google OAuth Testing mode, or there's a
concrete report of a stolen/leaked session cookie.
**Priority:** P3

### Deliverability infra
**What:** SPF/DKIM/DMARC (DNS-level, not code), per-account send warm-up ramp,
multi-inbox rotation guidance.
**Why:** Sending from a fresh domain/account without warm-up gets flagged as spam;
this determines whether outreach actually lands in inboxes.
**Pros:** Directly determines whether the product's core value (emails that get read)
actually works at scale.
**Cons:** Partly outside the codebase (DNS records), partly a product decision
(how aggressive is the warm-up ramp).
**Context:** `outreach-agent/send_outreach.py` currently sends without any ramp-up
or rotation logic.
**Effort:** L (human) → M (CC + gstack)
**Priority:** P2
**Depends on:** Having a second real customer to warm up for.

### Billing
**What:** Plans/limits tied to the existing `usage_events` table.
**Why:** Usage is already metered (`accounts_db.record_usage`) but nothing enforces
limits or charges for it.
**Pros:** Turns metering data already being collected into actual revenue/limits.
**Cons:** Needs a payment provider integration and plan-tier design first.
**Context:** `usage_summary()` in `outreach-agent/accounts_db.py` already aggregates
per-account usage by kind — the raw data this would build on already exists.
**Effort:** L (human) → M (CC + gstack)
**Priority:** P3
**Depends on:** Auth hardening (P2) probably lands first — no point billing accounts
that can be created via a brute-forceable signup.

### Removed NeverBounce entirely — done 2026-07-18
**What:** Deleted `outreach-agent/email_verification.py` and every call site
(`leads.py`'s verify pass, `server.py`'s `/api/outreach/campaigns/verify`
endpoint, `static/outreach.html`'s "Verify pending emails" button). Removed
`NEVERBOUNCE_API_KEY` from `config.py`.
**Why:** NeverBounce's trial credits were exhausted (0 balance, see the
three entries below) and blocked every real send. Decision: replace
automated verification with manual verification — `sheets.py::is_verified()`
already just checks whether the sheet's `EmailConfidence` column literally
says `"verified"`, so a sheet owner can type that in by hand for any contact
they've personally confirmed. The fail-closed gate itself (unverified
contacts are never eligible to send) is unchanged.
**Context:** New leads from lead-sourcing now always land as `"unverified"`
in the sheet — no automated verification attempt happens at all anymore.
The three entries below (logging, parallelization, tests) describe work
done on the now-deleted module; kept for history, not actionable.
**Effort:** M (human) → S (CC + gstack)
**Priority:** Done.
**Depends on:** Nothing.

### Verified-leads gate restored as a hard gate — done 2026-08-10
**What:** `sheets.campaign_readiness` now excludes any row whose
`EmailConfidence` reads `unverified` (case-insensitive), so a machine-guessed
address is never drafted and never counted toward a batch. The send paths
re-check the *live* cell at send time — `send_all_prepared` retires a pending
draft whose address now reads "unverified" (`send_outreach.py`), and
`_guard_draft_send` refuses the single-draft send with a 409 (`server.py`) —
because a draft can exist from before the gate or the sheet can be edited
after drafting. `sheets.email_confidence()` is the shared send-time lookup.
Blank confidence (rows the owner typed or imported themselves) and "verified"
both send; only the "unverified" machine-guess stamp blocks. Copy that
described the column as informational was corrected wherever it lived:
`config.py`, `sheet_template.py`, `FUNCTIONS.md`, `app/src/GettingStartedPage.tsx`
(rebuilt), and `docs/getting-started.md`.
**Why:** The entry above claimed the fail-closed gate was "unchanged" — but
`is_verified()` and the eligibility filter had quietly rotted away, and
`config.py` described EmailConfidence as "informational only... any approved
row with an email can send." A 2026-08-10 roast review (reshape: keep the
verified-leads requirement as a hard gate instead of a suggestion) caught
guessed, unverified addresses going out as designed behavior. This restores
the gate the docs claimed existed.
**Context:** New leads still land as `"unverified"` (`leads.py`) and must be
confirmed by a human before they can be emailed; there is no automated
verification to lean on anymore.
**Effort:** S (CC + gstack)
**Priority:** Done.
**Depends on:** Nothing.

### Log email_verification failures / surface NeverBounce credit exhaustion — done 2026-07-17
**What:** `email_verification.py` has no logging. Two independent /autoplan
outside-voice reviews (CEO phase + Eng phase, 2026-07-17) converged
separately on the same underlying risk: `NEVERBOUNCE_API_KEY` is a single
server-wide credential shared across all tenants in this multi-tenant app,
and NeverBounce meters credits, not per-tenant. When credits run out, every
account's `verify()` calls silently fall back to `"unverified"` — identical
to the "not configured" state this plan is trying to fix — with zero signal
to the operator about which tenant burned the pool or that it happened.
**Why:** Without a log line (or NeverBounce's balance-check endpoint), a
credit-exhaustion event is indistinguishable from "these leads are bad" or
"the feature was never turned on," during exactly the live-send week this
is meant to unblock.
**Pros:** Cheap observability win; closes a gap two independent reviews
found from different code paths.
**Cons:** None identified.
**Context:** No `logging` import or print statement anywhere in
`outreach-agent/email_verification.py`. Consider also polling NeverBounce's
balance endpoint and moving to per-account keys before onboarding a second
real paying customer.
**Effort:** XS (human) → XS (CC + gstack)
**Priority:** P2
**Depends on:** Nothing.
**Resolution:** Logging added. Immediately useful: it surfaced that this
deployment's real NeverBounce trial credits are already exhausted (0
balance) — right now every verification silently fails closed to
`"unverified"` for that reason, not because addresses are unverifiable.
Needs a real top-up before the verification gate does anything.

### Batch/parallelize email_verification calls in lead discovery — done 2026-07-17
**What:** `leads.py:109` calls `email_verification.verify()` synchronously,
one HTTP call per candidate, 20s timeout each, no concurrency or backoff.
**Why:** A `limit=10` lead search can add up to ~200s of latency to one
request in the worst case (found by /autoplan's eng-phase outside voice,
2026-07-17).
**Pros:** Meaningfully faster lead searches at moderate limits.
**Cons:** Adds concurrency complexity to a currently simple, easy-to-reason-
about loop; NeverBounce rate limits would need checking before parallelizing.
**Context:** `outreach-agent/leads.py:109`, inside `find_leads()`.
**Effort:** S (human) → S (CC + gstack)
**Priority:** P3
**Depends on:** Nothing blocking; lower priority than the misclassification
fix above since latency (not correctness) is the cost.
**Resolution:** Split into a sequential filter/dedup pass followed by a
`ThreadPoolExecutor(max_workers=4)` verification pass, same concurrency
pattern as `send_outreach.py`. Dedup/limit semantics unchanged, verified by
`outreach-agent/tests/test_leads.py`. NeverBounce's actual concurrent-request
limit was not independently verified — worth confirming if lead-search volume
grows.

### Unit tests for email_verification.py response parsing — done 2026-07-17
**What:** Add tests covering `verify()`'s branches: valid, invalid, timeout,
malformed/no-result response, missing key.
**Why:** This module gates whether real emails get sent to real people —
worth locking down given it's fail-closed by design and easy to regress.
**Pros:** Catches regressions in the fail-closed logic before they ship.
**Cons:** Repo has zero test infrastructure today (confirmed via `/health`,
2026-07-16) — this would be the first test in the project.
**Effort:** S (human) → S (CC + gstack)
**Priority:** P3
**Depends on:** Picking a test framework for the repo (not scoped here).
**Resolution:** Added `outreach-agent/tests/` using stdlib `unittest` +
`unittest.mock` (zero new dependencies) rather than picking a framework —
9 cases for `email_verification.py`, plus 5 for `leads.py` and 5 for
`ratelimit.py` added alongside other fixes this session (19 total).
**Note:** a stray `tests/__pycache__/test_rate_limit.cpython-313-pytest-9.1.1.pyc`
exists at the repo root with no matching `.py` source on disk — evidence an
earlier session wrote a `pytest`-based test at `tests/test_rate_limit.py`
that never got committed (or was deleted). `pytest` is installed locally but
not in `requirements.txt`. Worth an explicit decision (`pytest` vs stdlib)
before this grows into two parallel conventions.

### Cleanup: stale reviews.db
**What:** Remove `outreach-agent/reviews.db` (old SQLite file).
**Why:** Dead since the migration to Supabase Postgres (see commit `8481239`).
Harmless but confusing to a new engineer wondering which store is live.
**Pros:** Removes a red herring from the codebase.
**Cons:** None — purely dead weight.
**Context:** Confirmed unused by any current code path as of the 2026-07-16 review.
**Effort:** S (human) → S (CC + gstack)
**Priority:** P3
**Depends on:** Nothing.

## From /qa run (2026-07-20, pre-connect candidate state)

### Serve HTML routes with Cache-Control: no-cache
**What:** Page routes (`/outreach`, `/settings`, `/login`, `/signup`, `/`)
send no Cache-Control header, so browsers heuristically cache the HTML+JS.
**Why:** Observed live during QA: after a fix landed in `static/outreach.html`,
a plain reload still ran the old JS; only a cache-busted URL picked it up.
If a fix ships mid-validation-week, a candidate's browser may keep the old
page until a hard refresh — right when a stale bug matters most.
**Fix:** Add `Cache-Control: no-cache` to the HTML page responses in
`server.py` (static assets can stay cacheable).
**Effort:** S · **Priority:** P2 (before shipping any mid-week fix to candidates)

### Add a favicon
**What:** `/favicon.ico` 404s for logged-in users — one console error per tab.
**Why:** Cosmetic, but it's the only recurring console 404 and shows up in
every QA console sweep as noise.
**Effort:** S · **Priority:** P3

## From /qa run (2026-07-23, branch feat/lead-sheet-template-and-guide)

Fixed in that run and not repeated here: the five missing legal pages
(`/privacy`, `/terms`, `/acceptable-use`, `/dpa`, `/security`), `/docs` serving
Swagger UI, and the raw-JSON 404. Report:
`.gstack/qa-reports/qa-report-localhost-8000-2026-07-23.md`.

### Decide the LLM route that sees prospect reply text
**What:** `agent.draft_reply()` sends the prospect's own message to OpenRouter,
and `config.OPENROUTER_MODEL` defaults to `openai/gpt-oss-20b:free`.
**Why:** Free inference routes commonly permit the provider to retain or train
on submitted prompts. The prompt here is a third party's private email, which
the customer is data controller for. It is the first thing a serious vendor
review will ask about, and the new `/privacy` and `/dpa` pages now disclose it.
**Fix:** either pin a paid model carrying a no-training term, or keep the free
route and state the tradeoff as a deliberate product position.
**Context:** `outreach-agent/agent.py:173`, `outreach-agent/config.py:22`.
**Effort:** S (human) → S (CC + gstack) · **Priority:** P2
**Depends on:** Nothing; it is a decision, not a build.

### Legal pages need a lawyer's review before charging anyone
**What:** `/privacy`, `/terms`, `/acceptable-use` and `/security` were written
from the codebase and are factually accurate, but are marked "draft pending
legal review" on the page itself. `/dpa` deliberately publishes no contract
text, since a DPA is a signed Article 28 agreement rather than a notice.
**Why:** They unblock the broken links and Google's OAuth verification
requirement today. They are not a substitute for drafted terms once money
changes hands, and `/terms` in particular lacks governing law, jurisdiction,
and entity-appropriate warranty disclaimers.
**Fix:** have a lawyer produce the real versions; keep the sub-processor table
in `/dpa` in sync with the code when a provider is added or removed.
**Context:** `static/privacy.html`, `terms.html`, `acceptable-use.html`,
`dpa.html`, `security.html`.
**Effort:** M (human, mostly external) · **Priority:** P2 (before billing)
**Depends on:** Billing work; the two land together.

### Session for a deleted account 500s instead of clearing the cookie
**What:** A validly signed session whose `account_id` no longer resolves raises
an unhandled `postgrest.APIError` → 500.
**Why:** Not reachable today: tokens are HMAC-signed, so the id cannot be chosen
without `APP_SECRET_KEY`. It becomes reachable the day account deletion ships,
or after any accounts-table migration, where the right behaviour is to clear the
cookie and redirect to `/login`.
**Effort:** S · **Priority:** P3
**Depends on:** Pairs naturally with account deletion.

### Legacy pages still load Tailwind from the CDN
**What:** `/login` (and the other not-yet-migrated pages) pull
`cdn.tailwindcss.com`, which compiles styles in the browser on every load.
**Why:** Puts a third-party origin on the critical render path of the sign-in
page; if the CDN is slow or blocked, sign-in renders unstyled. The React pages
under `static/app/` already compile Tailwind at build time.
**Fix:** falls out of the React migration already in progress. Tracked so it is
not mistaken for a regression in the meantime.
**Effort:** absorbed by the migration · **Priority:** P3
**Depends on:** The page-by-page React conversion.

### Delete the QA account
**What:** `/qa` created `qa-2026-07-23@example.com` to test the logged-in pages.
It has no Google token and no sheet, so it can send nothing.
**Why:** It is a real row in the accounts table, and self-service account
deletion does not exist yet, so it needs removing by hand.
**Effort:** S · **Priority:** P3

## Deferred from the daily-use build CEO review (2026-07-27, `/plan-ceo-review`)

Filed while planning the reply-detection / background-watcher / notification
work. Mode was HOLD SCOPE over all seven phases; these are the items explicitly
kept out of that scope, plus two existing entries this plan promotes.

### Land the current branch before phase 0 — merged 2026-07-29, review still owed
**What:** `feat/outreach-drafting-optout` carries +8,860/-448 across 55 files,
unmerged and never shipped, and all seven phases of the daily-use build land on
top of it.
**Why:** Every phase compounds review surface on a base that has never been
exercised in production. A defect in the base and a defect in phase 1 are
indistinguishable from the outside.
**Pros:** Bounds the blast radius of everything that follows; makes the first
worker deploy a change to a known-good base.
**Cons:** Landing costs a review cycle before any of the new work starts.
**Context:** `git diff main --stat` on the branch. Nothing in the daily-use
plan sequences this.
**Effort:** S (human) → S (CC + gstack) · **Priority:** P1
**Depends on:** Nothing. Blocks phase 0.

**Resolution:** `main` was fast-forwarded to the branch tip and pushed. **The
review this entry was named for did not happen first** — landing was chosen
over reviewing, at the time, for speed. `d755f22` alone (+4,280 lines, "the
billing, plans, bounce and metering work") is still sitting on `main` today
having never been through `/review` or equivalent. This is not resolved by
the merge; it's a live, disclosed risk. `/review` on that diff (or a
targeted audit of `billing.py`/`plans.py`/`bounces.py`) is still the
highest-value thing to do before trusting `main` as a base for anything
further.

### Migration tooling (resolved)
**What:** The schema previously depended on hand-run SQL fragments and an
inline `_MIGRATED_COLUMNS` boot check.
**Resolution:** `supabase/migrations/20260827000000_sendkeep_baseline.sql` is
now the canonical, idempotent baseline. `outreach-agent/schema_contract.py`
calls the protected `sendkeep_schema_contract()` RPC to verify columns,
nullability, indexes, RLS, and check constraints. CI rebuilds the database and
runs pgTAP plus `supabase db lint`; production changes go through the protected
GitHub Actions workflow described in `docs/operations.md`.
**Follow-up:** Create a separate forward-only migration for each future schema
change and rehearse it in staging before production. Do not edit the deployed
baseline or reintroduce application-owned DDL.

### get_credentials should update the caller's account dict
**What:** `google_auth.get_credentials` persists a refreshed token to Supabase
but does not write it back into the `account` dict it was handed, while
`accounts_db.get_google_token` reads the token *from that dict*.
**Why:** Any long-lived holder of an account dict therefore re-reads a stale
token every time, forces an OAuth refresh, and never sees its own write. The
worker avoids this by re-reading the row each cycle (decided in review), but the
shape stays present for every future caller. Two lines at the source fixes it
everywhere.
**Pros:** Removes the defect rather than routing around it.
**Cons:** None material.
**Context:** `outreach-agent/google_auth.py:56-78`,
`outreach-agent/accounts_db.py:241-245`.
**Effort:** S → S · **Priority:** P2
**Depends on:** Nothing blocking.

### Batch the reply-review lookup per cycle
**What:** The new detector rule ("newest contact message with no review") calls
`reviews_db.find_review_id` once per contact message per row per cycle — roughly
50 Supabase round trips per account per cycle at 25 rows.
**Why:** One query per account per cycle, fetching that account's reviewed
`(thread_id, customer_reply)` pairs into a set, replaces all of them. Needs a
composite index on `reviews(account_id, thread_id)`, which the hot path does not
have today. (Corrected 2026-07-28: an earlier draft of this entry named a
non-existent `outreach_replies` table. The reviews table is `reviews` —
`reviews_db.py:8`. It already has `reviews_account_status_idx (account_id,
status)`, which covers the status-based queue queries; only the thread_id
lookup is unindexed.)

**Superseded 2026-07-28 by the eng review:** dedupe moves off the body text
entirely and onto the Gmail message id, so the index to add is
`unique (account_id, thread_id, gmail_message_id)` — which also makes
`add_review`'s check-then-insert race-safe rather than "not airtight". See the
eng-review task list.
**Pros:** Removes an N+1 on the loop that now runs continuously.
**Cons:** None; strictly an optimization of a correct rule.
**Context:** `outreach-agent/reviews_db.py` `find_review_id`;
`outreach-agent/watch_replies.py` per-row loop.
**Effort:** S → S · **Priority:** P2
**Depends on:** Phase 0 landing first — done. **Deliberately still not done
2026-07-29:** the background watcher (this dependency) landed and explicitly
left this out of scope — TODOS' own "does not bite below ~10 accounts" and
the account count today being nowhere near that. Revisit when it is.

### Per-thread last_processed_message_id cursor
**What:** Store the last message id processed per thread, so a cycle skips
everything at or before it.
**Why:** `sheets.get_reply_check_rows` deliberately keeps `Replied` rows in
scope forever so later messages keep being picked up, which means every cycle
re-reads every message of every Sent/Replied/Delayed thread. Cost grows with
thread length × rows × accounts × 288 cycles/day, without bound. Correctness is
already handled by superseded-marking; this is purely about cost.
**Pros:** Bounds per-cycle work as conversations get longer.
**Cons:** More state to keep correct, and a wrong cursor loses replies silently
rather than duplicating them — the worse failure direction.
**Context:** `outreach-agent/gmail.py`, `outreach-agent/sheets.py`
`get_reply_check_rows`.
**Effort:** M (human) → S (CC + gstack) · **Priority:** P3
**Depends on:** Phase 0 landing first. Does not bite below ~10 accounts.

### Structured logging beyond the heartbeat row
**What:** A `worker_events` table capturing account, action, outcome and
timestamp per cycle, plus a retention policy. Supersedes item (a) of the
operational hygiene bundle below for the worker specifically.
**Why:** The heartbeat row answers "is it working right now"; it does not answer
"why did this break three weeks ago". A headless multi-tenant process whose
logging strategy is `print()` is not debuggable after the fact.
**Pros:** Post-hoc reconstruction of any account's detection history.
**Cons:** A table and a retention policy to own.
**Context:** `outreach-agent/watch_replies.py`; the hygiene bundle entry above.
**Effort:** M (human) → S (CC + gstack) · **Priority:** P2 (was P3 as hygiene;
phase 1 promotes it)
**Depends on:** Phase 1 landing first — **done 2026-07-29.** Phase 1 shipped
a lighter version of this specifically (`accounts.worker_heartbeat_at` /
`last_error`, one row per account, overwritten each cycle) — it answers "is
it working right now," which this entry's own "why" explicitly says is not
the same question as "why did this break three weeks ago." Still open.

### Phase 6b: suggested instruction updates from edit diffs
**What:** After enough captured edit pairs, offer the user a concrete, approved
change to `custom_instructions` ("you've been cutting the second paragraph and
shortening the ask — write that way by default?").
**Why:** Deferred from the daily-use build. The capture half (phase 6a,
`original_subject` / `original_body` / `original_draft_reply` written at insert)
ships now because it is the only work in the plan that is lossy to defer —
`drafts_db.mark_sent` and `reviews_db.mark_sent` currently overwrite the model's
original in place, so every day without capture destroys corpus permanently. The
*suggestion* half is deferred because the agreed gate cannot be met yet.
**Gate agreed in review:** suggest only when the same edit shape appears in a
clear majority of the last N pairs with N ≥ 30, and show the user the actual
examples being generalized from, not a bare count. Visible, reversible, never
silent. Fails safe: no pattern means no suggestion, forever.
**Pros:** The honest form of lock-in — month three is measurably better than
week one.
**Cons:** The only one-way door in the plan. Inferred edits shape
`custom_instructions`, which shapes output, which shapes the next edits; a wrong
early generalization entrenches because the user cannot tell "it learned my
voice" from "it learned its own last mistake". "A consistent pattern across
pairs" is also a real component hiding behind one sentence.
**Context:** `outreach-agent/drafts_db.py` `mark_sent`,
`outreach-agent/reviews_db.py` `mark_sent`, `outreach-agent/agent.py`
`_custom_instructions_block` (already fences safely).
**Effort:** L (human) → M (CC + gstack) · **Priority:** P3
**Depends on:** Phase 6a shipping. **Trigger to revisit:** any account reaching
30+ captured edit pairs.

## Deferred from the daily-use build ENG review (2026-07-28, `/plan-eng-review`)

### Decide the reply-drafting provider before the VPS deploy
**What:** On the VPS, reply drafts will go to a free-tier LLM endpoint that may
train on them, carrying the prospect's own words.
**Why:** `providers.chain_for(DRAFT_REPLY)` returns paid-only providers when any
paid provider is configured. The only entry marked paid is `cliproxy`, whose
`enabled_when` is `CLIPROXY_BASE_URL` — which `config.py` describes as
"Local-dev-only backend: a CLIProxyAPI instance on this machine... Unset in
every real deployment." So on the laptop today, reply drafting goes to a paid
endpoint; on the VPS the paid chain is empty and it falls through to Groq's
free tier. `providers._warn_free_tier_replies` signals this with a single
`print()` — on a headless box, once per process. Volume also rises by an order
of magnitude at the same moment, because drafting stops requiring an open tab.
**Pros:** Either outcome is defensible; what is not defensible is the change
happening silently at deploy time.
**Cons:** A paid endpoint is real recurring cost on a pre-revenue product.
**Options:** (a) configure a paid endpoint and set
`REPLY_DRAFTS_REQUIRE_PRIVATE_ENDPOINT=1`, which already exists for exactly
this and hard-fails rather than falling back; or (b) accept the exposure
knowingly and update `static/privacy.html` and `static/dpa.html`, which promise
a complete sub-processor list and are enforced by
`tests/test_outreach_agent.py:3357`.
**Context:** `outreach-agent/providers.py` `chain_for`;
`outreach-agent/config.py` `REPLY_DRAFTS_REQUIRE_PRIVATE_ENDPOINT` (off by
default, with a comment saying to turn it on "once a paid endpoint is
configured and a customer DPA depends on it" — the VPS deploy is that moment).
**Effort:** S → S · **Priority:** P1
**Depends on:** Nothing. **Blocks:** the VPS deploy.

**Status update 2026-07-29 (P1 execution pass):** option (b) is already done
— `static/privacy.html:111-117` names the split plainly ("Drafting a reply...
is routed to a paid endpoint whenever one is configured... Where no paid
endpoint is configured, reply drafting runs on a free tier and the training
exposure above applies") and the sub-processor and free-tier-training tests
(`test_legal_pages_name_every_llm_provider_that_can_receive_lead_data`,
`test_legal_pages_disclose_free_tier_training_exposure`) already pass. No
code or docs gap remains. What is still open is purely a deploy-time choice
with no code attached to it: when the VPS is actually provisioned, either
configure a paid endpoint there (option a — e.g. `GEMINI_API_KEY` with
`GEMINI_TIER=paid`, plus `REPLY_DRAFTS_REQUIRE_PRIVATE_ENDPOINT=1`) or accept
the already-disclosed free-tier fallback as-is. Staying P1 because it still
blocks the VPS deploy; re-open this specifically at provisioning time rather
than treating it as build work now.

### Manual "Check now" semantics once the worker exists
**What:** `POST /api/outreach/replies/check` (`server.py:884`) runs the reply
detector in a server-process thread. Once a worker polls continuously, two
processes run the same detector.
**Why:** The double-insert itself is closed by the unique constraint on
`(account_id, thread_id, gmail_message_id)` decided in the eng review, so this
is no longer a correctness bug — it is an unresolved product question. The
button must survive phase 3, because it is the user's only recourse when the
worker has stalled. What is undecided is whether it keeps running the detector
directly (instant feedback, two processes detecting) or becomes a nudge that
sets `next_due_at = now()` on the account's `scheduled_jobs` row (single
writer, up to a 15s wait, and a silent no-op if the worker is down — which is
exactly the situation that made the user press it).
**Pros:** Deciding it deliberately avoids a button whose behaviour differs
depending on whether a background process happens to be healthy.
**Cons:** Neither option is clearly right; it depends on how visible worker
health ends up being in the UI.
**Context:** `server.py:884-890`; the `scheduled_jobs` table from the eng
review; the heartbeat staleness indicator.
**Effort:** S → S · **Priority:** P3
**Depends on:** The worker and `scheduled_jobs` landing first.
