# Gmail-first pilot validation

This runbook validates Sendkeep's actual wedge: a Gmail promise and follow-up
memory layer for people already sending 15-50 emails a day. The goal is not to
prove that Sendkeep can send more cold email or replace Gmail/Gemini replies.
The goal is to prove that a sender or agency keeps promises visible and makes
the one disciplined follow-up happen.

## Before recruiting a pilot

- [ ] Use a stable HTTPS hostname. Do not recruit from a temporary Daytona
  preview URL; Google OAuth redirect URIs must remain stable.
- [ ] Register both Google callbacks for that hostname:
  `https://<host>/auth/google/callback` and
  `https://<host>/api/google/callback`.
- [ ] Set `GOOGLE_OAUTH_REDIRECT_URI`, `GOOGLE_LOGIN_REDIRECT_URI`, and
  `SECURE_COOKIES=1` for HTTPS.
- [ ] Add each pilot's Google account as a test user while the OAuth consent
  screen remains in Testing mode.
- [ ] Run the full suite: `python -m pytest -q`.
- [ ] Confirm the worker is a separate continuously running process. Verify
  `/internal/health/worker` with `X-Worker-Health-Token` before sharing the URL.
- [ ] Confirm the worker has completed a successful cycle with zero failed
  accounts and no schema warnings in the boot log.
- [ ] Keep automatic first-touch sending disabled. A pilot should experience
  Gmail-native Sent monitoring and promise tracking first; the optional draft
  lane is secondary.

## Pilot profile

Recruit people who already send 15-50 messages a day from Gmail and feel the
cost of missed replies or forgotten follow-ups. Prioritize growth agencies and
solo operators who can start with one client inbox. Do not recruit a generic
high-volume sender looking for warmup, rotation, or a sending console.

Each pilot starts with one isolated Sendkeep account and one connected Gmail
inbox. Agency expansion is earned by repeating the workflow across client
inboxes; do not promise a shared multi-inbox console before that workflow is
validated.

## First-session flow

- [ ] Connect Gmail. A Google Sheet is optional and not needed for the promise
  layer.
- [ ] Let the worker discover the bounded window of recent Gmail Sent threads.
- [ ] Open Promises and identify one real action with evidence attached.
- [ ] Confirm or dismiss one promise. Reply in the original Gmail thread, not
  in a second Sendkeep inbox.
- [ ] If a follow-up becomes due, send the one reminder or dismiss it, then
  verify the audit result. Never use seeded activity as demand evidence.
- [ ] Add the pilot's meeting link and show the thread-to-deal view. Meeting and
  deal outcomes are recorded only after the operator confirms them.

## Evidence to capture

Use the queue's proof summary after the session. It is aggregate account
evidence and must not include names, email addresses, thread IDs, or raw reply
text.

- Promise candidates detected, confirmed, due, or dismissed.
- Follow-ups scheduled, sent, cancelled, or reconciled.
- Replies that arrived after a follow-up.
- Confirmed meetings and confirmed deals, entered by the operator.
- Qualitative labor saved: what the pilot would otherwise have searched for or
  remembered manually.

Label every result as one of `organic`, `seeded`, or `operator-confirmed`.
Seeded replies demonstrate the loop but never count as demand proof. Do not
invent reply-rate lifts, meeting totals, testimonials, logos, or case studies.

## Interview questions

- What promised action or follow-up would you normally have missed this week?
- How much time did the memory layer save compared with searching Gmail
  manually?
- Was one reminder per contact enough, or did you want a sequence?
- What would make you connect a second client inbox?
- Would $29 for one Inbox plan be cheaper than the labor or missed revenue this replaces?
- Would $49/month per managed Gmail inbox feel credible for the Agency pilot with concierge onboarding?

## Success criteria

A pilot is promising when a non-founder connects Gmail, reviews a real
conversation, and describes the missed-reply or follow-up problem without being
prompted. Stronger evidence is an organic reply, a confirmed meeting, or a
request to repeat the workflow on another client inbox.

Do not expand into multi-inbox infrastructure, warmup, rotation, sequences,
reply drafting, or lead sourcing because one pilot asks for it. First prove
that the memory layer earns a second inbox and a paid continuation.

## Safety rules

- [ ] The core Gmail Sent workflow remains read-only. Manual review remains the
  default for optional first-touch and follow-up sends.
- [ ] Never send to a pilot's real prospects without their explicit review and
  approval of each message.
- [ ] Never click a meeting or deal outcome on the pilot's behalf.
- [ ] If Gmail disconnects or the worker becomes stale, stop the session and
  show the reconnect or health state instead of claiming coverage.
- [ ] If a message is duplicated, misrouted, or unaccounted for, stop sending,
  preserve the audit trail, and investigate before continuing.
