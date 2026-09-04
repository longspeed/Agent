# Sendkeep Full Interactive QA Report

**Date:** 2026-09-03  
**Scope:** Credential-free demo, public production-shaped routes, browser composer regression suite, responsive layouts, navigation, protected routes, API documentation access, downloads, and network failures.  
**Safety:** No OAuth credentials entered. No Gmail or real-recipient send was attempted.

## Executive summary

| Area | Result |
|---|---:|
| Local demo functional checks | **12/12 passed** |
| Composer Puppeteer suite | **Passed** |
| Production-shaped public routes | **13/13 loaded with expected route behavior** |
| Production-shaped responsive checks | **12/12 passed without horizontal overflow** |
| Demo responsive checks | **3/3 passed** |
| Demo mobile review/reset interaction | **Passed** |
| Unexpected JavaScript/page errors | **None observed** |

**Overall:** The credential-free workflow is usable and send-safe. One medium-severity responsive defect and one low-severity asset defect were verified.

## Verified findings

### ISSUE-001 — Mobile landing header does not switch to mobile navigation

- **Severity:** Medium
- **Category:** Functional / Responsive UX
- **Viewport:** 390 × 844
- **URL:** `http://127.0.0.1:8000/`

At 390px, the landing page renders the full desktop navigation. The `Open menu` button exists but has `display: none` and a zero-sized bounding box, so it cannot be clicked. Desktop links remain visible in the cramped header instead of being replaced by a usable mobile menu.

**Expected:** The hamburger/menu control is visible at mobile widths and opens the navigation links.  
**Actual:** The hamburger is hidden and desktop navigation remains rendered.

**Evidence:** `screenshots/landing-mobile-390.png`

### ISSUE-002 — Missing favicon request

- **Severity:** Low
- **Category:** Asset / Content
- **URLs:**
  - `http://127.0.0.1:8000/favicon.ico`
  - `http://127.0.0.1:8010/favicon.ico`

Both the production-shaped server and safe demo return `404` for `/favicon.ico`. Existing page-specific favicon assets load, but the browser still requests the root favicon.

**Expected:** Serve `/favicon.ico` or avoid the root request.  
**Actual:** Root favicon returns `404`.

## Interaction results

Passed:

- Landing-page `How it works` anchor
- Landing-page `Pricing` anchor
- Getting-started `Sign in` link
- 404 page `Go home` recovery
- API route protection behavior (`/api/docs` and `/api/openapi.json` return `401` without authentication)
- Demo review action
- Demo reset action
- Intentional protected demo send rejection (`409`)
- CSV and XLSX template downloads
- Login/signup behavior in the safe demo

The API documentation 401 is treated as expected security behavior for the production-shaped server, not a defect. The safe demo exposes its docs separately.

## Composer and send-safety coverage

`node tests/browser/outreach-composer.spec.mjs` passed. The suite exercised:

- Non-actionable threads without a Draft reply button
- Send blocked while rewrite is pending
- Rewrite blocked while sending
- Double-click Send produces one request
- Closing during rewrite stops polling
- Composer cannot close while sending
- Late rewrite cannot overwrite a reopened composer
- Successful send refreshes queues and hides Send
- Retryable failure preserves edits and re-enables Send
- `send_uncertain` permanently hides Send, locks the editor, and exposes Gmail guidance
- Polling stops after cancellation and timeout
- Timeout displays the 90-second retryable message

The suite used intercepted API responses and a local static server; it did not contact Gmail.

## Route and responsive coverage

Production-shaped server on port 8000:

- `/`, `/login`, `/privacy`, `/terms`, `/security`, `/acceptable-use`, `/dpa`
- `/signup` reached the Google OAuth login page; no credentials were entered
- `/outreach`, `/leads`, and `/settings` redirected unauthenticated users to login
- `/getting-started`
- Unknown route returned the custom 404 page
- Desktop, tablet (768px), and mobile (390px) checks showed no horizontal overflow

Safe demo server on port 8010:

- `/health`
- `/demo`
- `/api/demo/state`
- `/api/demo/queue/{item_id}/review`
- `/api/demo/reset`
- `/api/demo/send`
- `/api/plan`
- `/api/lead-sheet-template.csv`
- `/api/lead-sheet-template.xlsx`
- `/login` and `/signup` safe redirects
- 390px, 768px, and 1440px layout checks

## Not tested

- Real Google OAuth completion
- Authenticated Settings, Leads, and Outreach pages against Supabase
- Gmail read/send/reconciliation
- Supabase migrations, RLS, and production data
- LLM providers
- Background worker
- Cloudflare tunnel and deployment workflow

## Recommended next actions

1. Fix the landing-page mobile breakpoint/menu behavior and add a real click test for it.
2. Add `/favicon.ico` or update the document head to reference the existing favicon consistently.
3. Keep the browser composer suite in required CI, including its local fixture and cancellation tests.

## Evidence

- `screenshots/landing-mobile-390.png`
- `tests/browser/outreach-composer.spec.mjs`
