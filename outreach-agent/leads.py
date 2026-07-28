import json

from agent import _chat
import plans
import search
import sheets
import usage

MAX_QUERIES = 3
MAX_RESULTS_PER_QUERY = 5
MAX_CANDIDATES_FED_TO_LLM = 15

QUERY_SYSTEM = """You turn a description of an ideal customer/lead into concrete web search
queries that surface NAMED INDIVIDUALS matching that description -- not just companies. A
lead with no person's name is useless downstream, since there's no one to email. Prefer
queries likely to surface a name: "founder", "CEO", team/about pages, interviews, press
quotes, LinkedIn-style bios, conference speaker lists -- over queries that just return
company directories or "best X companies" listicles.
Output exactly 3 short search queries, one per line, nothing else -- no numbering,
no explanation."""

EXTRACT_SYSTEM = """You are a lead researcher. Given a target description and a batch of web
search results, extract real, named individuals who plausibly match the target.

Rules:
- Only extract a candidate if you can name a specific person (first and last name found in the
  content, e.g. "the site names its founder as Jane Doe"). If a result only names a company with
  no individual mentioned, skip it entirely -- do not invent a name and do not extract the
  company on its own. A lead search is only useful if there's a person to contact.
- Skip generic listicles, directories, review sites, and anything that isn't a specific real
  company or person.
- For each match, infer a likely contact email ONLY if you can reasonably derive the company's
  domain from the URL (e.g. first.last@domain, or an address literally present in the content).
  If you can't derive a plausible domain, set email_guess to null -- do not invent a domain.
- reason is one plain sentence: why this specific person fits the target description.

Output ONLY a JSON array, nothing else, in this exact shape:
[{"name": "...", "company": "...", "url": "...", "reason": "...", "email_guess": "..." or null}]
"""


def _expand_queries(target_description):
    text = _chat(QUERY_SYSTEM, target_description, usage.SOURCE_LEADS)
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
    text = _chat(EXTRACT_SYSTEM, user_prompt, usage.SOURCE_LEADS)

    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []


def find_leads(account, target_description, limit=10):
    # Before the search, not after. Everything below this line spends metered
    # third-party calls -- three LLM query expansions, up to fifteen Tavily
    # searches, one more LLM extraction -- and an account with no allowance left
    # must not be able to burn our quota on work it is not entitled to keep.
    # This is also why the gate is here rather than on the API endpoint: the
    # lead agent runs from the CLI too, which never touches FastAPI.
    plans.check(account, usage.UNIT_LEAD_SOURCED)
    _, allowance, _ = plans.headroom(account, usage.UNIT_LEAD_SOURCED)
    if allowance is not None:
        # Take what is left rather than refusing a batch that only partly fits.
        limit = min(limit, allowance)

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

    # Dedup must stay sequential so two candidates in the same batch sharing
    # a guessed email don't both survive. Every surviving candidate lands as
    # "unverified" -- there's no automated verification anymore; a sheet
    # owner marks the EmailConfidence column "verified" by hand once they've
    # confirmed the address, which is what makes a contact eligible to send.
    to_add = []
    skipped_duplicates = 0
    for c in candidates:
        if len(to_add) >= limit:
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

        to_add.append((name, email_guess, company, (c.get("reason") or "").strip()))
        existing_emails.add(email_guess.lower())

    new_rows = [
        [name, email_guess, company, "Needs verification", "", "", "", reason, "unverified"]
        for name, email_guess, company, reason in to_add
    ]

    sheets.append_rows(account, new_rows)

    # Counted after the rows are in the sheet, so a failed append is not charged
    # against the customer's allowance -- they got nothing, they owe nothing.
    # One event carrying the whole batch (quantity=N), which is why
    # usage_quantity_since sums quantity instead of counting rows.
    usage.record_unit(usage.UNIT_LEAD_SOURCED, len(new_rows), target_description[:120])

    return {
        "found": len(candidates),
        "added": len(new_rows),
        "skipped_duplicates": skipped_duplicates,
    }
