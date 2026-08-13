import json
import re
from urllib.parse import urlparse

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
quotes, speaker bios, conference speaker lists -- over queries that just return
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
- A candidate is only valid if the SAME result ties the person to the company -- the content
  must state or clearly imply that the named person works for / founded / runs that company
  (e.g. "Jane Doe, CEO of Acme", "Acme's founder, Jane Doe"). Never attach a person to a
  company just because their name and a company name both happen to appear in the search batch.
  A real person matched to a company they have nothing to do with is a hallucinated lead.
- Skip generic listicles, directories, review sites, and anything that isn't a specific real
  company or person.
- For each match, set email_guess ONLY when you can derive the domain from a URL in the results
  that actually belongs to that company (e.g. a result whose URL host is the company's own
  site, then first.last@host), or when a real address is literally present in the content.
  Never guess email_guess for well-known companies from brand knowledge alone -- "warp.com" is
  not Warp's domain (it's warp.dev), "port.com" is not Port's (it's port.io / getport.io), and
  a guessed first.last@domain on a plausible-looking but wrong domain is a fabricated address.
  If you cannot derive a domain that the company genuinely owns, set email_guess to null -- do
  not invent a domain.
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
        candidates = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    return _guard_candidates(candidates, results)


def _registered_domain(host):
    host = (host or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return host


def _contains_words(text, words):
    """Whether all words occur as whole tokens, in order, in text.

    The extractor's evidence must survive a deterministic check. Plain
    substring matching made short names ("An", "May") match arbitrary prose
    and treated "AI" as present inside words such as "annual". Token matching
    keeps the guard conservative without requiring the LLM's punctuation or
    middle initials to match byte-for-byte.
    """
    if not words:
        return False
    pattern = r"\b" + r"\W+".join(re.escape(word) for word in words) + r"\b"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _guard_candidates(candidates, results):
    """Deterministic backstop to the extractor's two known failure modes.

    The prompt tells the LLM not to attach a person to a company the content doesn't
    tie them to, and not to guess email_guess on well-known brands. This re-checks
    both in code because each has shipped before: a real person matched to an
    unrelated site (Simon Willison attached to a blog that belongs to someone else),
    and first.last@domain guesses on wrong-but-plausible domains (warp.com for Warp,
    whose domain is warp.dev; port.com for Port, whose domain is port.io).

    A candidate survives only if the same result mentions both the person and their
    company. A candidate keeps email_guess only if the email's domain matches the
    registered domain of some result that ties person to company, or the exact
    address appears literally in a result. Anything else is dropped or nulled --
    unverified is fine to store, fabricated is not.
    """
    def _text(r):
        return ((r.get("title") or "") + " " + (r.get("content") or "")).lower()

    guarded = []
    for c in candidates:
        name = (c.get("name") or "").strip()
        company = (c.get("company") or "").strip()
        name_words = re.findall(r"[\w']+", name)
        company_words = re.findall(r"[\w']+", company)
        if len(name_words) < 2 or not company_words:
            continue
        # A middle initial often appears in one source but not another, so use
        # the first and last name as stable identity tokens. Company words must
        # still occur in sequence in that same result.
        person_words = [name_words[0], name_words[-1]]
        tied = [
            r for r in results
            if _contains_words(_text(r), person_words)
            and _contains_words(_text(r), company_words)
        ]
        if not tied:
            continue

        email = (c.get("email_guess") or "").strip()
        if email:
            domain = email.split("@")[-1].lower()
            hosts = {_registered_domain(urlparse(r.get("url") or "").netloc) for r in tied}
            if not (any(email.lower() in _text(r) for r in results) or domain in hosts):
                c["email_guess"] = None
        guarded.append(c)
    return guarded


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
    # This is now a hard gate, not a label: sheets.campaign_readiness never
    # returns a row stamped "unverified", and the send paths in server.py /
    # send_outreach.py re-check the cell at send time.
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
