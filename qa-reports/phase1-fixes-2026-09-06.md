# Phase 1 QA fixes — September 6, 2026

Status: DONE_WITH_CONCERNS. Code fixes implemented; Stripe configuration and live external-data release gates remain outstanding.

## Changes

- Account reads retry once on HTTP transport failure. Exhaustion returns a privacy-safe `503 account_store_unavailable`, Retry-After, and no-store headers. Only the account SELECT is retried, never an email send or mutation. Unknown account status no longer says Gmail attention; its explanation tells users not to reconnect Gmail unnecessarily.
- Scheduled promise cards now offer dismissal through the existing account-scoped endpoint. Failure leaves the item in place with a retryable message; success refreshes the ledger and shows a receipt. No SQL migration was needed for these fixes.
- Audit history expands to show available status transitions and timestamps, including their timezone. Imported history is shown as Recorded, not claimed to be a newly observed confirmation.
- Heading badge says how many items need review. Failed ledger reads show Unavailable instead of false empty/zero totals.
- Local date inputs preserve the detected instant when confirming in a non-UTC browser timezone.
- Public mockup uses fictional example.com contacts. Public hero and final CTA use the promise-ledger positioning.
- Guide no longer describes blank Sheet status as automatic send authorization, and directs core replies to Gmail with promise confirmation/completion in Sendkeep.
- New backend regression tests and an isolated browser ledger suite. CI runs the ledger and responsive tests with the existing composer tests.

## Verification

- Python: 576 tests passed.
- Existing composer and responsive browser suites passed.
- New browser ledger suite: scheduled dismissal failure/retry, audit expansion, detected confirmation, simulated worker due transition, explicit completion, account/ledger outage and recovery, and 375/768px overflow checks passed. All API responses in this suite are intercepted; no prospect data or external mutation is involved.
- App lint/build passed. Site lint/build passed with two existing Fast Refresh warnings in primitives.tsx.
- Build freshness and git diff whitespace checks passed.
- Real connected Codex browser: the existing scheduled promise shows Dismiss promise, `0 need review`, and expandable audit history with its recorded timestamp. No real promise was changed.
- Anonymous browser: rebuilt hero and fictional mockup are served correctly.

## Still blocked / not claimed

All six Stripe configuration values are absent: STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_PRICE_PILOT, STRIPE_PRICE_TEAM, and their _YEARLY price variants. No fake checkout, prices, credentials, or payment transaction were created. Configure the correct Stripe account and matching $29/$49 per-inbox prices, register the existing webhook, then verify a test-mode checkout and webhook before accepting live payments. Do not paste secrets into chat.

The intermittent Windows transport fault was not deterministically reproduced against live Supabase. Injected transport-failure tests verify recovery and exhaustion handling; the network-enabled local app subsequently loaded the real ledger. This is not proof that the underlying network will never fail.

Live promise lifecycle, actual alert delivery, worker outage/recovery, and checkout remain isolated-inbox release gates. Existing real promises, contacts, Gmail messages, and settings were not intentionally modified. Non-blocking mobile table/card redesign suggestions are not included in these focused fixes.

## Durable implementation notes

Never retry an entire send request to recover a database read fault. Keep retry boundaries around known idempotent reads. Browser tests restoring scroll position must scroll controls clear of the sticky header before clicking; otherwise a click can land on the status badge instead of the completion control.

The local server runs at http://localhost:8000. Existing unrelated worktree changes were preserved; no commit or production deployment was performed.
