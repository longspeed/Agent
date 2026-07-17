"""Fail-closed email verification for outreach recipients.

The app uses NeverBounce's single-address endpoint when configured. A source
mention or a pattern-derived address is not treated as verification: only a
provider result of ``valid`` can enter a sendable campaign.
"""
import requests

from config import NEVERBOUNCE_API_KEY
import usage

API_URL = "https://api.neverbounce.com/v4/single/check"


def is_configured() -> bool:
    return bool(NEVERBOUNCE_API_KEY)


def verify(email: str) -> str:
    """Return ``verified``, ``invalid``, or ``unverified``.

    Network/provider errors deliberately return ``unverified``. It is safer to
    hold a lead for review than to convert a transient API failure into a send.
    """
    if not email or not is_configured():
        return "unverified"
    try:
        response = requests.get(
            API_URL,
            params={"key": NEVERBOUNCE_API_KEY, "email": email},
            timeout=20,
        )
        response.raise_for_status()
        usage.record("email_verification", 1, email)
        payload = response.json()
        if "result" not in payload:
            # NeverBounce returns 200 with no "result" field on auth failure
            # or credit exhaustion, not just on a real invalid-address verdict.
            # Treat that as retryable, not a permanent invalid classification.
            print(
                f"email_verification: no 'result' in NeverBounce response "
                f"(status={payload.get('status')!r}, message={payload.get('message')!r}) "
                f"-- likely auth failure or credit exhaustion, not an invalid address"
            )
            return "unverified"
        result = (payload.get("result") or "").lower()
        return "verified" if result == "valid" else "invalid"
    except requests.RequestException as e:
        print(f"email_verification: NeverBounce request failed: {e}")
        return "unverified"
