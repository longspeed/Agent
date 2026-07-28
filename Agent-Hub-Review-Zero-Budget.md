# Agent Hub — Second Review, Zero-Budget Path

Deeper pass over `server.py`, `auth.py`, `accounts_db.py`, `usage.py`, and the
legal pages — plus a $0 route through the two items that were blocked on paid
APIs.

---

## Already shipped since the last review

Verified in the code, not taken on trust:

- **Bounce loop** — `gmail.find_bounce()`, `bounces.py`, hard/soft split, auto-pause.
- **DNS pre-flight** — `dns_check.py` does SPF/DKIM/DMARC over DNS-over-HTTPS, no
  new dependency, correctly distinguishes "lookup failed" from "nothing published."
  Exposed at `/api/deliverability`, advisory-only. That last call is right.
- **COGS metering** — `usage.py` now prices per call with `MODEL_PRICES`, records
  input and output separately, and refuses to guess a split when the provider
  reports only a total (`usage.py:55-64`). This is the instrument you need to
  make the model decision below.
- **Google tokens encrypted at rest** — Fernet in `accounts_db.py:50`. The
  security page's claim is true.

---

## New findings

### A. `APP_SECRET_KEY` does double duty, and rotating it is unrecoverable

`accounts_db.py:50` derives the Fernet encryption key from `APP_SECRET_KEY`:

```python
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(APP_SECRET_KEY.encode()).digest()))
```

That same secret also signs session tokens, OAuth state, and **unsubscribe
tokens** (`auth.py:47-48`).

If you ever rotate that key — a leak, a laptop lost, or ordinary hygiene — three
things happen at once: every stored Google OAuth token becomes undecryptable and
every customer must reconnect Gmail; every active session drops; and **every
unsubscribe link you have ever sent stops working, permanently.**

That third one is the serious one. `auth.py:88-96` correctly documents that
unsubscribe tokens have no expiry because "an unsubscribe link must keep working
forever." But it can only keep working as long as `APP_SECRET_KEY` never changes.
A dead opt-out link is a CAN-SPAM problem, not an inconvenience.

**Fix (free):** split into three env vars — `APP_SECRET_KEY` for sessions,
`TOKEN_ENCRYPTION_KEY` for Fernet, `UNSUBSCRIBE_SIGNING_KEY` for opt-out. Then
make unsubscribe verification accept a list of keys (current + retired) so old
links survive rotation. About an hour of work; removes a landmine you can only
step on once.

### B. Logging out doesn't log you out

`create_session_token` (`auth.py:51`) signs `account_id.expires_at` with a 14-day
TTL. `/logout` clears the cookie, but the token itself stays valid for the full
14 days. There is no server-side revocation, and changing a password doesn't
invalidate existing sessions.

**Fix (free):** add a `session_epoch` integer to the accounts row, include it in
the signed payload, and compare on verify. Bump it on password change and on
"log out everywhere." Small change, and it's the difference between "we logged
you out" being true and being decorative.

### C. There is no plan system, so there is nothing to charge for

`/api/plan` (`server.py:403-413`) returns a hardcoded dict — one plan, "Pilot",
$49, with the comment *"Checkout is coming next."* There's no `plan` column, no
monthly lead quota, no enforcement anywhere. `DAILY_SEND_LIMIT` is a global
constant, not a per-plan value.

This is the actual gap between the product and revenue. Stripe costs nothing
monthly — only per transaction — so this is free to build today.

**Minimum to charge money:** a `plan` column, monthly quota counters read off the
`usage_events` you already record, enforcement at `prepare_drafts` and
`find_leads`, and Stripe Checkout with a webhook that sets the column. Your
metering groundwork means the quota half is mostly already done.

### D. Free LLM tiers may conflict with your own privacy policy

This one matters most given the budget constraint.

`privacy.html:82` discloses that OpenRouter receives "Lead name and company, your
meeting purpose, and **the text of replies your prospects send you**." That is an
honest disclosure — most competitors bury it.

But free-tier endpoints generally carry different data terms than paid ones.
OpenRouter states that prompts and completions sent to `:free` endpoints may be
eligible for use in model improvement by the upstream provider, and Google's
Gemini free tier says free-tier data may be used to improve their products.

Your prospect's private reply text going into someone's training corpus is hard
to square with "We never sell or share lead data" and with a DPA you're offering
to sign. See the split recommended below — it's the fix, and it's still free.

### E. Unchanged from last review

In-memory rate limiting and jobs (`ratelimit.py:1-6`), single-process only. Not
urgent at zero users; blocking at ~50.

---

## The $0 path

### Email verification: build it, don't buy it

You already own the hard part. `dns_check.py` established a DNS-over-HTTPS
pattern using nothing but `requests` — no new dependency, works on Windows. A
verifier reuses it directly.

**A free five-layer check, in yield order:**

1. **MX lookup** (reuse the `dns_check.py` DoH helper, `type=MX`). Does the domain
   accept mail at all? Highest-yield free check by a wide margin — `leads.py`
   derives domains from URLs, and a large share of those are marketing sites,
   parked domains, or CDNs that have no mail exchanger.
2. **Syntax validation.** Catches malformed extractions before they cost anything.
3. **Disposable-domain blocklist.** Public lists on GitHub, updated regularly, free.
4. **Role-account detection** — `info@`, `support@`, `admin@`, `sales@`, `contact@`.
   These reply at a fraction of the rate of a named mailbox and draw complaints.
   Flag them as low-confidence rather than sending blind.
5. **Learned local-part patterns.** `leads.py` guesses `first.last@domain`. Your
   new bounce loop tells you which patterns actually bounce, per domain. Record
   it, prefer patterns that have worked, and the guess gets better for free. The
   hard-bounce suppression you already shipped is the feedback signal.

**What this cannot do:** confirm a specific mailbox exists. Only SMTP `RCPT TO`
probing does that, and **I'd advise against it**: from a cloud IP it gets you
blocklisted, Gmail and Outlook accept-all at the RCPT stage so it returns false
confidence anyway, and it damages the sending reputation the whole product exists
to protect. Not worth it at any price, let alone as a shortcut.

**Then stack free API tiers on top,** for only the addresses that survive layers
1–5, so you never spend a credit on a domain with no MX record:

| Provider | Free allowance | Renews |
|---|---|---|
| Reoon | 20/day, up to ~600/month | Daily, ongoing |
| ZeroBounce | 100/month | Monthly, ongoing |
| MillionVerifier | 500 | One-time signup |

That's roughly **700 verifications/month recurring at $0**, plus 500 to start.
Your Trial tier is 50 leads. This comfortably covers the trial and your first
handful of paying customers — by which point verification is paying for itself.

**Honest limit:** at Solo-tier volume (750 leads/month) free tiers run dry. The
local checks still apply to everything; the API check becomes best-effort above
the free ceiling. Say so in the UI — show per-address confidence and where it
came from — rather than implying a guarantee you can't fund.

### Model quality: you're on the wrong free model, not the wrong price tier

This was never really a budget problem. `gpt-oss-20b:free` is a small model, and
better ones are also free.

| Option | Free allowance | Notes |
|---|---|---|
| **Groq** | ~14,400 requests/day, 30 RPM | Open-source models only (Llama-70B class). No credit card. Highest daily ceiling by far. |
| **Google AI Studio** | ~1,500 requests/day, 15 RPM, 1M TPM | Gemini 2.5 Flash. Strong model. Free-tier data may train. |
| **OpenRouter `:free`** | 20 RPM, **50 requests/day** unfunded | 1,000/day only if you've ever bought $10 in credits. |

Note that last row: **50 requests/day on an unfunded account.** At 25 sends/day
plus your two-attempt retry loop, a single active customer can exhaust your
entire daily quota. That alone is reason to move.

**Recommendation:** Groq as primary, Gemini Flash as fallback. Both $0, both
substantially larger models than what you're running.

**And you already built the eval harness.** `_validate_outreach()` in `agent.py`
returns a list of specific problems per generation. Run 50 drafts through each
candidate model, count how many pass on the first attempt, and compare. No new
tooling, no spend, and the answer is quantitative. If a bigger free model halves
your retry rate it also halves your request count — which matters when the
constraint is requests per day rather than dollars.

**The privacy split (finding D).** Send **first-touch drafts** to free endpoints:
they contain the prospect's name, company, a public research note, and your own
pitch. Nothing private. Keep **reply drafting** — which carries the prospect's
own words — off free endpoints until you can afford a paid one. Gemini Flash paid
is cheap enough that a single $49 customer covers it many times over.

Until then, either route replies through the paid tier of one provider as your
first small spend, or update `privacy.html` to disclose the training exposure
plainly. Do not leave the current DPA language standing over a free endpoint that
trains on the data.

---

## Revised order, all $0 except where noted

| # | Work | Cost |
|---|---|---|
| 1 | Swap to Groq/Gemini Flash; measure pass rate via `_validate_outreach` | $0 |
| 2 | MX + syntax + disposable + role-account verification, reusing `dns_check.py` | $0 |
| 3 | Layer Reoon + ZeroBounce free tiers behind the local checks | $0 |
| 4 | Split the secrets (finding A); key-list for unsubscribe tokens | $0 |
| 5 | Plan column, quota enforcement, Stripe Checkout | $0/mo, per-txn only |
| 6 | Session revocation (finding B) | $0 |
| 7 | Follow-up sequences | $0 |
| 8 | Paid reply-drafting endpoint | ~$5–20/mo, after first customer |

Only the last line needs money, and it comes after revenue rather than before it.

**The reframe worth keeping:** of the two things blocked on budget last time, one
wasn't a budget problem at all (better free models exist), and the other is ~80%
solvable with DNS lookups you're already equipped to do. The remaining 20% is
what you buy once someone has paid you.

---

## Sources

- [Groq free tier limits 2026](https://tokenmix.ai/blog/groq-free-tier-limits-2026)
- [Gemini API free tier 2026](https://tokenmix.ai/blog/gemini-api-free-tier-limits)
- [Google AI Studio free tier limits](https://docs.bswen.com/blog/2026-03-23-google-ai-studio-free-tier-limits/)
- [OpenRouter free tier limits 2026](https://klymentiev.com/blog/openrouter-free-tier)
- [Best free email verification APIs 2026](https://prospeo.io/s/free-email-verification-api)
- [Verifying a cold email list free, 2026](https://puzzleinbox.com/blog/how-to-verify-cold-email-list-free-2026/)
