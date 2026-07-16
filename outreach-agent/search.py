import requests

import usage
from config import TAVILY_API_KEY

API_URL = "https://api.tavily.com/search"


def tavily_search(query, max_results=5):
    """Returns [{title, url, content}] or [] if the query fails."""
    if not TAVILY_API_KEY:
        raise RuntimeError("TAVILY_API_KEY is not set — add it to outreach-agent/.env")

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
    usage.record("search", 1, query)
    results = response.json().get("results", [])
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")} for r in results]
