"""What each plan entitles an account to, and the gate that enforces it.

Three tiers, and the numbers here are not free-floating: every one of them is a
promise already made on the pricing page (site/src/components/pricing.tsx). That
file is the customer-facing contract and this one is its implementation, so they
have to be read together. Where the page states a number ("50 sourced leads",
"25 approved sends per day", "500 sourced leads/mo") this module uses exactly
that number. Where the page says "Unlimited reply drafts", the quota here is
None -- deliberately, because a cap we enforce against a word the site prints is
the same class of untrue claim the verification copy already had to be fixed for.

WHAT GETS COUNTED, AND WHY IT ISN'T THE COST BUCKETS
usage.py already meters every LLM call into "cost:<purpose>" buckets, and the
obvious move is to read quotas off those. It does not work, for a reason worth
recording: record_llm() only emits "cost:<purpose>" when the model has a
configured price in MODEL_PRICES. Every provider this product actually runs on
is a free tier with no configured price, so those calls land in "cost:unpriced"
and the purpose bucket is never written. A quota counting them would read zero
forever and never refuse anybody.

The deeper mismatch is that cost buckets count LLM *calls* priced in micro-USD,
and a plan sells *products* -- a queued draft, a sourced lead. A draft that took
two attempts is two calls but one draft, and billing someone twice for our own
retry is indefensible. So the quota reads dedicated unit events ("unit:*"),
recorded once per delivered product by usage.record_unit(). Same table, same
module, same event stream as the cost meter -- one counter, asked a second
question -- and the cost buckets keep meaning money, undisturbed.

WHERE THE GATE LIVES
On the operation (send_outreach.prepare_drafts, leads.find_leads), not on the
HTTP endpoint, because the CLI drives both modules directly and bypasses the API
entirely. This is the same placement bounces.assert_sendable already uses, for
the same reason.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

import usage

# The plan an account has when the column is missing, empty, or holds something
# this code does not recognize. Trial rather than Pilot on purpose: an
# unreadable plan must fail toward the *least* entitlement, or a typo in the
# column becomes free unlimited service. The same reasoning applies to a row
# written by a future version of this code with a tier this one has not heard of.
DEFAULT_PLAN = "trial"


class QuotaExceeded(Exception):
    """This account has used up an allowance its plan grants for the period.

    Carries the plan and the bucket so the caller can render an upgrade path
    instead of a bare failure -- a quota refusal is a sales moment, and a 402
    that does not say what ran out or what to do about it just reads as broken.
    """

    def __init__(self, plan_name: str, bucket: str, used: int, limit: int, message: str):
        super().__init__(message)
        self.plan_name = plan_name
        self.bucket = bucket
        self.used = used
        self.limit = limit


@dataclass(frozen=True)
class Plan:
    name: str
    label: str
    price_monthly_usd: int
    price_yearly_usd: int
    # Emails this account may send in a rolling 24 hours. Per-plan rather than
    # the single global DAILY_SEND_LIMIT it replaces, but note the values are
    # equal for trial and pilot: the pricing page promises "25 approved sends
    # per day" on the free trial, so raising pilot above it would be an upsell
    # the page does not offer. Team is higher because its monthly draft
    # allowance is otherwise unreachable -- 3000 drafts against a 25/day cap is
    # 750 sends a month, and selling an allowance the daily cap forbids
    # spending is a broken promise on the more expensive plan.
    daily_send_limit: int
    # Drafts queued for review per calendar month. None means uncapped.
    monthly_drafts: int | None
    # Leads the sourcing agent may return. None means uncapped.
    lead_allowance: int | None
    # "month" resets on the 1st; "lifetime" never resets. The trial's 50 leads
    # are a lifetime figure because the pricing page writes them without a
    # "/mo" suffix, unlike Pilot's "500 sourced leads/mo" -- a free tier that
    # silently refills every month is a free tier forever.
    lead_window: str
    # Reply drafts are uncapped on every plan: the page sells "Unlimited reply
    # drafts" on Pilot, and neither Trial nor Team contradicts it.
    monthly_replies: int | None
    lead_searches_per_hour: int
    # Which env var holds this tier's Stripe price id. Free plans have none --
    # there is nothing to check out.
    stripe_price_env: str | None


PLANS: dict[str, Plan] = {
    "trial": Plan(
        name="trial",
        label="Trial",
        price_monthly_usd=0,
        price_yearly_usd=0,
        daily_send_limit=25,
        monthly_drafts=100,
        lead_allowance=50,
        lead_window="lifetime",
        monthly_replies=None,
        lead_searches_per_hour=10,
        stripe_price_env=None,
    ),
    "pilot": Plan(
        name="pilot",
        label="Pilot",
        price_monthly_usd=19,
        price_yearly_usd=190,
        daily_send_limit=25,
        monthly_drafts=750,
        lead_allowance=500,
        lead_window="month",
        monthly_replies=None,
        lead_searches_per_hour=10,
        stripe_price_env="STRIPE_PRICE_PILOT",
    ),
    "team": Plan(
        name="team",
        label="Team",
        price_monthly_usd=149,
        price_yearly_usd=1490,
        daily_send_limit=100,
        monthly_drafts=3000,
        lead_allowance=2000,
        lead_window="month",
        monthly_replies=None,
        lead_searches_per_hour=30,
        stripe_price_env="STRIPE_PRICE_TEAM",
    ),
}

PAID_PLANS = tuple(name for name, plan in PLANS.items() if plan.price_monthly_usd > 0)


def get(name: str | None) -> Plan:
    """The Plan for a name, falling back to the trial for anything unrecognized.

    Never raises. A bad value in the column must not take the product down --
    it degrades to the smallest entitlement, which is both safe and visible
    (the customer complains, which is how we find out)."""
    return PLANS.get((name or "").strip().lower(), PLANS[DEFAULT_PLAN])


def for_account(account: dict) -> Plan:
    """The Plan for an account row. Tolerates the column not existing yet, so a
    database that has not run the migration behaves as all-trial rather than
    raising on every request."""
    return get((account or {}).get("plan"))


def daily_send_limit_for(account: dict) -> int:
    """The rolling-24h send cap for this account.

    Pure -- it reads the plan off the account row already in hand and makes no
    database call, which is what lets sheets.py use it without acquiring the
    database dependency that module is deliberately built without.

    A deployment-wide DAILY_SEND_LIMIT still wins when it is explicitly set, for
    self-hosted single-tenant installs that have no plans at all."""
    from config import DAILY_SEND_LIMIT_OVERRIDE

    if DAILY_SEND_LIMIT_OVERRIDE is not None:
        return DAILY_SEND_LIMIT_OVERRIDE
    return for_account(account).daily_send_limit


def month_start_iso() -> str:
    """First instant of the current UTC calendar month.

    UTC, not the customer's timezone: the reset boundary has to be the same one
    the stored created_at timestamps use, or a quota can be walked backwards by
    changing timezone."""
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()


def _window_start(window: str) -> str | None:
    return month_start_iso() if window == "month" else None


def headroom(account: dict, bucket: str) -> tuple[Plan, int | None, int]:
    """How much of `bucket` this account has left: (plan, remaining, used).

    remaining is None when the plan does not cap this bucket. Otherwise it is
    clamped at zero, so a plan downgrade that leaves an account already over its
    new limit reads as "none left" rather than a negative allowance that a
    caller might slice a list with -- pending[:-3] silently drops work.

    One query, returning enough for both callers: the gate that refuses outright
    and the batch that trims itself to what is left.

    accounts_db is imported inside the function because it constructs a Supabase
    client at import time; keeping it here leaves the plan rules importable, and
    testable, without a database."""
    import accounts_db

    plan = for_account(account)
    limit, window = _limit_for(plan, bucket)
    if limit is None:
        return plan, None, 0
    used = accounts_db.usage_quantity_since(
        account["id"], bucket, since_iso=_window_start(window)
    )
    return plan, max(0, limit - used), used


def check(account: dict, bucket: str, requesting: int = 1) -> None:
    """Raise QuotaExceeded if `requesting` more units of `bucket` would exceed
    this account's plan. Call it before doing the work, not after."""
    plan, remaining, used = headroom(account, bucket)
    if remaining is None or requesting <= remaining:
        return
    limit, window = _limit_for(plan, bucket)
    raise QuotaExceeded(
        plan.name, bucket, used, limit,
        _refusal_message(plan, bucket, used, limit, window),
    )


def _limit_for(plan: Plan, bucket: str) -> tuple[int | None, str]:
    if bucket == usage.UNIT_DRAFT_EMAIL:
        return plan.monthly_drafts, "month"
    if bucket == usage.UNIT_LEAD_SOURCED:
        return plan.lead_allowance, plan.lead_window
    if bucket == usage.UNIT_DRAFT_REPLY:
        return plan.monthly_replies, "month"
    # An unknown bucket is not silently unlimited -- but it is also not a reason
    # to break the operation, so it is uncapped and named, which shows up in a
    # grep the first time someone adds a bucket and forgets to price it.
    return None, "month"


_BUCKET_NOUNS = {
    "unit:draft_email": ("outreach drafts", "this month"),
    "unit:lead_sourced": ("sourced leads", ""),
    "unit:draft_reply": ("reply drafts", "this month"),
}


def _refusal_message(plan: Plan, bucket: str, used: int, limit: int, window: str) -> str:
    noun, _ = _BUCKET_NOUNS.get(bucket, ("units", ""))
    period = "so far" if window == "lifetime" else "this month"
    upgrade = {
        "trial": " Upgrade to Pilot for 750 drafts and 500 sourced leads a month.",
        "pilot": " Upgrade to Team for 3,000 drafts and 2,000 sourced leads a month.",
        "team": " Email support@sendkeep.app if you need a higher limit.",
    }.get(plan.name, "")
    return (
        f"Your {plan.label} plan includes {limit:,} {noun} {period}, and you have "
        f"used {used:,}.{upgrade}"
    )


def describe(account: dict) -> dict:
    """The plan as the API reports it. Shape kept close to what /api/plan
    already returned so the existing settings page keeps rendering."""
    plan = for_account(account)
    return {
        "plan": plan.name,
        "name": plan.label,
        "price_monthly_usd": plan.price_monthly_usd,
        "price_yearly_usd": plan.price_yearly_usd,
        "daily_send_limit": plan.daily_send_limit,
        "lead_searches_per_hour": plan.lead_searches_per_hour,
        "monthly_drafts": plan.monthly_drafts,
        "lead_allowance": plan.lead_allowance,
        "lead_window": plan.lead_window,
        "monthly_replies": plan.monthly_replies,
        # Every clause here has to be true of the code. It used to end with
        # "Checkout is coming next", which stopped being true the moment
        # billing.py landed, and there is a test asserting this string never
        # claims addresses are verified -- because they are not.
        "note": (
            "Human-approved leads, human-reviewed emails and replies, one-click "
            "opt-out, and a daily send cap set by your plan."
        ),
    }
