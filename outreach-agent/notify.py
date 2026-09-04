"""Privacy-safe transactional alert delivery from the durable outbox."""
from datetime import datetime, timedelta, timezone
import os

import requests

import config
import notifications_db


def _message(event):
    event_type = event["event_type"]
    source_id = event["source_id"]
    if event_type == "reply_unhandled":
        return ("A Gmail reply needs your attention",
                "A reply is still waiting for review in Sendkeep.",
                f"/outreach?tab=inbox&review={source_id}")
    if event_type == "promise_due":
        return ("A confirmed promise is due",
                "A confirmed promise is due for review in Sendkeep.",
                f"/outreach?tab=promises&commitment={source_id}")
    if event_type == "send_uncertain":
        return ("Check a Gmail send result",
                "Sendkeep could not verify a send result. Check Gmail before taking another action.",
                f"/outreach?tab=inbox&review={source_id}")
    if event_type == "follow_up_send_uncertain":
        return ("Check a Gmail follow-up result",
                "Sendkeep could not verify a follow-up result. Check Gmail before taking another action.",
                f"/outreach?tab=follow-ups&review={source_id}")
    if event_type == "gmail_disconnected":
        return ("Sendkeep needs your attention",
                "Gmail monitoring has remained unavailable. Reconnect it to resume checks.",
                "/settings")
    if event_type == "gmail_recovered":
        return ("Sendkeep is checking Gmail again",
                "Gmail monitoring has recovered.", "/outreach?tab=inbox")
    return ("Your Sendkeep test alert worked",
            "Transactional alerts are configured for your signed-in account.",
            "/settings")


def _retry_at(attempt_count):
    minutes = min(60, max(2, 2 ** min(int(attempt_count or 1), 5)))
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def dispatch_pending(*, limit=25, account_id=None, event_id=None):
    """Deliver due events and return aggregate, non-sensitive results."""
    summary = {"claimed": 0, "delivered": 0, "cancelled": 0,
               "retrying": 0, "failed": 0, "not_configured": 0}
    api_key = os.environ.get("TRANSACTIONAL_EMAIL_API_KEY", "").strip()
    sender = os.environ.get("TRANSACTIONAL_EMAIL_FROM", "").strip()
    endpoint = os.environ.get(
        "TRANSACTIONAL_EMAIL_API_URL", "https://api.resend.com/emails"
    ).strip()
    if not api_key or not sender or not endpoint:
        summary["not_configured"] = 1
        return summary
    try:
        if event_id is not None:
            events = notifications_db.claim_event(account_id, event_id)
        else:
            notifications_db.recover_expired_claims(account_id=account_id)
            events = notifications_db.claim(limit=limit, account_id=account_id)
    except Exception as exc:
        print(f"Notification outbox could not be claimed: {type(exc).__name__}")
        summary["failed"] += 1
        return summary

    summary["claimed"] = len(events)

    for event in events:
        try:
            account = notifications_db.get_account(event["account_id"])
            recipient = (account or {}).get("email", "").strip()
            if not account or not notifications_db.is_actionable(event, account):
                notifications_db.finalize(event, "cancelled", error_code="resolved")
                summary["cancelled"] += 1
                continue
            if not recipient:
                notifications_db.finalize(event, "failed", error_code="recipient_missing")
                summary["failed"] += 1
                continue

            subject, copy, path = _message(event)
            url = f"{config.PUBLIC_BASE_URL}{path}"
            response = requests.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Idempotency-Key": f"sendkeep/{event['id']}",
                },
                json={
                    "from": sender,
                    "to": [recipient],
                    "subject": subject,
                    "text": f"{copy}\n\nOpen Sendkeep: {url}\n\nNo email content is included in this alert.",
                },
                timeout=10,
            )
            if 200 <= response.status_code < 300:
                payload = response.json() if response.content else {}
                provider_id = payload.get("id") if isinstance(payload, dict) else None
                notifications_db.finalize(event, "delivered", provider_message_id=provider_id)
                summary["delivered"] += 1
            elif response.status_code == 429 or response.status_code >= 500:
                notifications_db.finalize(
                    event, "retry", error_code=f"provider_{response.status_code}",
                    retry_at=_retry_at(event.get("attempt_count")),
                )
                summary["retrying"] += 1
            else:
                notifications_db.finalize(
                    event, "failed", error_code=f"provider_{response.status_code}",
                )
                summary["failed"] += 1
        except (requests.Timeout, requests.ConnectionError):
            notifications_db.finalize(
                event, "retry", error_code="provider_ambiguous",
                retry_at=_retry_at(event.get("attempt_count")),
            )
            summary["retrying"] += 1
        except Exception as exc:
            print(f"Notification event could not be finalized: {type(exc).__name__}")
            summary["failed"] += 1
    return summary


def dispatch_event(*, account_id, event_id):
    """Deliver exactly one account-owned event without global maintenance."""
    return dispatch_pending(limit=1, account_id=account_id, event_id=event_id)


# Direct sending is intentionally disabled. Product transitions enqueue
# atomically; direct sends would recreate the crash gap this outbox closes.
def notify(*_args, **_kwargs):
    return False


notify_transactionally = notify
new_reply = notify
follow_up_due = notify
commitment_due = notify
account_error = notify
account_recovered = notify
