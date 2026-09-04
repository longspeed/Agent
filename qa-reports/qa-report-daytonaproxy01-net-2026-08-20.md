# QA Report: Sendkeep

| Field | Value |
|---|---|
| Date | 2026-08-20 |
| URL | https://8000-smi36mynvpzo2i7r.daytonaproxy01.net/ |
| Tier | Exhaustive live QA |
| Scope | Authenticated app, public bundle, legal pages, worker health, safe interactions |
| Framework | FastAPI with React pages and static HTML pages |
| Status | Complete. No critical, high, or medium issues remain. |

## Health score: 99/100

| Category | Score |
|---|---:|
| Console | 95 |
| Links and routes | 100 |
| Visual | 100 |
| Functional | 100 |
| UX | 100 |
| Performance | 100 |
| Content | 100 |
| Accessibility | 100 |

The only deduction is one non-blocking production console warning from the legacy Tailwind CDN. It is already tracked in `TODOS.md` under “Legacy pages still load Tailwind from the CDN.”

## Routes tested

Authenticated routes all loaded without an application error:

- `/`
- `/getting-started`
- `/settings`
- `/outreach`
- `/leads`
- `/security`
- `/privacy`
- `/terms`
- `/acceptable-use`
- `/dpa`

`/login` rendered correctly. An authenticated visit to `/signup` resolved to `/outreach`, which is the expected signed-in behavior. The active anonymous landing bundle was also checked for the Gmail-first CTA, the absence of `Start free` and `LinkedIn`, and the exit-copy card.

## Functional flows verified

- Queue help dialog opens and closes with `Got it`.
- `Copy proof summary` reports `Copied` and produces no console errors.
- `Check for replies now` completes without an error.
- A monitored-thread `Check` action completes without an error.
- Settings safety panel renders the SPF/DKIM/DMARC state.
- Settings export starts and returns to the normal `Download my data` state.
- Settings save returns `PUT /api/settings 200 OK` and now visibly shows `Saved.`.
- Top navigation, browser back, and browser forward all work.
- Empty lead-sourcing state renders correctly. No metered lead search was launched.
- Mobile queue layout at 375x812 has no horizontal overflow and keeps the approval contract readable.
- Public security and legal pages contain no visible placeholders or retired CTA copy.
- Worker remains healthy and the web process remains running.

## QA-008: Settings save had no visible success feedback

| Field | Value |
|---|---|
| Severity | Low |
| Category | UX |
| Fix status | Verified |

The API write was successful, but the UI could leave the operator unsure whether the save completed while the post-save DNS/settings refresh was running. The success state now appears immediately, and a refresh failure is reported separately instead of hiding a successful write.

Changed:

- `app/src/SettingsPage.tsx`
- `tests/test_qa_settings_save_feedback.py`

Evidence: live Settings interaction showed `Saved.` after a 200 response. The regression test passes.

## Deferred QA-009: Tailwind CDN console warning

| Field | Value |
|---|---|
| Severity | Low |
| Category | Performance / console |
| Fix status | Deferred, already tracked |

The browser reports the standard `cdn.tailwindcss.com should not be used in production` warning on legacy pages. No application errors were observed. Moving the remaining legacy pages to the compiled React/CSS path is already tracked in `TODOS.md`.

## Runtime health

- `sendkeep-web`: RUNNING
- `sendkeep-worker`: RUNNING
- Worker telemetry: `ok: true`, `run_status: success`, 2 accounts checked, 0 failures
- Schema warnings after restart: none
- Browser console errors: 0
- Browser console warnings: 1 known Tailwind CDN warning

## Previous regression report comparison

The previous live baseline was 72/100 with seven issues. This run reverified the affected areas:

- Settings save now returns 200 and visibly confirms success.
- Follow-up status renders a real value, not `-`.
- Security copy matches the manual-review and controlled auto-send contract.
- Protected routes redirect to `/login` when unauthenticated.
- Legal pages no longer expose the previous unfinished-document warnings.
- Public bundle no longer contains the footer placeholder, `Start free`, or LinkedIn copy.

## Exclusions

No email was sent, no Gmail connection was disconnected, no lead was discarded, no lead search was launched, and no account data was deleted. Those actions require an explicit test recipient or would mutate customer data.

## Ship summary

QA found 2 low-severity issues, fixed 1, deferred 1 known maintenance item, and raised the live health score from 72 to 99.
