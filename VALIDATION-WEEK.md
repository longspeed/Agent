# Validation Week Checklist

Operational runbook for the demand-validation Assignment (design doc
2026-07-16: get 1-3 real people to sign up and send 5+ real outreach emails,
watched, this week). Product decisions live in TODOS.md; this file is the
"don't corrupt the experiment" checklist from the 2026-07-19 CEO review.

## Before recruiting anyone (one-time)

- [ ] **Make the app reachable.** Server binds 127.0.0.1 only. Set up a
  **named** cloudflared tunnel (NOT a quick tunnel — quick-tunnel URLs rotate
  on every restart and Google OAuth redirect URIs must match exactly):
  `cloudflared tunnel create agent-hub`, route a stable hostname, run
  `cloudflared tunnel run` pointing at `http://127.0.0.1:8000`.
- [ ] Add the tunnel hostname to the Google OAuth client's authorized
  redirect URIs — **both** flows: `https://<host>/api/google/callback` and
  `https://<host>/auth/google/callback`.
- [ ] Set env for the tunnel: `GOOGLE_OAUTH_REDIRECT_URI` and
  `GOOGLE_LOGIN_REDIRECT_URI` to the https URLs, and `SECURE_COOKIES=1`.
- [ ] Set `OPENROUTER_MODEL` to the paid mid-tier model (decided in review —
  candidates' real cold emails should not be drafted by the free 20B model).
- [ ] **Dry run end-to-end on the tunnel URL** (design doc precondition):
  fresh signup → connect Google → pick sheet → approve a lead → send a real
  test email to yourself → reply to it → see the review card → send the
  reply. If any step breaks, fix before recruiting — a candidate hitting a
  broken flow corrupts the signal into a false "no demand."
- [ ] Run the test suite: `python tests/test_outreach_agent.py`.

## Per candidate, BEFORE sending them the link

- [ ] Add their Google account as a **test user** in Google Cloud Console
  (OAuth consent screen → Test users). Without this, Google hard-rejects
  their connect attempt — an instant false negative.
- [ ] Help them prep their sheet: exact header row
  `Name, Email, Company, Status, ThreadID, SentAt, EmailBody, LeadReason,
  EmailConfidence` + 5+ real leads. (The app now validates the header and
  fails with a clear message, but fixing it live wastes session time.)
- [ ] Warn them about two Google screens: the "This app hasn't been
  verified" warning (expected — Testing mode; Advanced → continue) and that
  their Google connection **expires after 7 days** (Testing-mode policy;
  reconnect is one click, not a bug).

## During the watched session

- [ ] Watch, don't demo. Their machine, their Google account, their real
  prospects. Don't help unless truly stuck; note every surprise.
- [ ] **Seeded reply step** (so the differentiator gets observed): their
  sheet includes one prospect address you control, disclosed to them. Reply
  to that outreach email live mid-session, so they experience the full loop:
  detect → drafted reply → review card → edit → send. A seeded reply
  demonstrates the loop; only an organic reply counts as demand signal.
- [ ] Tell them not to sort/insert rows in the sheet while a batch is
  sending (writes now verify-and-follow by email, but don't test fate live).
- [ ] Keep the laptop awake — tunnel + server + reply polling all run on it.

## Signal accounting (from the design doc)

- Success: a non-founder connects, sends real outreach, and either gets an
  organic reply or says something unprompted about the experience.
- If every candidate is a warm contact doing a favor, "zero conversions is
  real signal" no longer holds — recruit people with the actual pain.
- If nobody converts by end of week: that's an Approach-A miss — revisit
  distribution before touching deliverability (B) or repositioning (C).
