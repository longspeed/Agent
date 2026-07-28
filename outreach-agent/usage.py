"""Per-account usage metering for pooled third-party keys (OpenRouter, Tavily).

The account whose request is being served is set once at the entry point
(server.py sets it per request/background job); agent.py and search.py then
record usage without every call site having to thread an account through."""
import contextvars

import accounts_db
from config import MODEL_PRICES, TAVILY_COST_PER_SEARCH_USD

# Cost buckets are named "cost:<purpose>" so accounts_db.usage_summary(), which
# groups by kind, rolls them up per purpose with no extra query. These are the
# three COGS lines the pricing model is built on: what one sourced lead, one
# drafted email, and one drafted reply actually cost us.
SOURCE_LEADS = "source_leads"
DRAFT_EMAIL = "draft_email"
DRAFT_REPLY = "draft_reply"

# Product units, the things a plan is actually sold in. Separate from the cost
# buckets above because those answer "what did this cost us" and these answer
# "how much of what they bought have they used", and the two genuinely differ:
#
#   - record_llm() only writes a "cost:<purpose>" row when the model has a price
#     in MODEL_PRICES. Every provider this runs on is a free tier with no
#     configured price, so those calls go to "cost:unpriced" and the purpose
#     bucket stays empty. A quota reading cost buckets would count zero forever.
#   - A cost row is one LLM call. A draft that needed a retry is two calls but
#     one draft, and charging a customer for our own retry is indefensible.
#
# So these are recorded once per delivered product, by the code that delivered
# it. Same table and same event stream as the cost meter -- plans.py reads them
# through accounts_db.usage_quantity_since().
UNIT_DRAFT_EMAIL = "unit:draft_email"
UNIT_DRAFT_REPLY = "unit:draft_reply"
UNIT_LEAD_SOURCED = "unit:lead_sourced"

_current_account_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_account_id", default=None
)


def set_account(account_id: str | None):
    _current_account_id.set(account_id)


def run_as(account_id: str | None, fn, *args, **kwargs):
    """Runs fn with usage attributed to account_id. Needed for worker threads
    (ThreadPoolExecutor, background jobs) which don't inherit the submitting
    thread's contextvars."""
    set_account(account_id)
    return fn(*args, **kwargs)


def record(kind: str, quantity: int = 1, detail: str = ""):
    """Best-effort: metering must never break the action being metered."""
    account_id = _current_account_id.get()
    if not account_id:
        return
    try:
        accounts_db.record_usage(account_id, kind, quantity, detail)
    except Exception as e:
        print(f"Usage metering failed ({kind}): {e}")


def record_llm(purpose, model, prompt_tokens, completion_tokens, total_tokens=None):
    """One LLM call: token volume under the existing "llm" kind (unchanged, the
    settings dashboard reads it), plus its dollar cost bucketed by purpose.

    prompt_tokens/completion_tokens are None when the provider reported only a
    total. Volume is still recorded, but the call is priced as unknown rather
    than guessed at -- output tokens cost several times what input does, so
    assuming a split would quietly skew every margin computed from this."""
    split_known = prompt_tokens is not None and completion_tokens is not None
    total = total_tokens if total_tokens is not None else (prompt_tokens or 0) + (completion_tokens or 0)
    breakdown = f"in={prompt_tokens} out={completion_tokens}" if split_known else f"total={total}"
    record("llm", total or 1, f"{model} {purpose} {breakdown}")

    price = MODEL_PRICES.get(model)
    if price is None or not split_known or not total:
        # No configured price, or no usable token counts to price. Both are
        # blind spots, not zero-cost calls.
        record("cost:unpriced", 0, f"{model} {purpose}")
        return
    # Rates are USD per 1M tokens, so tokens * rate is already micro-USD --
    # which is what we store, because usage_events.quantity is an integer and
    # dollars-as-float would truncate every realistic call to 0.
    input_rate, output_rate = price
    micros = round(prompt_tokens * input_rate + completion_tokens * output_rate)
    record(f"cost:{purpose}", micros, model)


def record_unit(unit: str, quantity: int = 1, detail: str = ""):
    """One delivered product unit, counted against the account's plan.

    Deliberately NOT best-effort in the way record() is. record() swallows
    failures because metering must never break the action being metered, and for
    a cost bucket that is right -- a lost cost row costs us a rounding error in a
    margin report. A lost unit row is different: it is quota the customer
    consumed and will never be charged for, and the failure mode compounds
    silently (every subsequent check reads a low number, so the account drifts
    permanently over its allowance). It still cannot be allowed to destroy work
    that already happened, so the write is retried once and then reported loudly
    rather than raised -- the draft is already queued by this point, and throwing
    would lose it to keep a counter tidy."""
    account_id = _current_account_id.get()
    if not account_id or quantity <= 0:
        return
    for attempt in (1, 2):
        try:
            accounts_db.record_usage(account_id, unit, quantity, detail)
            return
        except Exception as e:
            if attempt == 2:
                print(
                    f"QUOTA UNDERCOUNT: failed to record {quantity} x {unit} for "
                    f"account {account_id} ({e}). This account's usage is now "
                    "understated and it will be allowed past its plan limit."
                )


def record_search(query: str):
    """One Tavily search, charged to the lead-sourcing bucket."""
    record("search", 1, query)
    if TAVILY_COST_PER_SEARCH_USD <= 0:
        record("cost:unpriced", 0, "tavily search")
        return
    record(f"cost:{SOURCE_LEADS}", round(TAVILY_COST_PER_SEARCH_USD * 1_000_000), "tavily search")
