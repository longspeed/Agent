# Manual test — bug log

Session: 2026-08-04. Reported by OanhVK during manual walkthrough.
Status key: OPEN / NEEDS-REPRO / FIXED.

---

## BUG-1 — Settings: required and optional fields are indistinguishable on first run

**Status:** FIXED — `/api/me` now returns `sendBlockers` (from
`agent.account_send_blockers`), rendered as a banner on the Outreach settings
form; sender first name and meeting purpose are labeled `(required)`, company
`(optional)`; `handleSave` reloads settings after a save and reports the
blockers instead of a bare "Saved." if any remain. Regression test:
`test_me_surfaces_send_blockers` in `tests/test_outreach_agent.py`.
**Reported:** on first use, "Sender first name (signs your emails)" and
"Company name" are empty with nothing indicating what must be filled.
(Originally filed against login; the fields named are on Settings.)

**The actual defect — the two fields behave differently and look identical:**

| Field | Really required? | Marked in UI? |
| --- | --- | --- |
| Sender first name (`SettingsPage.tsx:376`) | **Yes** — `agent.account_send_blockers` refuses to generate outreach until it's set; unset it signs every email "the team" | No marker |
| Company name (`:387`) | **No** — `_sender_context` treats it as optional, `outreach_prompt` includes it only when present | No marker |
| What you're reaching out about (`:398`) | **Yes** — blocked while it equals `DEFAULT_MEETING_PURPOSE` | No marker; helper text hints ("rejected at send time") |
| Custom instructions (`:414`) | No | **Marked `(optional)`** |

Because `custom_instructions` is the only field carrying `(optional)`, the form
establishes that unmarked means required. That reads exactly backwards: the one
unmarked field a user would infer is required (company) isn't, and the two that
genuinely are carry nothing.

**Compounding it — a blank required field saves silently.** `handleSave`
(`:129-136`) sends `sender_name: form.sender_name?.trim() ?? ''` with no
client-side check, and the server accepts it. The user saves, sees a success
state, and only learns first name was mandatory several steps later when
preparing a campaign is refused.

**Fix, in order of value:**

1. **Surface the blockers where they're fixable.** `agent.account_send_blockers`
   already returns human-readable strings naming the field and the consequence
   ("outreach is signed 'the team' until you do, which reads as a bot"). Today
   they only appear on the campaign preview. Render the same list on Settings —
   no new copy to write, no new logic, and it puts the message next to the input
   that fixes it.
2. Label the fields: `(required)` on sender first name and meeting purpose,
   `(optional)` on company — matching the convention `custom_instructions`
   already set.
3. Warn (don't block) on save when a required field is blank, so "Saved" never
   means "saved in a state that can't send."

**Test to add:** saving Settings with an empty `sender_name` surfaces the
blocker text, rather than reporting an unqualified success.

---

## BUG-2 — Failed input validation burns a rate-limit attempt (found while tracing BUG-1)

**Status:** FIXED, now vestigial (2026-08-06) — `signup_submit` and
`login_submit` no longer exist; password auth was deleted outright rather
than hardened further (PLAN-WEEK-2026-08-05.md, M1). The fix and its
regression test (`test_signup_rejects_bad_format_without_consuming_ratelimit`)
were removed with it (M4) — there is no rate-limit-before-validation ordering
left to test. Left FIXED, not reopened: the code path this bug lived in is
gone, not broken again.

`server.py:303` and `:319` call `ratelimit.check(...)` **before** any input
validation. `ratelimit.check` *records* the attempt when it allows it (see
`outreach-agent/ratelimit.py` — "Records a call under `key` and returns True if
it's allowed"). So a request that never had a chance of succeeding still spends
a slot.

**Signup is the worse one.** `limit=5, window_seconds=3600`:

```
server.py:303   ratelimit.check(f"signup:{ip}", limit=5, window_seconds=3600)
server.py:306   if "@" not in email ... raise 400 "Enter a valid email address"
server.py:308   if len(password) < 8 ... raise 400 "Password must be at least 8 characters"
```

A new user who mistypes their email twice and picks a short password three
times is locked out of signing up **for a full hour**, having never created an
account. The rate limit exists to stop automated abuse; a 400 on password
length is not abuse, it is a person learning the form.

**Login** has the same shape at `limit=10, window_seconds=300` — ten empty or
malformed submits and a real user is locked out for five minutes without ever
having guessed a password.

**Fix:** validate first, rate-limit second — only count attempts that reached
the credential check. Concretely, move `ratelimit.check` below the format
validation in `signup_submit`, and below an empty-field guard in
`login_submit`.

**Test to add:** a 400-producing signup does not consume a rate-limit slot —
six bad-format attempts followed by one valid one should succeed, not 429.

---

## BUG-3 — Empty email reports "Wrong email or password" (minor, same trace)

**Status:** FIXED, now vestigial (2026-08-06) — `login_submit` no longer
exists; password auth was deleted outright rather than hardened further
(PLAN-WEEK-2026-08-05.md, M1). There is no "Wrong email or password" message
left to send, correctly or otherwise. The fix and its regression test
(`test_login_rejects_empty_credentials_before_ratelimit_and_lookup`) were
removed with it (M4). Left FIXED, not reopened.

`server.py:321-325`: an empty email produces no account, which falls through to
`401 "Wrong email or password"`. For a first-time visitor who submitted an
empty form, that message says they got a credential wrong when they supplied
none — pointing them at a password reset that doesn't exist rather than at
signup.

Only reachable if native `required` validation is bypassed (programmatic
submit, or a client that ignores it), so it is a robustness fix rather than a
live path. Server-side guard: reject empty email/password with a 400 that names
the missing field before the lookup.
