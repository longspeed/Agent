# Sendkeep authenticated app pages

This React/Vite project owns the authenticated Settings page and the public
Getting Started guide. It builds compiled pages into `../static/app/`.

## Development

From this directory:

```bash
npm ci
npm run dev
```

For a safe end-to-end local surface, run the repository demo server instead:

```bash
cd ..
python dev_server.py
```

The demo does not require Google or Supabase and never sends email. Its queue is
at `/demo`; the generated API contract is at `/api/docs`.

## Verification and build

```bash
npm run lint
npm run build
```

The build writes `static/app/settings.html`,
`static/app/getting-started.html`, shared assets, and a source freshness stamp.
Run `python check_build_freshness.py` from the repository root after building
both frontend projects.

The Getting Started content is reviewed in parallel with
`../docs/getting-started.md`. Update both when onboarding behavior changes.
Do not edit generated files by hand.
