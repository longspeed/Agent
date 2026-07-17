import json
from concurrent.futures import ThreadPoolExecutor

from agent import _chat
import email_verification
import search
import sheets

MAX_QUERIES = 3
MAX_RESULTS_PER_QUERY = 5
MAX_CANDIDATES_FED_TO_LLM = 15
# Same conservative worker count as send_outreach.py's send loop -- not a
# verified NeverBounce-specific rate limit, just avoiding an unbounded burst
# of concurrent requests to a third-party API.
MAX_VERIFY_WORKERS = 4

QUERY_SYSTEM = """You turn a description of an ideal customer/lead into concrete web search
queries that would surface real people or companies matching that description.
Output exactly 3 short search queries, one per line, nothing else -- no numbering,
no explanation."""

EXTRACT_SYSTEM = """You are a lead researcher. Given a target description and a batch of web
search results, extract real companies/people that plausibly match the target.

Rules:
- Only include results that plausibly match the target description. Skip generic listicles,
  directories, review sites, and anything that isn't a specific real company or person.
- For each match, infer a likely contact email ONLY if you can reasonably derive the company's
  domain from the URL (e.g. first.last@domain, or an address literally present in the content).
  If you can't derive a plausible domain, set email_guess to null -- do not invent a domain.
- reason is one plain sentence: why this specific result fits the target description.

Output ONLY a JSON array, nothing else, in this exact shape:
[{"name": "...", "company": "...", "url": "...", "reason": "...", "email_guess": "..." or null}]
"""


def _expand_queries(target_description):
    text = _chat(QUERY_SYSTEM, target_description)
    queries = [line.strip("-* \t") for line in text.splitlines() if line.strip()]
    return queries[:MAX_QUERIES]


def _extract_candidates(target_description, results):
    if not results:
        return []
    formatted = "\n\n".join(
        f"URL: {r['url']}\nTitle: {r['title']}\nContent: {r['content'][:500]}"
        for r in results[:MAX_CANDIDATES_FED_TO_LLM]
    )
    user_prompt = f"Target description: {target_description}\n\nSearch results:\n\n{formatted}"
    text = _chat(EXTRACT_SYSTEM, user_prompt)

    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []


def find_leads(account, target_description, limit=10):
    queries = _expand_queries(target_description)

    all_results = []
    seen_urls = set()
    for query in queries:
        for r in search.tavily_search(query, max_results=MAX_RESULTS_PER_QUERY):
            if r["url"] not in seen_urls:
                seen_urls.add(r["url"])
                all_results.append(r)

    candidates = _extract_candidates(target_description, all_results)

    existing = sheets.get_all_rows(account)
    existing_emails = {row[sheets.COL_EMAIL].strip().lower() for _, row in existing if row[sheets.COL_EMAIL].strip()}
    existing_names_companies = {
        (row[sheets.COL_NAME].strip().lower(), row[sheets.COL_COMPANY].strip().lower())
        for _, row in existing
    }

    # First pass: filter down to the exact candidate set to verify (cheap,
    # no network calls) -- dedup must stay sequential so two candidates in
    # the same batch sharing a guessed email don't both survive.
    to_verify = []
    skipped_duplicates = 0
    for c in candidates:
        if len(to_verify) >= limit:
            break

        name = (c.get("name") or "").strip()
        company = (c.get("company") or "").strip()
        email_guess = (c.get("email_guess") or "").strip() or None
        if not name:
            continue

        if (email_guess and email_guess.lower() in existing_emails) or (
            (name.lower(), company.lower()) in existing_names_companies
        ):
            skipped_duplicates += 1
            continue

        if not email_guess:
            continue

        to_verify.append((name, email_guess, company, (c.get("reason") or "").strip()))
        existing_emails.add(email_guess.lower())

    # Second pass: the actual per-candidate network calls, run concurrently.
    with ThreadPoolExecutor(max_workers=MAX_VERIFY_WORKERS) as pool:
        verifications = list(pool.map(lambda t: email_verification.verify(t[1]), to_verify))

    new_rows = [
        [
            name, email_guess, company,
            "Ready for review" if verification == "verified" else "Needs verification",
            "", "", "",
            reason, verification,
        ]
        for (name, email_guess, company, reason), verification in zip(to_verify, verifications)
    ]

    sheets.append_rows(account, new_rows)

    return {
        "found": len(candidates),
        "added": len(new_rows),
        "skipped_duplicates": skipped_duplicates,
        "verified": sum(row[sheets.COL_EMAIL_CONFIDENCE] == "verified" for row in new_rows),
        "needs_verification": sum(row[sheets.COL_EMAIL_CONFIDENCE] != "verified" for row in new_rows),
    }
