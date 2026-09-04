# Development guide

This repository has two runtime paths. Start with the local demo unless you
are specifically changing OAuth, Supabase, Gmail, billing, or deployment.

## Local demo

From the repository root:

```bash
python -m pip install -r outreach-agent/requirements.txt
python dev_server.py
```

Open:

- `http://127.0.0.1:8000/demo` — seeded review queue
- `http://127.0.0.1:8000/api/docs` — local OpenAPI UI
- `http://127.0.0.1:8000/health` — local liveness check

The demo uses fake account data and `.example` addresses. It never imports the
production server or its credential-backed database modules. `POST
/api/demo/send` always returns `409`; this is an intentional safety boundary,
not a missing feature. Use **Reset demo data** or restart the process to restore
the seed queue.

Useful smoke checks:

```bash
python dev_server.py --help
python dev_server.py --version
```

## Production-shaped app

The real app requires Google OAuth, Supabase, and at least one configured LLM
provider. Follow [`outreach-agent/README.md`](../outreach-agent/README.md) for
the complete setup and migrations. Do not point the local demo at production
credentials.

Run the web app from the repository root after configuring the environment:

```bash
python -m uvicorn server:app --port 8000
```

The background reply worker is independent from the web process:

```bash
python outreach-agent/watch_replies.py --once
python outreach-agent/watch_replies.py
```

Prepare outreach drafts for one account with:

```bash
python outreach-agent/send_outreach.py you@company.com
```

Both CLIs are safe to inspect before configuration:

```bash
python outreach-agent/watch_replies.py --help
python outreach-agent/send_outreach.py --help
```

## Verification

```bash
python tests/test_outreach_agent.py
python tests/test_build_freshness.py
cd outreach-agent && python -m unittest discover tests
cd ../app && npm run lint && npm run build
cd ../site && npm run lint && npm run build
cd .. && python check_build_freshness.py
```

The frontend builds write compiled bundles into `static/`. Serve the repository
over HTTP when previewing those bundles. Opening a built HTML file with
`file://` makes root-relative `/static/...` assets resolve to the wrong place
and can produce a blank page.
