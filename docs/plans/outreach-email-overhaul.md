# Outreach email quality + safety overhaul

## Context

A review of the auto-send path (`outreach-agent/agent.py` → `send_outreach.py` → `gmail.py`)
found the prompt itself is well built, but the email it produces can't be good because of
what it is fed and what is never checked afterward:

1. The lead agent writes a per-lead `reason` sentence into `COL_LEAD_REASON`
   (`leads.py:111-116`), and `send_outreach.main` never passes it to the model — the only
   real personalization in the system is collected, stored, then dropped.
2. The default `meeting_purpose` ("a quick intro call to see if there's a fit to work
   together") is content-free, so the model is asked to write "what's in it for them" from
   a string that says nothing.
3. `agent.py:168-169` — if the model omits/rewrites the `Subject:` prefix, the first line of
   the *body* silently becomes the subject and vanishes from the email.
4. Nothing verifies the output before an irreversible send: not that the calendar link
   survived, not that the subject is non-empty, not that placeholders are gone.
5. Replies get a human review queue (`watch_replies.py`), outreach does not. The campaign
   preview approves *recipients*, never *text* — unreviewed LLM prose goes to strangers under
   the user's own name and Gmail reputation.
6. The signature is a bare first name, and there is no opt-out at all —
   `static/acceptable-use.html:63-66` documents this as a known gap.

**Outcome:** every outreach email is personalized from verified lead data, validated before
it can be sent, approved by a human, and legally sendable.

Two scope decisions were confirmed by the user: **full per-email approval queue** (not a
lightweight preview) and **real one-click opt-out** (not just a signature fix).

---

## Phase 0 — Schema (blocks everything; user runs the SQL)

Supabase MCP is unauthorized in this session, so these must be applied by hand in the SQL
editor. Add them to the schema block in `outreach-agent/README.md:51-93`, matching its style.

```sql
alter table public.accounts add column sender_company text not null default '';

create table public.outreach_drafts (
    id bigint generated always as identity primary key,
    account_id uuid not null references public.accounts(id) on delete cascade,
    row_index integer not null,
    name text not null default '',
    email text not null default '',
    company text not null default '',
    subject text not null default '',
    body text not null default '',
    status text not null default 'pending',  -- pending | sent | discarded
    created_at timestamptz not null default now()
);
-- Dedupe guard: re-running "prepare" must never queue a row twice.
create unique index outreach_drafts_pending_row_idx
    on public.outreach_drafts (account_id, row_index) where status = 'pending';

create table public.suppressions (
    id bigint generated always as identity primary key,
    account_id uuid not null references public.accounts(id) on delete cascade,
    email text not null,
    source text not null default 'unsubscribe_link',
    created_at timestamptz not null default now(),
    unique (account_id, email)
);

alter table public.outreach_drafts enable row level security;
alter table public.suppressions enable row level security;
```

New config in `outreach-agent/config.py`:
- `PUBLIC_BASE_URL` (default `http://localhost:8000`) — the unsubscribe link must be absolute.
- `DEFAULT_MEETING_PURPOSE` — the exact DB default string, so Phase 2 can detect "untouched".

---

## Phase 1 — Generation quality (`outreach-agent/agent.py`)

All pure logic, all covered by the existing fakes in `tests/test_outreach_agent.py`.

**1a. Feed the lead reason.** `generate_outreach_email(account, name, company, lead_reason="")`.
`send_outreach` reads `row[sheets.COL_LEAD_REASON]` alongside name/email/company
(`send_outreach.py:83-86`). In the prompt, add a "Research note" line only when non-empty, with
an explicit rule: the note came from automated web research, so it may be referenced as the
*reason for writing* but never extrapolated beyond and never asserted as intimate knowledge.
This extends the existing anti-fabrication rules at `agent.py:24-29` rather than weakening them.

**1b. Robust subject parsing.** Replace the `partition("\n")` + `removeprefix` at
`agent.py:168-169` with a case-insensitive regex tolerating markdown and dash separators
(`**Subject:**`, `subject -`, …). Critically: if line 1 does *not* match, do **not** consume it —
keep the whole text as body and let 1c handle the missing subject. This alone stops the silent
sentence loss.

**1c. Pre-send validator + one retry.** New `_validate_outreach(subject, body, ctx) -> list[str]`
checking: subject non-empty and ≤ 90 chars; calendar link present literally; opt-out line present
(Phase 4); no placeholders (`[`, `{{`, "Your Name"); no markdown; no banned phrases (the
`OUTREACH_SYSTEM` jargon list, plus "hope this email finds you well"); no em dashes; body length
sane. On failure, retry generation **once** with the problems appended as a correction; if it
still fails, raise. A raise leaves the sheet row Pending and surfaces in the batch failure
list — consistent with the existing crash-safety design in `send_outreach.py:31-44`.

**1d. Prompt hardening.** Add the current AI tells to the ban list (em dash, "Quick question
about {Company}" as a subject, "It's not X, it's Y"). Change the signoff rule to first name +
`sender_company`.

---

## Phase 2 — Block the content-free default

`generate_outreach_email` already hard-blocks a missing calendar link (`agent.py:151-152`);
give `meeting_purpose` the same treatment when blank or still equal to `DEFAULT_MEETING_PURPOSE`.

Surface it *before* the batch rather than failing 25 rows: `_campaign_preview` (`server.py:523`)
returns a `blockers: []` list, and `static/outreach.html` disables the primary button and shows
the reason. Existing accounts sitting on the default will be blocked — intended, and the preview
explains why.

Settings: add `sender_company` to `EDITABLE_SETTINGS` (`accounts_db.py:28-34`), plus the field in
`app/src/SettingsPage.tsx` (the live React settings page, per `server.py:204-209`).

---

## Phase 3 — Per-email approval queue

New `outreach-agent/drafts_db.py`, mirroring `reviews_db.py` almost line for line
(`add_draft` / `list_pending_drafts` / `get_draft` / `mark_sent` / `discard`, every query
filtered on `account_id`).

**Split the one-shot send into two phases:**

- `send_outreach.prepare_drafts(account)` — generate + validate + insert draft rows. No Gmail
  send, no sheet write; rows stay Pending. Capped at `remaining_today` so the LLM never drafts
  mail that can't go out. Keeps `MAX_WORKERS = 4` and the `require_full_header` gate.
- Extract the post-send marking block from `_send_one` (`send_outreach.py:48-68`) into
  `mark_row_sent(account, row_index, email, thread_id, body)` so the approval path reuses the
  existing retry/backoff logic **unchanged** — the send → mark-immediately invariant is the one
  thing in this file that must not regress.

**API** (`server.py`, mirroring the reply endpoints at `server.py:616-636`):
- `POST /api/outreach/campaigns/prepare` replaces `.../send` (keep the rate limit and the
  `_accounts_sending` lock).
- `GET /api/outreach/drafts`
- `POST /api/outreach/drafts/{id}/send` — re-check the daily cap and the suppression list at
  send time (not draft time), then `gmail.send_email` → `mark_row_sent` → `drafts_db.mark_sent`.
- `POST /api/outreach/drafts/{id}/discard`
- `POST /api/outreach/drafts/send-all` — routed through the same per-draft path so the cap,
  suppression check and row marking all still hold. Without it the UX is punishing at 25/batch.

**UI** (`static/outreach.html`): add a "Drafts awaiting approval" section above "Needs review",
cloning the `review-card-template` pattern (`:250-265`) with an editable subject input plus body
textarea, and copying the fetch/render logic at `:424-465`. Retitle the primary button to
"Prepare drafts". The "How it works" copy at `:190` and `:236` currently describes automatic
sending and must be rewritten. `server.py:381` plan copy ("human-reviewed replies") should
become "human-reviewed emails" — note `test_server_plan_copy_is_accurate` asserts this string.

Side benefit: real sends become one-at-a-time and human-paced, which dissolves the
4-concurrent-sends burst pattern. Only `send-all` needs jitter — a
`time.sleep(random.uniform(3, 12))` between sends there.

---

## Phase 4 — One-click opt-out

**Token** (`outreach-agent/auth.py`, reusing `_sign` at `:44-45`): `create_unsubscribe_token` /
`verify_unsubscribe_token`, signing a **namespaced** payload (`unsub:<account_id>:<email>`) so a
token can never be cross-used as a session or OAuth-state token. No TTL — unsubscribe links must
work forever. Follow the existing dot-count convention comment at `:66-69`.

**Suppression store**: new `outreach-agent/suppressions_db.py` (`add` / `is_suppressed` /
`list_all`), same shape as `reviews_db.py`.

**Public routes**, added to `PUBLIC_PATHS` (`server.py:48`):
- `GET /unsubscribe?t=…` → confirmation *page only*, no side effect. Mail-client link scanners
  prefetch GETs; a GET that unsubscribes would fire on its own.
- `POST /unsubscribe` → suppress, best-effort mark the sheet row "Unsubscribed", render
  confirmation. Doubles as the RFC 8058 one-click endpoint.

**Delivery** (`gmail.py:17-26`): `send_email` takes the unsubscribe URL and sets
`List-Unsubscribe: <https://…/unsubscribe?t=…>` plus
`List-Unsubscribe-Post: List-Unsubscribe=One-Click`.

**Body line**: appended in code after generation, never generated by the model — a placeholder
the LLM is trusted to reproduce is a link it will eventually mangle. The Phase 1c validator
asserts it survived.

**Enforcement**: `sheets.campaign_readiness` (`sheets.py:108-126`) filters suppressed addresses
out of `eligible`, and `drafts/{id}/send` re-checks immediately before the send.

Finally, rewrite `static/acceptable-use.html:63-66` — it currently promises this feature does
not exist.

---

## Verification

**Automated** — `python tests/test_outreach_agent.py` (self-contained, no pytest) and
`python -m py_compile server.py outreach-agent/*.py`.

New cases, all using the existing `patched` / `fake_sheets_service` helpers:
- lead reason reaches the prompt; omitted cleanly when blank
- subject parsing: `**Subject:**`, lowercase `subject:`, and the missing-prefix case that must
  *not* eat line 1
- each validator rule; retry-then-raise leaves the row Pending
- default `meeting_purpose` blocks generation
- `prepare_drafts` inserts and neither sends nor writes the sheet; re-running does not duplicate
- draft send marks the row, and refuses when the cap is exhausted or the address is suppressed
- unsubscribe token roundtrip, tamper, and cross-token rejection (mirror
  `test_session_token_roundtrip_and_tamper` at `:771`)
- suppressed addresses drop out of `campaign_readiness`

Existing tests needing updates: the three `generate_outreach_email` patches (`:601`, `:636`,
`:665`) for the new signature, the `test_send_outreach_*` group (`main()` no longer sends), and
`test_server_plan_copy_is_accurate` (`:759`).

**Manual end-to-end**, with a sheet whose rows carry a LeadReason and a test address you own:
1. Set a real `meeting_purpose` + `sender_company` in Settings; confirm the send button is
   blocked until you do.
2. Prepare drafts → confirm nothing is sent and the sheet is untouched; check the drafted copy
   actually uses the lead reason.
3. Edit one draft, approve it, confirm arrival, and confirm the row flips to Sent exactly once.
4. In Gmail, use the client's own unsubscribe control (proves the `List-Unsubscribe` headers),
   then confirm the address is suppressed and drops out of the next prepare run.
5. Re-run prepare and confirm no duplicate drafts appear.
