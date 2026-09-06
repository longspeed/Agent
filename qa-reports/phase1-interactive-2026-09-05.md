# Sendkeep Phase 1 interactive QA — 2026-09-05

Target: http://localhost:8000. Authenticated Codex browser, existing account.
Report-only test; no application source edits or explicit promise/contact mutations. Real Gmail reply checking can update monitoring state; the existing worker also continued running.

## Verdict

Not ready for an unqualified pilot sign-off. The ledger migration is serving real records, normal navigation works, and automated regressions pass. A live backend read failure, incomplete scheduled-item controls, and inaccessible audit history remain. No claim is made that every external-data workflow passed.

## Confirmed findings

1. **Medium — Scheduled promise cannot be dismissed through the ledger.** Open Promises. The existing scheduled card exposes only Open Gmail. No dismiss control is present even though scheduled → dismissed is part of the approved state machine. Reproduced after navigating to Today and using Back. Screenshot captured inline in the QA conversation.
2. **Medium — Audit history is not inspectable.** A scheduled card displays “1 audit event”, but it is plain text with no disclosure or link. Neither the confirmation timestamp nor transition details are visible. The evidence/source link is present. Screenshot captured inline.
3. **Low — Ledger title count contradicts the visible total.** “Promise ledger 0” sits above “What do I owe? 1” and one scheduled item. The badge apparently counts decisions, but it has no explanatory label. Reproduced through Back navigation.
4. **Medium — Checkout remains unavailable.** Settings explicitly says “Online checkout is not configured on this deployment.” No upgrade checkout can be exercised. Screenshot captured inline. This is a pre-existing deployment gap outside Phase 1.
5. **High — Transient database read failure escapes as HTTP 500.** During the tablet navigation sequence, the server logged `GET /api/me` returning 500 with `httpx.ReadError: [WinError 10035] A non-blocking socket operation could not be completed immediately`. Another traceback passed through `list_campaigns → _account → accounts_db.get_account → PostgREST`. The ledger screenshot simultaneously showed “Gmail attention”, without explaining that the account/database read failed. A subsequent Settings visit and ledger reload recovered to “Gmail active”. This occurred once during this run; the trigger is not deterministically reproduced. Treat transient service unavailability separately from Gmail authorization failure. This is runtime evidence, not evidence that the ledger SQL migration failed.
6. **Medium — Public mockup contains identifiers matching account contacts.** The anonymous homepage displays two masked email local-parts that match contacts in the authenticated Threads table. The mockup is labeled Example, but that does not anonymize matching identifiers. Replace them with clearly fictional reserved-domain examples. This observation does not establish a live API data leak or prove that the sample message bodies are real.
7. **Medium — Guide gives contradictory instructions.** Its introduction says the core reads Sent and never sends, with Gmail/Gemini as the reply surface. Step 8 then describes an editable reply queue and sending replies inside Outreach. Its blank Sheet Status reference says “Will be emailed on the next run,” despite manual approval being required in this deployment. The visible public landing also remains revenue-detector-led while the signed-in home is ledger-led. Align the Guide and funnel with the actual supported contract; this test does not establish an unsafe automatic-send path.

### Non-blocking usability observation

At 375px, Threads keeps its wide table in a horizontally scrollable container, leaving actions offscreen initially. No page-level overflow was measured, but a compact mobile contact card would make actions easier to find. The ledger similarly spends its first phone screen on four summary cards and empty sections before the one scheduled item becomes visible.

## Checks completed

- Promise Ledger loads real scheduled and dismissed records.
- Dismissed history expands and preserves evidence.
- Today and Follow-ups empty states load.
- Today filters respond; browser Back restores Promises and disclosure state.
- Threads shows existing contacts; real Gmail Check shows No reply yet and re-enables its button.
- Settings loads connection and worker information; manual Sheet input disclosure opens.
- Browser console queries returned no warnings/errors, but server logs caught the HTTP 500 described above. A clean browser console is not proof of backend health.
- Python suite: 571 passed.
- Composer browser suite: passed (intercepted API fixtures).
- Responsive filter browser suite: passed at 375px and 768px.
- Build freshness: passed.
- Signed-in home loads live counts (one owed, zero due/completed) and its promise links target `/outreach?tab=promises`.
- Authenticated Guide shows Settings/Log out; anonymous Guide shows Sign in.
- Anonymous `/outreach?tab=threads` redirects to `/login?next=/outreach%3Ftab%3Dthreads`; the Google sign-in link preserves that destination. A fresh OAuth consent/callback round trip was not performed.
- Invalid tab normalizes to `/outreach?tab=inbox`.
- Login renders with styling. External CDN blocking was not simulated in this live run.
- Public pricing switch changes $29/$49 monthly offers to $290/$490 yearly offers; FAQ disclosure responds; sample workflow actions are explicitly non-interactive labels.
- Public template links were clicked while logged out: CSV and Excel requests returned 200 in server logs. Downloaded file contents were not manually inspected in this browser run.
- Account export button produced `/api/account/export` 200. Export field completeness was not audited.
- Privacy, Security, Terms, Acceptable Use, and Data Processing pages loaded through anonymous browser navigation.
- At 375px, Today filters wrap and the last Verify filter is clickable; Promises and Threads navigation works. At 768px, ledger summary becomes two columns and public pricing uses a grid. Measured no page-level horizontal overflow in these checked views. Screenshots captured inline at both widths and desktop; viewport restored afterward.
- Scheduled December 12, 2026 date and UTC reminder remain visible after reload. This verifies persisted display, not fresh date extraction.

## Coverage limits

No live emails sent, contacts deleted, tokens disconnected, settings saved, or existing promises confirmed/dismissed/completed. The account currently has no detected or due promise available for a non-destructive lifecycle test. Automated fixtures do not establish live Gmail lifecycle correctness.

Also not exercised: live checkout, test-alert delivery, worker outage/recovery drill, new-account discovery, uncertain Gmail-send reconciliation, clean database reset/pgTAP, or a new OAuth round trip. These remain release gates, not implied passes.

## Recommended next order

1. Reproduce and handle the transient Supabase transport failure; show service-unavailable rather than implying Gmail needs reconnecting.
2. Remove account-matching identifiers from public demo content.
3. Add scheduled-promise dismissal and inspectable audit details; clarify what the heading badge counts.
4. Reconcile Guide instructions and finish checkout configuration before paid pilots.
5. Run an isolated promise lifecycle: detect → confirm → scheduled → due → complete, plus dismissal and alert delivery. Do not use real prospect promises as disposable test data.

## Handoff

Application source was not changed. The server remains running, and the test browser is left on the Promise ledger. This report is the only task-authored workspace artifact.
