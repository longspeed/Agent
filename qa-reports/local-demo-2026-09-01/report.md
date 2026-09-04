# Sendkeep Local Demo QA Report

**Target:** `http://127.0.0.1:8000/demo`
**Date:** 2026-09-01
**Scope:** Credential-free local demo: health, seeded queue, review action, protected send guard, reset, invalid item handling, plan endpoint, template downloads, redirects, console errors, and mobile layout.
**Tester:** Hermes Agent (automated exploratory QA with isolated headless Chromium)

---

## Executive Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 1 |
| **Total** | **1** |

**Overall Assessment:** The local demo passed all 12 functional checks. The only finding is a missing favicon request returning 404; the intentional protected-send 409 behaved correctly.

### Results

- **12/12 automated checks passed**
- Health endpoint returned `200` and `safe_to_send: false`
- Seeded queue rendered 3 items
- Review action changed the queue from 3 to 2 pending items
- Protected send returned the intentional `409` and did not send email
- Reset restored the seeded state
- Invalid queue item returned `404`
- CSV and XLSX template downloads returned non-empty attachments
- Mobile viewport had no horizontal overflow (`390px` scroll width / `390px` client width)
- `/login` and `/signup` safely redirected to `/demo`
- No unexpected JavaScript/page errors observed

---

## Issues

### Issue #1: Demo page requests a missing favicon

| Field | Value |
|-------|-------|
| **Severity** | Low |
| **Category** | Visual / Content |
| **URL** | `http://127.0.0.1:8000/demo` |

**Description:**
The demo page triggers a request for `/favicon.ico`, which returns HTTP `404 Not Found`. This can produce a missing favicon in the browser tab and a noisy console entry.

**Steps to Reproduce:**
1. Start the local demo with `python dev_server.py`.
2. Open `http://127.0.0.1:8000/demo`.
3. Inspect network requests or browser console output.

**Expected Behavior:**
The page should either provide a favicon at `/favicon.ico` or explicitly reference an existing favicon asset.

**Actual Behavior:**
`GET http://127.0.0.1:8000/favicon.ico` returns `404`.

**Evidence:**
The isolated browser captured the failing request as:

```text
GET http://127.0.0.1:8000/favicon.ico → 404
```

**Screenshot:**
`qa-reports/local-demo-2026-09-01/screenshots/01-initial-desktop.png`

**Console Errors:**
The other console status was the expected protected-send response:

```text
POST /api/demo/send → 409 Conflict
```

---

## Issues Summary Table

| # | Title | Severity | Category | URL |
|---|-------|----------|----------|-----|
| 1 | Demo page requests a missing favicon | Low | Visual / Content | `http://127.0.0.1:8000/demo` |

## Testing Coverage

### Pages and endpoints tested

- `/demo`
- `/health`
- `/api/demo/state`
- `/api/demo/queue/{item_id}/review`
- `/api/demo/reset`
- `/api/demo/send`
- `/api/plan`
- `/api/lead-sheet-template.csv`
- `/api/lead-sheet-template.xlsx`
- `/login`
- `/signup`

### Features tested

- Initial seeded queue rendering
- Summary metric accuracy
- Marking a queue item reviewed
- Protected-send safety boundary
- Resetting demo state
- Invalid queue item handling
- Local plan limits
- Safe template downloads
- Mobile responsive layout
- Credential-free auth redirects

### Not Tested / Out of Scope

- Real Google OAuth
- Gmail read/send operations
- Supabase persistence and RLS against a live database
- LLM provider behavior
- Authenticated production pages
- Background reply worker
- Production deployment and Cloudflare tunnel

### Blockers

None for the local demo test. Chrome remote-debugging automation was unavailable, so testing used an isolated headless Chromium run instead.

## Evidence Files

- `qa-reports/local-demo-2026-09-01/results.json`
- `qa-reports/local-demo-2026-09-01/screenshots/01-initial-desktop.png`
- `qa-reports/local-demo-2026-09-01/screenshots/02-after-review.png`
- `qa-reports/local-demo-2026-09-01/screenshots/03-protected-send.png`
- `qa-reports/local-demo-2026-09-01/screenshots/04-mobile.png`
