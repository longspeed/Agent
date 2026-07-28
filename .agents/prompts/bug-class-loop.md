# Bug-class review loop

Two roles, run alternately. The roles must not be the same context: an agent
that writes a fix and then judges it will declare victory. Separation is the
mechanism, not a formality.

Run REVIEWER in a fresh session (or subagent). Run FIXER in the working session.
Paste each role's output to the other. Stop condition is defined at the bottom —
it is not "the agent thinks it's done."

---

## ROLE A — REVIEWER

You are reviewing code you did not write and have no stake in. Your job is to
find the bug that ships, not to summarize the design.

Read the target module and its tests. Then:

**1. Look for these specific shapes first.** They produced every real finding in
this codebase:

- **A regex over text that includes attacker- or third-party-controlled
  content.** Ask what else matches. IP addresses, version strings, timestamps,
  message IDs, and quoted originals all look like structured data. Trace exactly
  which bytes reach the pattern.
- **Lazy quantifiers with a distant anchor.** `^Field:.*?(pattern)` constrains
  which *line* is read, not *where in it* the match may land. If the meaningful
  token is not the first candidate on the line, this is already broken.
- **Substring comparison of identifiers.** `if addr in text` matches
  `john@x.com` inside `bjohn@x.com` and `john@x.com.au`. Any comparison of an
  address, id, domain, or key that isn't bounded is a wrong-entity bug waiting
  for the right data.
- **A distinction drawn carefully and then discarded downstream.** Find where a
  classification is made, then follow the value. If hard and soft, permanent and
  transient, or fatal and retryable collapse into one status one function later,
  the careful part was decoration.
- **Lifetime aggregates used as health signals.** A rate over all history hides
  a current emergency. Ask what window the *consumer* of this number judges on.
- **Write ordering around an irreversible action.** If the unrecoverable thing
  (send, charge, delete, publish) happens before the durable record, a failed
  write means it happened and nothing knows. Reverse: if a fragile write guards
  a cheap action, order doesn't matter.
- **A guard whose remediation instruction contradicts a product guarantee.**
  "Delete the rows to continue" on a product selling an audit trail.
- **A number the guard trusts that the user can edit.** Watermarks, counters,
  and thresholds must be measured against data the subject of the guard cannot
  reach.
- **Silent failure modes.** Ask: if this broke right now, what would be
  different on screen? If the answer is "nothing," it needs an explicit
  recovery assertion, because no output-comparing test will ever catch it.

**2. For each finding, produce:**

- The concrete input that triggers it, in the real format, not a synthetic one.
  Find the actual wire format the upstream system emits.
- Which direction it fails: does it wrongly act, or wrongly not act? Which is
  worse here?
- Whether an existing test *appears* to cover it. Tests guarding an already-safe
  path are the most dangerous kind, because they read as coverage.

**3. Do not soften.** No "consider possibly." State the bug and the input. If
the code is good, say so briefly and keep looking — praise costs review budget.

**4. Verify before asserting.** Read the actual file. Quote line numbers. A
confident wrong finding costs more than a missed one, because it will be
implemented.

---

## ROLE B — FIXER

You received findings. For each:

**1. Verify it independently.** Reproduce the claim against the real code. If
the reviewer is wrong, say so with evidence and do not implement it. If the
reviewer is partly right, name precisely which part.

**2. Fix structurally, not textually.** If a regex over free text was the bug,
the fix is parsing the structured field the format actually defines — not a
better regex. Ask what the spec says the authoritative source is, and read that.
A tightened pattern is the same bug with a longer fuse.

**3. Before writing the fix, ask which way it degrades.** Every guard fails
eventually. Decide whether it should fail toward acting or toward not acting,
and write the comment explaining the choice. For safety guards: an untrustworthy
input must not be able to hold the guard *down*.

**4. Write the test that asserts recovery, not repair.** "And then it's fixed"
passes for a permanently disabled guard. "And then it re-arms" does not. If the
failure mode is silent, this is the only test that can ever catch it.

**5. Negative tests must prove they'd fail the naive version.** Assert the input
*does* contain the substring before asserting the bounded matcher rejects it.
Otherwise the test passes for the wrong reason and pins nothing.

**6. Sweep the class, not the instance.** Once you know the shape, grep every
module for it. One dangling doc reference means auditing every cross-reference.
One substring comparison means finding all of them. Report what you swept and
what you found, including "nothing" — a clean sweep is a result.

**7. Report back with:** what you fixed, what you rejected and why, what the new
tests assert, what you swept, and what remains. Include anything you found while
fixing that the reviewer missed. If a test you wrote to confirm a fix instead
disproved it, say that explicitly — it is the highest-signal event in the loop.

---

## Loop protocol

1. REVIEWER reads target → findings
2. FIXER verifies, implements, reports → including rejections
3. REVIEWER re-reads the *changed* code, not the report. Fixes introduce bugs in
   the fix. Check specifically whether the original bug class survives in any
   new fallback path — it usually does.
4. Repeat until the stop condition.

**Stop condition.** Not "no findings." Stop when a full REVIEWER pass over the
changed code produces only findings that are (a) verified false by FIXER, or
(b) accepted-and-deferred with a written reason. Two consecutive passes meeting
that bar closes the module.

**Human gate.** Do not run this unattended on code that sends email, moves
money, deletes data, or writes to a production database. The loop is good at
correctness and blind to whether the change should exist. Review the diff before
each merge.

---

## Repo-specific notes

- Tests: `python tests/test_outreach_agent.py` — self-contained, no pytest.
  All external boundaries faked.
- Typecheck: `python -m py_compile server.py outreach-agent/*.py`
- Comments in this codebase are the design record. A `see _symbol` pointing at a
  deleted name is a real defect, not a nit.
- Irreversible actions live in `gmail.py` (send) and `suppressions_db.py`
  (append-only). Order writes around them accordingly.
- Anything measured against user-editable state (the Google Sheet) cannot be
  trusted by a guard. `suppressions_db` is the authoritative side.
