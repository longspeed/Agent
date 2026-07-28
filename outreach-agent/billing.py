"""Stripe Checkout and the webhook that grants the plan.

Two calls to Stripe's REST API over `requests`, plus HMAC signature
verification. No SDK: the surface used here is one POST and one signature
check, `requests` is already this project's HTTP client (agent.py, search.py,
dns_check.py), and a dependency that has to be installed before anyone can take
money is a dependency that will be missing in production exactly once.

THE TRUST BOUNDARY
The webhook is the only place a plan is granted, and it is a public,
unauthenticated URL -- anyone can POST to it. Two rules follow, and both are
load-bearing:

  1. Nothing is processed without a valid signature. An unsigned or badly
     signed event is refused, and an unset webhook secret refuses everything,
     because "no secret configured" must never degrade into "trust the caller".
     Without this, a stranger who guesses the URL upgrades themselves for free.

  2. The account being upgraded comes from client_reference_id, which *we* set
     when creating the Checkout session against an already-authenticated
     account. It is never read from customer-controlled fields on the event.
     Stripe will happily echo back an email a customer typed into the payment
     form; trusting it would let anyone pay $49 and have someone else's account
     upgraded, or their own upgraded on a stranger's card.

WHAT IT DOES NOT DO
Track subscription state beyond the plan column. There is no local mirror of
Stripe's subscription object, no proration handling, and no dunning. The plan
column is set on checkout completion and cleared back to trial on cancellation
or on a subscription that lapses; anything more detailed is a question to ask
Stripe, which is authoritative anyway.
"""
import hashlib
import hmac
import json
import time

import requests

import plans
from config import (
    PUBLIC_BASE_URL,
    STRIPE_SECRET_KEY,
    STRIPE_WEBHOOK_SECRET,
)

API = "https://api.stripe.com/v1"
TIMEOUT = 20

# How far a webhook's own timestamp may be from our clock. Stripe's documented
# default. Bounded so a signed event captured off the wire cannot be replayed
# indefinitely -- the signature stays valid forever, the timestamp does not.
SIGNATURE_TOLERANCE_SECONDS = 300


class BillingNotConfigured(Exception):
    """No Stripe key on this deployment. Checkout is unavailable, which is a
    normal state for a self-hosted install, not an error to alarm anyone."""


class WebhookRejected(Exception):
    """The event did not come from Stripe, or did not come recently."""


def configured() -> bool:
    return bool(STRIPE_SECRET_KEY)


def price_id_for(plan_name: str, yearly: bool = False) -> str:
    """The configured Stripe price for a tier, or "" if that tier is not sellable
    on this deployment."""
    import config

    plan = plans.PLANS.get(plan_name)
    if not plan or not plan.stripe_price_env:
        return ""
    env_name = plan.stripe_price_env + ("_YEARLY" if yearly else "")
    return getattr(config, env_name, "") or ""


def _post(path: str, form: list[tuple[str, str]]) -> dict:
    """Stripe's API takes form-encoded bodies with bracketed nested keys, which
    is why this passes a list of pairs rather than a dict -- repeated keys and
    ordering both matter to it."""
    response = requests.post(
        f"{API}/{path}",
        data=form,
        auth=(STRIPE_SECRET_KEY, ""),
        timeout=TIMEOUT,
    )
    if response.status_code >= 400:
        # Stripe puts the actionable part in error.message ("No such price").
        # Surfacing that beats a bare 402 when someone has mistyped a price id.
        try:
            message = response.json()["error"]["message"]
        except Exception:
            message = response.text[:300]
        raise RuntimeError(f"Stripe {path} failed ({response.status_code}): {message}")
    return response.json()


def create_checkout_session(account: dict, plan_name: str, yearly: bool = False) -> str:
    """Returns the URL to redirect the customer to.

    client_reference_id carries our account id through Stripe and back on the
    webhook. It is the whole mechanism by which a payment is attached to an
    account, and it is set here -- from the authenticated session -- precisely
    so it can never be supplied by whoever is paying."""
    if not configured():
        raise BillingNotConfigured(
            "Stripe is not configured on this deployment (STRIPE_SECRET_KEY is unset)."
        )
    price = price_id_for(plan_name, yearly)
    if not price:
        raise BillingNotConfigured(
            f"No Stripe price is configured for the {plan_name} plan. Set "
            f"{plans.PLANS[plan_name].stripe_price_env}{'_YEARLY' if yearly else ''}."
        )

    session = _post("checkout/sessions", [
        ("mode", "subscription"),
        ("line_items[0][price]", price),
        ("line_items[0][quantity]", "1"),
        ("client_reference_id", str(account["id"])),
        # Repeated on the subscription so a later cancellation event can still
        # be traced back to an account: cancellations arrive as a subscription
        # object, which carries no client_reference_id of its own.
        ("subscription_data[metadata][account_id]", str(account["id"])),
        ("subscription_data[metadata][plan]", plan_name),
        ("metadata[account_id]", str(account["id"])),
        ("metadata[plan]", plan_name),
        ("customer_email", account.get("email") or ""),
        ("success_url", f"{PUBLIC_BASE_URL}/settings?checkout=success"),
        ("cancel_url", f"{PUBLIC_BASE_URL}/settings?checkout=cancelled"),
    ])
    return session["url"]


def verify_webhook(payload: bytes, signature_header: str | None, now: float | None = None) -> dict:
    """Parses a Stripe webhook after checking its signature. Raises
    WebhookRejected on anything it cannot prove came from Stripe.

    The signed string is "{timestamp}.{raw body}" -- the RAW body, which is why
    the caller must hand over the exact bytes received and not a re-serialized
    dict. Re-encoding JSON reorders keys and changes whitespace, and the
    signature is over bytes, so a round-tripped body fails to verify even when
    it is authentic."""
    if not STRIPE_WEBHOOK_SECRET:
        # Refusing rather than trusting. An endpoint that grants paid plans on
        # unverified input is worse than an endpoint that is switched off.
        raise WebhookRejected(
            "STRIPE_WEBHOOK_SECRET is not set, so webhook events cannot be verified."
        )
    if not signature_header:
        raise WebhookRejected("Missing Stripe-Signature header.")

    parts = dict(
        piece.split("=", 1) for piece in signature_header.split(",")
        if "=" in piece
    )
    timestamp = parts.get("t", "")
    if not timestamp.isdigit():
        raise WebhookRejected("Malformed Stripe-Signature timestamp.")

    now = time.time() if now is None else now
    if abs(now - int(timestamp)) > SIGNATURE_TOLERANCE_SECONDS:
        raise WebhookRejected("Stripe-Signature timestamp is outside the tolerance window.")

    expected = hmac.new(
        STRIPE_WEBHOOK_SECRET.encode(),
        f"{timestamp}.".encode() + payload,
        hashlib.sha256,
    ).hexdigest()
    # v1 may appear more than once during a secret rotation, so every candidate
    # is checked. compare_digest, not ==, because a byte-at-a-time comparison
    # leaks how much of a forged signature was correct.
    candidates = [
        value for piece in signature_header.split(",")
        if piece.startswith("v1=") for value in [piece[3:]]
    ]
    if not any(hmac.compare_digest(expected, candidate) for candidate in candidates):
        raise WebhookRejected("Stripe-Signature did not match.")

    try:
        return json.loads(payload.decode())
    except (ValueError, UnicodeDecodeError) as e:
        raise WebhookRejected(f"Webhook body was not valid JSON: {e}") from e


def plan_change_from_event(event: dict) -> tuple[str, str] | None:
    """Maps a verified Stripe event to (account_id, plan_name), or None when the
    event is not one that changes a plan.

    Unknown event types return None rather than raising: Stripe sends whatever
    the endpoint is subscribed to, that list changes in a dashboard nobody will
    remember to keep in sync with this file, and an unrecognized event is not an
    error. Returning None lets the endpoint answer 200 so Stripe stops retrying
    something we are deliberately ignoring."""
    kind = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    if kind == "checkout.session.completed":
        # Only a paid session grants anything. An unpaid session reaching here
        # means the customer got to the confirmation page without the payment
        # settling, which is exactly the case that must not be upgraded.
        if obj.get("payment_status") not in ("paid", "no_payment_required"):
            return None
        account_id = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("account_id")
        plan_name = (obj.get("metadata") or {}).get("plan", "")
        if account_id and plan_name in plans.PLANS:
            return str(account_id), plan_name
        return None

    if kind in ("customer.subscription.deleted", "customer.subscription.paused"):
        account_id = (obj.get("metadata") or {}).get("account_id")
        # Back to the free tier, not to nothing: the account keeps working at
        # trial quotas rather than being locked out of data it still owns.
        if account_id:
            return str(account_id), plans.DEFAULT_PLAN
        return None

    return None
