# Sendkeep marketing site

This React/Vite project builds the logged-out Sendkeep landing page into
`../static/landing/`. The backend serves that compiled output at `/` for
anonymous visitors.

## Development

From this directory:

```bash
npm ci
npm run dev
```

Use the local demo server from the repository root when you need to see the
landing page beside a safe seeded queue:

```bash
cd ..
python dev_server.py
```

## Verification and build

```bash
npm run lint
npm run build
```

The build writes `static/landing/index.html`, the bundle under
`static/landing/assets/`, and a source freshness stamp. Run
`python check_build_freshness.py` from the repository root after building both
frontend projects.

Do not open the compiled HTML with `file://`. Its `/static/...` asset paths need
an HTTP server. Do not edit generated files by hand; change `site/src/` and
rebuild.

Before an agency pilot, provide a real booking destination at build time:

```bash
$env:VITE_PUBLIC_BOOKING_URL = 'https://cal.com/your-team/sendkeep'
npm run build
```

When the variable is absent, the public site fails closed to `Connect Gmail` or
`See your queue`; it never renders a placeholder or a mailto booking CTA.
