"""Measure how well a candidate model writes cold outreach, scored by the same
validator that decides in production whether a draft is safe to queue.

The point is to make the model choice quantitative instead of a vibe. agent.py
already rejects a draft for a specific, enumerated reason -- banned phrase,
non-Latin token, missing subject, dropped CTA link -- and retries once. So the
numbers that matter are:

  first-pass %   how often the model gets it right with no retry
  final %        how often it gets there within production's 2 attempts
  calls/email    requests spent per accepted email, first-pass and retries
                 together. This is the number that matters on a free tier,
                 where the budget is requests per day rather than dollars: a
                 model with double the first-pass rate costs half the quota.
  failures       which validator rule fired, so a bad score is diagnosable.
                 Lots of "garbled" means the model is too small; lots of
                 "banned phrase" is a prompt problem, not a model problem.

Nothing here sends mail or touches the sheet. It calls the provider and the
validator, and prints a table.

Run:
  python outreach-agent/eval_models.py                     # every configured provider
  python outreach-agent/eval_models.py --n 50
  python outreach-agent/eval_models.py --provider groq --provider openrouter
  python outreach-agent/eval_models.py --model llama-3.1-8b-instant   # override on
                                                                      # the first provider
  python outreach-agent/eval_models.py --show-passes       # print the actual emails
"""

import argparse
import re
import statistics
import sys
import time

import agent
import providers
import usage

# Realistic sender profiles. Two, because a vague-but-legal goal and a sharply
# specific one are different difficulties and a model that only handles the
# second one is not usable.
SENDERS = (
    {
        "label": "specific goal + CTA link",
        "account": {
            "sender_name": "Oanh",
            "sender_company": "Ledgerloop",
            "meeting_purpose": (
                "we cut invoice reconciliation from 3 days to about 4 hours for finance "
                "teams running Xero and Stripe together; I want a 15 minute call to show "
                "how it handles their volume"
            ),
            "calendar_booking_link": "https://cal.com/oanh/15min",
            "custom_instructions": "",
        },
    },
    {
        "label": "no link, reply-only ask, custom instructions",
        "account": {
            "sender_name": "Sam",
            "sender_company": "",
            "meeting_purpose": (
                "a free 20-page teardown of how their onboarding email sequence compares "
                "to the 40 SaaS companies we benchmarked; I want them to reply asking for it"
            ),
            "calendar_booking_link": "",
            "custom_instructions": "Keep it under 90 words. British spelling.",
        },
    },
)

# Leads shaped like what leads.py actually produces: sometimes a company,
# sometimes a research note, sometimes neither. The no-note rows are the hard
# ones -- with nothing verified to open on, weak models start inventing.
LEADS = (
    ("Priya Raman", "Northwind Logistics",
     "spoke at a supply chain conference in May about consolidating three ERP systems"),
    ("Tom Okafor", "Bright Harbour Dental", ""),
    ("Elena Vasquez", "Corvid Analytics",
     "posted that their finance team closes the month on spreadsheets exported from four tools"),
    ("Daniel Whitfield", "", "runs a 12-person agency and writes a newsletter about pricing"),
    ("Mei Ling Chen", "Fernpoint Studio", ""),
    ("Adaeze Nwosu", "Kestrel Freight",
     "their careers page lists two open roles for accounts payable clerks"),
    ("Jonas Berg", "Alpine Rehab Group",
     "took over as operations lead in January after the practice merged with two clinics"),
    ("Ruth Adeyemi", "", ""),
    ("Marco Bellini", "Tessera Interiors",
     "wrote a post about quoting jobs from paper measurements"),
    ("Hana Sato", "Blue Kite Learning",
     "launched a second product line for school districts last quarter"),
)

UNSUBSCRIBE_URL = "https://app.example.com/u/eval-token-placeholder"

# Buckets the validator's human-readable problems into a short label. The
# validator returns prose on purpose (it goes into the retry prompt and in front
# of operators), so the classification lives here rather than changing its
# output contract. Order matters: first match wins.
_FAILURE_KINDS = (
    ("garbled", r"non-English/garbled"),
    ("no subject", r'No "Subject:" line'),
    ("subject too long|subject mislabelled", r"^Subject (is|still contains)"),
    ("body too short", r"reads as truncated"),
    ("body too long", r"cut it to 3-5 sentences"),
    ("dropped CTA link", r"scheduling link is missing"),
    ("dropped opt-out", r"unsubscribe line is missing"),
    ("banned phrase", r"^Remove these banned phrases"),
    ("filler", r"^Cut this empty filler"),
    ("em/en dash", r"em/en dashes"),
    ("placeholder", r"^Unfilled placeholder"),
    ("markdown", r"^Remove markdown"),
)


def classify(problem):
    for label, pattern in _FAILURE_KINDS:
        if re.search(pattern, problem):
            return label
    return "other"


def scenarios(n):
    """n (sender, lead) pairs, cycling both lists so the mix stays balanced at
    any n instead of over-weighting whatever comes first."""
    out = []
    for i in range(n):
        sender = SENDERS[i % len(SENDERS)]
        lead = LEADS[(i // len(SENDERS)) % len(LEADS)]
        out.append((sender, lead))
    return out


def _one_attempt(provider, system, prompt, calendar_link):
    """A single generation, scored. Returns (problems, subject, body, tokens)."""
    text = agent._post(provider, system, prompt, usage.DRAFT_EMAIL, may_wait=True)
    subject, body = agent._split_subject(text)
    body = f"{body}\n\n{agent._opt_out_line(UNSUBSCRIBE_URL)}"
    problems = agent._validate_outreach(subject, body, calendar_link, UNSUBSCRIBE_URL)
    return problems, subject, body, text


def run(provider, cases, sleep, show_passes):
    """Production's exact 2-attempt loop, per scenario, with the outcome of each
    attempt recorded separately."""
    stats = {
        "first_pass": 0, "final_pass": 0, "calls": 0, "errors": 0,
        "latencies": [], "failures": {}, "examples": [],
    }
    for i, (sender, lead) in enumerate(cases, 1):
        name, company, note = lead
        account = sender["account"]
        base = agent.outreach_prompt(account, name, company, note)
        calendar_link = account["calendar_booking_link"]

        prompt, passed_on = base, None
        last_problems, last_body, last_subject = [], "", None
        for attempt in (1, 2):
            started = time.monotonic()
            try:
                stats["calls"] += 1
                problems, subject, body, raw = _one_attempt(
                    provider, agent.OUTREACH_SYSTEM, prompt, calendar_link
                )
            except Exception as e:
                stats["errors"] += 1
                print(f"  [{i}/{len(cases)}] call failed: {e}")
                break
            stats["latencies"].append(time.monotonic() - started)
            if sleep:
                time.sleep(sleep)  # once per call, to respect a per-minute cap
            last_problems, last_subject, last_body = problems, subject, body
            if not problems:
                passed_on = attempt
                break
            if attempt == 1:
                for problem in problems:
                    kind = classify(problem)
                    stats["failures"][kind] = stats["failures"].get(kind, 0) + 1
                prompt = agent.retry_prompt(base, raw, problems)

        if passed_on == 1:
            stats["first_pass"] += 1
        if passed_on:
            stats["final_pass"] += 1
            if show_passes and len(stats["examples"]) < 3:
                stats["examples"].append((last_subject, last_body))
        mark = {1: "pass", 2: "pass(retry)"}.get(passed_on, "FAIL")
        detail = "" if passed_on else f"  <- {'; '.join(last_problems)[:110]}"
        print(f"  [{i}/{len(cases)}] {mark:<12}{name}{detail}")
    return stats


def report(results, n):
    print("\n" + "=" * 78)
    print(f"{'provider / model':<42}{'1st':>6}{'final':>7}{'calls':>7}{'p50 s':>7}")
    print("-" * 78)
    for label, s in results:
        first = s["first_pass"] / n * 100
        final = s["final_pass"] / n * 100
        # Calls per accepted email: the free-tier budget line. Undefined with
        # nothing accepted, which is itself the answer.
        per_email = f"{s['calls'] / s['final_pass']:.2f}" if s["final_pass"] else "n/a"
        p50 = statistics.median(s["latencies"]) if s["latencies"] else 0
        print(f"{label[:42]:<42}{first:>5.0f}%{final:>6.0f}%{per_email:>7}{p50:>7.1f}")
    print("=" * 78)

    for label, s in results:
        if not s["failures"] and not s["errors"]:
            continue
        print(f"\n{label} -- first-attempt rejections by rule:")
        for kind, count in sorted(s["failures"].items(), key=lambda kv: -kv[1]):
            print(f"    {count:>3}x  {kind}")
        if s["errors"]:
            print(f"    {s['errors']:>3}x  call errored (not a quality signal)")

    for label, s in results:
        for subject, body in s["examples"]:
            print(f"\n--- {label} ---\nSubject: {subject}\n\n{body}")

    if len(results) > 1:
        best = max(results, key=lambda r: (r[1]["final_pass"], r[1]["first_pass"]))
        print(f"\nBest final pass rate: {best[0]}")
        print(
            "A higher first-pass rate is worth more than it looks on a free tier: "
            "every retry is a second request against the daily quota."
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--n", type=int, default=20,
                        help="scenarios per model (default 20; the review suggests 50)")
    parser.add_argument("--provider", action="append", default=[],
                        help="provider name to test; repeatable. Default: all configured.")
    parser.add_argument("--model", default="",
                        help="override the model id on the tested provider(s)")
    parser.add_argument("--sleep", type=float, default=0.0,
                        help="seconds between calls, to stay under a requests-per-minute cap")
    parser.add_argument("--show-passes", action="store_true",
                        help="print up to 3 accepted emails per model -- the validator "
                             "scores rule compliance, not whether the writing is any good")
    args = parser.parse_args()

    chain = providers.available()
    if args.provider:
        wanted = [p.lower() for p in args.provider]
        chain = tuple(p for p in chain if p.name in wanted)
        missing = [w for w in wanted if not any(p.name == w for p in chain)]
        if missing:
            print(f"Not configured (no API key set): {', '.join(missing)}")
    if args.model:
        from dataclasses import replace
        chain = tuple(replace(p, model=args.model) for p in chain)
    if not chain:
        print("No provider configured. Set GROQ_API_KEY (free, no card) or GEMINI_API_KEY.")
        return 1

    cases = scenarios(args.n)
    results = []
    for provider in chain:
        plural = "scenario" if args.n == 1 else "scenarios"
        print(f"\n### {provider.display} -- {args.n} {plural}, up to 2 attempts each")
        results.append((f"{provider.label} / {provider.model}",
                        run(provider, cases, args.sleep, args.show_passes)))
    report(results, args.n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
