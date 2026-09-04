# Operations runbook

## Preflight

Before starting the production-shaped app, run the local, network-free check:

```bash
python check_env.py
```

It verifies the application secret, Supabase connection settings, an LLM
provider, OAuth credentials, and worker monitoring configuration. It does not
test the credentials against a remote service.

## Database migrations

The canonical schema lives in `supabase/migrations/`, with the pgTAP contract
in `supabase/tests/schema_contract.sql`. For a clean local project:

```bash
supabase start
supabase db reset --local --no-seed
supabase test db --local
supabase db lint --local --level error --fail-on error
```

Never run `db reset` against a linked hosted project. Production migrations
must run through the protected **Deploy database migrations** workflow from
`main`: review its `supabase db push --dry-run` output, set the explicit
production confirmation, and complete the protected environment approval. The
workflow also refuses to run unless the exact `main` commit has a successful CI
run, and pins the same Supabase CLI version used by CI. Apply the baseline in a
staging project first, then start the web process and confirm the boot log has
no `SCHEMA` warnings. There are no automatic rollback scripts; take a database
snapshot before production and keep the previous application version available
until verification passes. Future schema changes are new forward-only,
timestamped migrations; do not edit the deployed baseline.

## Web and worker processes

The web process and reply worker are independent. A worker outage stops reply
detection, even when the website is healthy.

```bash
python -m uvicorn server:app --host 127.0.0.1 --port 8000
python outreach-agent/watch_replies.py --once
python outreach-agent/watch_replies.py
```

For Linux deployment, use the unit templates in
`outreach-agent/deploy/`. On Windows, run the same commands under a process
manager or scheduled task and keep the worker as a separate process.

## Worker health

Set `WORKER_HEALTH_TOKEN` and enable worker telemetry. Then configure an
external monitor to send the token as `X-Worker-Health-Token`:

```bash
curl -H "X-Worker-Health-Token: <token>" \
  http://127.0.0.1:8000/internal/health/worker
```

Expected responses:

- `200` with `ok: true`: worker is live and healthy.
- `503` with `status: not_configured`: monitoring is not configured.
- `503` with a degraded snapshot: worker is reachable but needs attention.
- `401`: the monitor token is missing or incorrect.

Customer action alerts do not use the account's Gmail token, because that
token may be the reason an alert is being sent. Apply migration
`20260830000000_notification_outbox.sql` before deploying the matching web and
worker code, then configure
`TRANSACTIONAL_EMAIL_API_KEY`, `TRANSACTIONAL_EMAIL_FROM`, and optionally
`TRANSACTIONAL_EMAIL_API_URL` for a Resend-compatible provider. Without these
values the worker still records product state and heartbeat, but leaves alerts
durably queued for retry.

The pilot sends only to the immutable signed-in account email. Reply alerts are
delayed for two hours and cancelled if the queue item is resolved first. Gmail
disconnect alerts are delayed for 30 minutes and cancelled on recovery. Due
promise and uncertain-send alerts are immediate. Provider requests use the
outbox event UUID as an idempotency key; expired claims are retried only inside
the provider's 24-hour idempotency window.

During concierge onboarding, open Settings and use **Send test alert**. A
successful test proves the provider key, sender domain, account destination,
outbox claim, and finalize path. The email intentionally contains no contact
name, address, message body, promise text, or raw error.

## Per-inbox billing

The product unit is one isolated Sendkeep account connected to one managed
Gmail inbox. Inbox is `$29/month` (or `$290/year`) per inbox. Agency pilot is
`$49/month` (or `$490/year`) per managed Gmail inbox with concierge onboarding.
These are validation offers, not volume-sending plans. Checkout always sends
Stripe quantity `1`, and agencies use one isolated account per client inbox
until shared multi-inbox controls are validated.

To enable self-serve checkout, set all six Stripe values in
`outreach-agent/.env.example`'s corresponding deployment environment:

- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `STRIPE_PRICE_PILOT` and `STRIPE_PRICE_PILOT_YEARLY` for the Inbox tier
- `STRIPE_PRICE_TEAM` and `STRIPE_PRICE_TEAM_YEARLY` for the Agency pilot tier

Register the public endpoint `/api/billing/webhook` in Stripe and subscribe it
to `checkout.session.completed`, `customer.subscription.deleted`, and
`customer.subscription.paused`. The webhook is signature-verified and grants a
plan only from the authenticated Checkout session's account reference; never
make it trust an email or account id supplied by a browser.

After setting the values:

1. Restart the web process and open Settings on a Trial account.
2. Confirm the Inbox checkout button appears and no self-serve Agency pilot checkout is advertised.
3. Complete a Stripe test-mode checkout and verify the account plan changes
   only after the signed `checkout.session.completed` webhook arrives.
4. Cancel the test subscription and verify the account returns to Trial.
5. Confirm a second client inbox uses a separate Sendkeep account and Stripe
   subscription, not a quantity selected by the browser.

If Stripe is not configured, Settings intentionally shows concierge
provisioning instead of a broken or misleading checkout action.

## Release verification

Run the full verification from the repository root before restarting either
process:

```bash
python check_env.py
python tests/test_devex_tools.py
python tests/test_outreach_agent.py
python tests/test_build_freshness.py
cd outreach-agent && python -m unittest discover tests
cd ../app && npm ci && npm run lint && npm run build
cd ../site && npm ci && npm run lint && npm run build
cd .. && python check_build_freshness.py
```

After deployment, run one worker cycle, check the health endpoint, load the
landing page, and confirm that a protected page redirects to `/login` when no
session is present.
