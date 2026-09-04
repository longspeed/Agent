# Contributing to Sendkeep

## Start safely

Use the credential-free demo before configuring production integrations:

```bash
python -m pip install -r outreach-agent/requirements.txt
python dev_server.py
```

Open `http://127.0.0.1:8000/demo`. The demo queue uses fake data and cannot send
email. Its local API documentation is at `/api/docs`.

## Change the application

- Backend and worker code lives in `server.py` and `outreach-agent/`.
- The authenticated React pages live in `app/` and build into `static/app/`.
- The logged-out marketing site lives in `site/` and builds into
  `static/landing/`.
- `docs/getting-started.md` is the reviewed source of truth for the in-app
  Getting Started page. Update it when changing onboarding behavior.
- Do not use real Gmail, Supabase, or customer data for local tests.

## Verify before opening a change

```bash
python check_env.py
python tests/test_devex_tools.py
python tests/test_outreach_agent.py
python tests/test_build_freshness.py
cd outreach-agent && python -m unittest discover tests
cd ../app && npm run lint && npm run build
cd ../site && npm run lint && npm run build
cd .. && python check_build_freshness.py
```

For worker changes, also run the worker once with an explicit account only in a
configured test environment:

```bash
python outreach-agent/watch_replies.py --help
python outreach-agent/watch_replies.py --once
```

## Pull request expectations

Describe the user-visible behavior, the failure mode being addressed, and the
verification commands you ran. Include migration steps for schema changes and
call out any change that can send email, access Google data, or alter tenant
boundaries.
