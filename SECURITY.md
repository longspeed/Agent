# Security policy

Sendkeep handles Gmail access tokens, lead data, reply content, and outbound
email. Treat changes to OAuth, sessions, tenant scoping, worker health, Gmail,
Supabase, LLM providers, and send guards as security-sensitive.

## Reporting a vulnerability

Do not open a public issue with credentials, tokens, customer data, or an
exploitable proof of concept. Email `support@sendkeep.app` with:

- a concise description and affected route or file;
- reproduction steps using synthetic data;
- the impact and whether a real send, token, or tenant boundary is involved;
- any suggested mitigation.

The local demo is safe for reproductions because it uses fake data and rejects
all send attempts with HTTP 409.

## Development rules

- Never commit `.env`, OAuth credentials, service-role keys, or real exports.
- Keep production API docs disabled unless intentionally enabled for a protected
  development environment.
- Preserve the send log, suppression checks, opt-out behavior, and tenant scope
  when changing send or reply code.
- Add a regression test for every security-sensitive failure mode.
