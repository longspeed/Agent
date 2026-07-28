"""Which LLM endpoint is allowed to draft which kind of text, and in what order
to try them.

Two things set the order. Cost, because every provider in the chain has a free
tier large enough to run this product, so free goes first. And privacy, which is
the constraint that actually matters here:

A first-touch draft's prompt contains a name, sometimes a company, one publicly
sourced research note, and the sender's own pitch. Nothing in it belongs to the
prospect. A reply draft's prompt contains the prospect's own words -- what they
asked, what they objected to, what they told us about their company. Free tiers
generally reserve the right to use prompts and completions for model training.
Those two facts do not combine: a first-touch draft can go to a free endpoint,
a reply draft cannot, while any paid endpoint is configured.

That is why chain_for() takes a purpose. It is the whole reason this module
exists as something other than a list.

Every provider speaks the OpenAI /chat/completions shape (see the table in
config.py), so agent.py keeps one request/response code path and this module
only decides ordering and eligibility -- no HTTP.
"""

from dataclasses import dataclass

import usage
from config import LLM_PROVIDERS, REPLY_DRAFTS_REQUIRE_PRIVATE_ENDPOINT

# Purposes whose prompt carries text the prospect wrote themselves. These are
# barred from free-tier endpoints whenever a paid one is available.
PRIVATE_PURPOSES = frozenset({usage.DRAFT_REPLY})


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    url: str
    api_key: str
    model: str
    free_tier: bool

    @property
    def endpoint(self) -> str:
        return f"{self.url}/chat/completions"

    @property
    def display(self) -> str:
        """What shows up in logs. The tier is included because "which endpoint
        wrote this" and "was that endpoint allowed to see it" are the same
        question when reading back a log."""
        return f"{self.label} ({self.model}, {'free' if self.free_tier else 'paid'} tier)"


def _enabled(row: dict) -> bool:
    """A provider is live once the field that activates it is set: a base URL for
    the local proxy (whose key is optional, it may not require auth), an API key
    for the hosted ones."""
    return bool(row["url"] if row["enabled_when"] == "url" else row["api_key"])


def available() -> tuple[Provider, ...]:
    """Every configured provider, in preference order."""
    return tuple(
        Provider(
            name=row["name"], label=row["label"], url=row["url"],
            api_key=row["api_key"], model=row["model"], free_tier=row["free_tier"],
        )
        for row in LLM_PROVIDERS
        if _enabled(row)
    )


_warned_free_tier_replies = False


def _warn_free_tier_replies(provider: Provider):
    global _warned_free_tier_replies
    if _warned_free_tier_replies:
        return
    _warned_free_tier_replies = True
    print(
        f"WARNING: drafting replies on {provider.display}. A free tier may use "
        "prompts for model training, and a reply prompt contains the prospect's "
        "own words. privacy.html must disclose this. Configure a paid provider, "
        "or set REPLY_DRAFTS_REQUIRE_PRIVATE_ENDPOINT=1 to refuse instead."
    )


def chain_for(purpose: str) -> tuple[Provider, ...]:
    """Providers to try, in order, for one kind of generation.

    Raises RuntimeError when nothing is eligible, because that is a
    configuration error and the message is the fix for it."""
    provs = available()
    if not provs:
        raise RuntimeError(
            "No LLM provider is configured. Set GROQ_API_KEY (free, no card, "
            "highest daily limit), GEMINI_API_KEY, or OPENROUTER_API_KEY."
        )

    if purpose not in PRIVATE_PURPOSES:
        return provs

    private = tuple(p for p in provs if not p.free_tier)
    if private:
        # Deliberately not falling back to free-tier providers when these are
        # down. A failed reply draft is recoverable -- the operator writes the
        # reply themselves. A prospect's words in someone's training corpus is
        # not recoverable by anyone.
        return private

    if REPLY_DRAFTS_REQUIRE_PRIVATE_ENDPOINT:
        raise RuntimeError(
            "Reply drafting needs a provider that is not on a free tier "
            "(REPLY_DRAFTS_REQUIRE_PRIVATE_ENDPOINT is on). Configure a paid "
            "endpoint, e.g. GEMINI_API_KEY with GEMINI_TIER=paid."
        )
    _warn_free_tier_replies(provs[0])
    return provs


def describe() -> list[dict]:
    """Diagnostic summary: what is configured, and what each one may draft.

    drafts_replies is read back off chain_for rather than re-derived, so this
    can never drift from the policy it is reporting on."""
    try:
        replies = {p.name for p in chain_for(usage.DRAFT_REPLY)}
    except RuntimeError:
        replies = set()
    return [
        {
            "name": p.name,
            "model": p.model,
            "tier": "free" if p.free_tier else "paid",
            "drafts_replies": p.name in replies,
        }
        for p in available()
    ]
