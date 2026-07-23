import requests

import usage
from config import TAVILY_API_KEY

API_URL = "https://api.tavily.com/search"


def tavily_search(query, max_results=5):
    """Returns [{title, url, content}] for the query.

    Raises RuntimeError if TAVILY_API_KEY is unset — a config error worth
    surfacing loudly (and the app's convention for user-safe messages).
    Returns [] if the search request itself fails (network error, Tavily
    5xx/429, malformed JSON) so one flaky query doesn't abort a multi-query
    lead search — the caller runs several and aggregates. Usage is metered
    only on a successful call."""
    if not TAVILY_API_KEY:
        raise RuntimeError("TAVILY_API_KEY is not set — add it to outreach-agent/.env")

    try:
        response = requests.post(
            API_URL,
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "max_results": max_results,
                "include_answer": False,
            },
            timeout=30,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
    except (requests.RequestException, ValueError) as e:
        print(f"Tavily search failed for {query!r}: {e}")
        return []

    usage.record("search", 1, query)
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")} for r in results]
