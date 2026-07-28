# Agent Hub — Go-To-Market & Monetization Plan

**Market:** US-first SaaS founders and small GTM teams
**Stage:** Product works, zero users
**Date:** July 2026

---

## 0. Read this first — five things that block launch

You cannot advertise until these are fixed. Two are legal exposure, one is a hard
platform gate with a 4–12 week lead time.

| # | Issue | Evidence | Severity |
|---|---|---|---|
| 1 | **The site claims email verification you no longer do.** The landing page says "Verified emails only", "Every address is confidence-scored before it's eligible to send", "Unverified leads never go out". | `outreach-agent/config.py:53-57` — "there is no verification gate: any approved row with an email can send. (verification via NeverBounce was removed 2026-07-18)" | **Critical.** False advertising, and it's the exact claim your trust page is built on. |
| 2 | **Google OAuth verification not done.** You request `gmail.modify` — a restricted scope. Until you pass CASA Tier 2 + Google review you are capped at 100 test users and show an "unverified app" warning at signup. | `config.py:59-65` | **Critical, long pole.** Budget $540–$1,000 and 4–12 weeks. Start today. |
| 3 | **No physical business address in the footer.** Placeholder text reads "[Your registered business address goes here]". | `site/src/components/sections.tsx:328` | **High.** CAN-SPAM requires it in every commercial email; your own footer is also non-compliant. |
| 4 | **Default LLM is a free model** (`openai/gpt-oss-20b:free`). | `config.py:22` | **High.** The email copy *is* the product. A weak model here caps every retention metric you'll try to fix later with marketing. |
| 5 | **No payment rail visible.** Pricing page CTAs all point to `/signup`. | `site/src/components/pricing.tsx:104` | **High.** You can't monetize what you can't charge for. |

**Fix 1 by choosing one:** re-add a verification provider (ZeroBounce/NeverBounce,
~$0.003–0.008/email) and keep the claim — it's your best differentiator — or
rewrite the copy to "you approve every address before it sends." Re-adding it is
the better business decision; verification is also a natural usage-based
revenue line.

---

## 1. Positioning: don't sell a cold email tool

The sending-infrastructure market is commoditized and racing to the bottom.
Instantly starts at $47/mo and Smartlead at $39/mo, both with **unlimited
mailboxes and unlimited warmup**. You cannot win a volume fight, and at $49/mo
for 500 leads and 25 sends/day you are priced *into* that fight while offering
1/10th the volume.

Reframe. Agent Hub's actual product is not sending — it's **the approval queue
plus reply handling**. That is a junior SDR's job description, not a mail-merge
feature.

**Positioning statement:**

> Agent Hub is an AI SDR that never sends without you. It finds your leads,
> writes every email and every reply, and puts them in a queue for your yes.
> One inbox, human volumes, nothing autonomous.

**The buyer this wins:** the founder who tried Instantly or Apollo, blew up a
domain or sent something embarrassing, and now refuses to hand an LLM the send
button. That fear is real, widespread, and currently unserved — every competitor
sells autonomy, which is precisely what this buyer is afraid of.

**Competitive frame:**

| | They sell | You sell |
|---|---|---|
| Instantly / Smartlead | Volume + deliverability infrastructure | Judgment + control |
| Apollo / Clay | A database and enrichment pipeline | A finished, reviewable draft |
| A human SDR ($4–6k/mo) | Full autonomy at high cost | 80% of the work at 3% of the cost |

Anchor against the SDR and the $2–3k/mo lead-gen agency retainer, **not** against
$39 Smartlead. That's what justifies a $149–$399 tier.

**Three ad angles, in priority order:**

1. **"No autopilot."** — *Every AI outreach tool sends for you. This one refuses.*
   Fear-of-embarrassment angle. Highest resonance with founders.
2. **"Stop writing cold emails. Start approving them."** — Your existing headline.
   Time-saving angle. Keep it.
3. **"Your domain, at human volumes."** — Deliverability angle. Speaks directly
   to anyone who's been burned. Needs the verification claim to be true (Issue #1).

**Do not** lead with "AI agent." That's noise in 2026 and it attracts the
autonomy-seeking buyer you're deliberately not serving.

---

## 2. Pricing: your current model leaves money on the table

Current: Free trial (50 leads, 25/day) → Pilot $49/mo (500 leads) → Team $149/mo
(3 inboxes).

**Problems.** Pilot at $49 with 500 leads/mo sits at Instantly's entry price with
a fraction of the capacity — a like-for-like comparison you lose. Lead volume is
your main cost driver (Tavily search + LLM tokens per lead) but is bundled flat,
so your worst-margin customers are your cheapest ones. And Team at $149 is only
3× Pilot for a buyer worth 10× more.

**Recommended structure:**

| Plan | Price | Includes | Positioned against |
|---|---|---|---|
| **Trial** | Free, 14 days | 50 leads, 25 sends/day, 1 inbox. No card. | — |
| **Solo** | **$79/mo** ($790/yr) | 750 leads/mo, 1 inbox, unlimited reply drafts | Instantly + a lead source, ~$100–150/mo combined |
| **Team** | **$249/mo** ($2,490/yr) | 3,000 leads/mo, 3 inboxes, shared queue, roles, audit export | A part-time SDR contractor |
| **Agency** | **$599/mo** | 10 inboxes, client workspaces, white-label sending | A $2–3k/mo lead-gen retainer |

**Expansion levers, in order of return:**

1. **Lead credit packs** — $29 per extra 500 leads. Usage-based, scales with your
   actual COGS, and turns your best customers into your best revenue. Add this
   before anything else.
2. **Done-for-you onboarding, $499 one-time** — you sit with them, define the ICP,
   set up domain/warmup, run the first campaign. Highest-margin revenue you can
   book in month one, and it's how you learn what to automate. Sell this to your
   first 20 customers even if the software is free.
3. **Per-inbox overage** — $39/inbox beyond plan. Clean, obvious, no negotiation.
4. **Annual prepay** — you already offer 2 months free. Push it hard; at
   zero users, cash now beats MRR later.
5. **Email verification as a metered add-on** — if you re-add it (Issue #1),
   charge $0.02/verification against a ~$0.005 cost.

**Unit economics you must fill in before spending on ads** (I can't compute these
without your provider rates):

```
Cost per lead sourced   = Tavily search cost + LLM tokens for qualification
Cost per email drafted  = LLM tokens (input context + output)
Cost per reply drafted  = LLM tokens (thread context is larger — measure it)
Monthly COGS at Solo    = 750 × cost/lead + ~750 × cost/email + reply volume
Gross margin at $79     = must exceed 80% or the plan is mispriced
```

Instrument this in `usage.py` before launch. A single Solo customer running
750 leads through a frontier model can plausibly cost more than $79/mo.

---

## 3. Channels: what to do with zero users and no budget

**Do not buy ads yet.** At $79 ARPU and a realistic 3–5% visitor→paid rate, your
CAC ceiling is roughly $150–250 for a 3-month payback. You cannot hit that on
cold traffic without a proven landing page. Paid comes at Day 90+, if at all.

### Tier 1 — do these first (free, high signal)

**1. Dogfood in public. This is your entire marketing strategy for 90 days.**

Use Agent Hub to acquire Agent Hub's first 100 customers, and publish the numbers
weekly: leads sourced, emails approved, replies, meetings booked, customers
closed. This solves four problems at once — it's your acquisition channel, your
case study, your QA process, and your content calendar. It is also the single
most credible proof an outreach tool can offer, and almost no competitor does it
transparently.

Post format, 3×/week on X and LinkedIn: a screenshot of the actual approval
queue, the actual reply, the actual number. No hype, no thread-bait.

**2. Hand-recruit 20 design partners.** Not "sign-ups" — named people you talk to
weekly. Free for 3 months in exchange for a call every Friday. Source them from:

- Indie Hackers, r/SaaS, r/Emailmarketing, r/coldemail, r/sales
- Lenny's Slack, RevGenius, GrowthMentor
- Your own network first — the first 5 should be people you can call

**3. Show HN.** Do it *after* you have ~20 users and a working dogfood story, not
before. Title angle: "Show HN: An AI outreach agent that can't send without your
approval." The HN audience is hostile to cold email and receptive to the control
framing — lean into that tension in the post body, don't hide it.

### Tier 2 — start at Day 45

**4. Comparison pages.** `/vs/instantly`, `/vs/smartlead`, `/vs/apollo`,
`/alternatives/lemlist`. High-intent, low-competition long tail. Be scrupulously
fair — list where they beat you (volume, mailbox count, warmup). Fairness is what
makes these rank and convert.

**5. Newsletter sponsorships.** GTM and founder newsletters run $200–$1,500 per
send with far better targeting than paid social. Test three, measure to signup,
kill the losers. This is your first real ad spend.

**6. Partner with fractional SDR consultants and small lead-gen agencies.** They
have the clients and hate the tooling. Offer 20–30% recurring revenue share. The
Agency tier exists for exactly this; one good partner is worth 50 self-serve
signups.

### Tier 3 — Day 90+, only if Tier 1–2 are working

**7. Product Hunt.** Only after your onboarding converts. A PH launch amplifies
whatever your activation rate already is — launching with a broken funnel wastes
the one launch you get.

**8. Google Search ads on competitor brand terms** (`instantly alternative`,
`smartlead alternative`). Narrow, high-intent, capped at $30/day to start. Never
broad match. Never Facebook or LinkedIn ads at this ARPU.

---

## 4. The 90-day sequence

### Days 1–14 — Unblock

- [ ] Submit Google OAuth verification / CASA Tier 2 (**do this on Day 1** — it's the long pole)
- [ ] Resolve the verification claim: re-add a provider, or rewrite the copy
- [ ] Real business address in the footer and in every outgoing email
- [ ] Stripe live, three plans, annual toggle wired to the pricing page
- [ ] Switch the default model off the free tier; A/B two models on real drafts
- [ ] Instrument COGS per lead / per draft in `usage.py`
- [ ] Analytics: signup → Gmail connected → first lead approved → first send

### Days 15–45 — 20 design partners

- [ ] Recruit 20 named design partners, free, weekly calls
- [ ] Run your own outreach campaign through Agent Hub; publish weekly numbers
- [ ] Start "build in public" cadence, 3×/week
- [ ] **Target: 10 of 20 reach first approved send within 24h of signup**
- [ ] Sell 3 done-for-you onboardings at $499 — real revenue, real learning

### Days 46–75 — First paying customers

- [ ] Turn on pricing; convert design partners at 50% lifetime discount
- [ ] Publish the dogfood case study with real numbers
- [ ] Show HN
- [ ] Ship comparison pages
- [ ] **Target: 10 paying customers, ~$1,000 MRR**

### Days 76–90 — First scalable channel

- [ ] Product Hunt launch
- [ ] Test 3 newsletter sponsorships
- [ ] Sign 2 agency partners
- [ ] **Target: 25 paying customers, ~$3,000 MRR, one channel with repeatable CAC**

---

## 5. Metrics

**North star:** approved sends per active account per week. It captures value
delivered, not vanity volume, and it's the number that predicts renewal.

**Activation:** first approved send within 24 hours of signup. Everything in
onboarding should serve this one number.

**Watch weekly:**

| Metric | Healthy at this stage |
|---|---|
| Signup → Gmail connected | > 60% (this is where OAuth friction shows up) |
| Gmail connected → first approved send | > 50% |
| Trial → paid | > 5% self-serve, > 30% for hand-recruited partners |
| Reply rate on sent emails | > 3% — below this your copy quality is the problem, not your funnel |
| Monthly logo churn | < 7% |
| Gross margin | > 80% |

If reply rate is under 3%, stop all marketing and fix the model. No channel
strategy survives a product that doesn't get replies.

---

## 6. The honest risk list

- **Google OAuth verification could take longer than 12 weeks or be refused.** It
  is a single point of failure for the entire product. Have a contingency: SMTP/app-password
  sending as a fallback path, or Nylas/Unipile as an intermediary.
- **Cold email regulation is tightening.** GDPR makes EU B2B outreach legally
  fraught. Stay US-first, keep the suppression list rigorous, keep unsubscribe
  one-click.
- **The approval queue is friction, and friction is churn.** Your differentiator
  is also your retention risk. Watch for users who stop opening the queue —
  that's your churn signal 30 days early. Batch approval and a mobile-friendly
  queue are the mitigations.
- **Incumbents can copy "approval mode" in a sprint.** Your moat isn't the
  feature; it's being the brand that stands for it. Which is why the
  build-in-public dogfooding matters more than any feature you could ship.

---

## Sources

- [Instantly.ai Pricing 2026 — Landbase](https://www.landbase.com/blog/instantly-ai-pricing)
- [Instantly.ai Pricing 2026 — Puzzle Inbox](https://puzzleinbox.com/blog/instantly-pricing-guide)
- [Smartlead Pricing 2026 — Landbase](https://www.landbase.com/blog/smartlead-pricing)
- [Smartlead Pricing 2026 — Amplemarket](https://www.amplemarket.com/blog/how-much-does-smartlead-really-cost)
- [Clay Pricing 2026 — Landbase](https://www.landbase.com/blog/clay-pricing)
- [Google restricted scope verification — Google for Developers](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)
- [Google CASA assessment overview — Deepstrike](https://deepstrike.io/blog/google-casa-security-assessment-2025)
