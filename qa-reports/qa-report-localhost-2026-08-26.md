# Outreach desk QA report

- Date: 2026-08-26
- Target: `http://127.0.0.1:8000/outreach?tab=inbox`
- Scope: focused Today, Promises, Follow-ups, Threads desk
- Browser: Chromium through Puppeteer, 1440×900 and 375×812
- Authentication: deterministic authenticated API fixtures; production server route and new static asset also probed live
- Health score: 82 → 96

## Outcome

The focused operator desk works on desktop and mobile. The browser run produced zero application or console errors. All tested action targets are at least 44px high, queue navigation works from the keyboard, the Verify lane prevents resending an unknown outcome, and mobile selection opens a full-width detail view with an explicit Back action.

Real-account Google OAuth was not repeated in this run. The send-capability reconnect return path is covered by source and build tests, while end-to-end OAuth still requires a real Google account.

## Issues found and fixed

### ISSUE-001 — Initial data load looked like a new arrival

- Severity: medium
- Category: UX
- Before: replies loaded before promises, so the completed promise request appeared as “1 arrived while you worked.”
- Fix: establish the session baseline only after both actionable queues finish their initial load.
- Verification: Chromium asserts `4 open · replies first` on first render.
- Status: verified

### ISSUE-002 — Returning to Today kept a lane-only filter

- Severity: high
- Category: functional
- Before: opening Follow-ups and returning to Today left the Due filter active, hiding replies and promises.
- Fix: tab routing no longer mutates the user’s Today filter; lane tabs apply an effective view without overwriting it.
- Verification: Chromium navigates Follow-ups → Today and asserts all four queue items are restored.
- Status: verified

### ISSUE-003 — Unknown send outcome could remain on an actionable card

- Severity: critical
- Category: safety
- Fix: uncertain normal replies and follow-ups render in a locked Verify lane; the UI reloads immediately after an ambiguous Gmail result and does not re-enable sending.
- Verification: Verify appears second in queue order and exposes no send action.
- Status: verified with mocked Gmail response; real Gmail timeout was not induced.

### ISSUE-004 — Due follow-up had no snooze operation

- Severity: medium
- Category: functional
- Fix: added a guarded server transition from `queued` to `waiting`, a one-day UI action, and stale-tab conflict handling.
- Verification: regression tests cover successful and raced transitions.
- Status: verified

## Evidence

- Desktop: `qa-reports/guided-review-desktop.png`
- Mobile: `qa-reports/guided-review-mobile.png`
- Browser assertions: `node qa-reports/visual-smoke-guided-review.js`
- Full suite: `python -m pytest -q` → 491 passed
- Frontend build: `npm run build` → passed
- Frontend lint: `npm run lint` → passed
- Live server probe: `/static/outreach-desk.js` → 200; logged-out `/outreach?tab=inbox` → expected 303 to login

## Remaining concern

The page still contains legacy HTML and JavaScript internally because removing it in this already-dirty worktree would be a high-risk rewrite. It is no longer reachable through navigation, and its unnecessary startup API calls were removed. A later cleanup can delete the dead implementation once this reshape has been stable in production.

PR summary: QA found four outreach-desk issues, fixed four, and raised the health score from 82 to 96.
