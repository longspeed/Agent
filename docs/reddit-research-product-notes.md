# Reddit Research Product Notes

Source: Codex session `019ff8f9-3422-74c0-bb69-edadbb9e37f9`, August 2026.
Updated after the RESHAPE verdict: lead with the reply loop, not deliverability.

## What people are actually asking for

The strongest Reddit pain was not "write me prettier emails." The repeated pain
was uncertainty:

- Are my emails landing or going to spam?
- Is my list bad?
- Are bounces damaging my domain?
- Am I following up correctly?
- Which tool should I trust for a small Gmail-based workflow?

That means Sendkeep should not lead as "GMass plus AI copy" or "we protect your
Gmail." It should lead as a reply and follow-up desk for small Gmail outbound.
Deliverability controls are guardrails, not the product promise.

## Build/positioning priorities

1. Make reply/follow-up operations the product.
   The loop is the wedge: sent thread, detected reply, drafted response, visible
   review queue, follow-up state. Sends are episodic; replies are the retention
   surface.

2. Make the first-week win a verified send plus a handled reply.
   A free user should not hit "50 blocked unverified leads" as the first
   experience. Either make manual confirmation a clear choice or test a cheap
   verifier API so the first batch can move.

3. Keep deliverability language honest.
   Caps, opt-outs, verified rows, and bounce pauses reduce obvious damage. They
   do not replace separate sending domains, warmup, multi-inbox rotation, or
   inbox placement testing. Never imply they do.

4. Price Pilot against GMass, not against agency platforms.
   The current realistic anchor is $15-25/mo for one Gmail inbox. $79+ only
   makes sense after the reply loop proves time saved or after multi-account
   agency features exist.

5. Do not build multi-inbox infrastructure before validation.
   High-volume agency users want it, but building it now changes the product
   category. Validate the single-inbox reply desk first.

## Good next product experiments

- Concierge the reply loop for 3-5 Reddit prospects for two weeks and measure
  minutes saved per reply plus willingness to pay $20/mo.
- Add a follow-up queue: no reply yet, follow-up due, replied needs response,
  positive reply, objection, not interested, out of office.
- Add a "Why no replies?" diagnostic panel after the reply loop is clear. It
  should support the loop, not become the main product.
- Add a simple reply-rate stat once enough sends exist.
- Add per-batch outcomes: prepared, sent, bounced, replied, awaiting reply.

## Anti-priorities

- Do not make AI prose the main promise.
- Do not sell "protect your domain" as a promise.
- Do not automate Reddit comments or DMs.
- Do not sell high-volume agency sending until multi-inbox and domain setup are
  real.
- Do not make manual verification the first-week wall if a user brought a list
  they already trust.
