"""Account-scoped style signals derived from human-approved drafts.

The review queue stores the original model copy and the copy an operator sent.
An unchanged approval is a weak positive preference signal; an edit is a
stronger one. This module turns only the *shape* of those approved bodies into
a small prompt hint. It deliberately does not return examples, contact data,
or reply text: the signal is useful for a sender's voice without becoming a
cross-account training corpus.
"""

import re
import threading
import time

import config
import drafts_db
import reviews_db


_CACHE_TTL_SECONDS = 300
_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_LOCK = threading.Lock()


def _word_count(text: str) -> int:
    return len(re.findall(r"\b[\w']+\b", text, flags=re.UNICODE))


def _sentence_count(text: str) -> int:
    return max(1, len(re.findall(r"[.!?]+", text)))


def _opening_style(text: str) -> str:
    first = next((line.strip().lower() for line in text.splitlines() if line.strip()), "")
    if re.match(r"^(hi|hello|hey)(?:[!,\s]|$)", first):
        return "a greeting"
    if first:
        return "a direct opening"
    return "no consistent opening"


def _human_approved_bodies(account_id: str) -> tuple[list[str], int]:
    """Return approved bodies and the subset that the operator edited.

    Model rewrites are excluded because they are not a founder preference.
    Requiring both original and approved copy keeps incomplete legacy rows from
    becoming style evidence.
    """
    rows = []
    for loader in (drafts_db.list_sent_edit_pairs, reviews_db.list_sent_edit_pairs):
        try:
            rows.extend(loader(account_id))
        except Exception as e:  # style learning must never block drafting
            print(f"Could not load voice signals for {account_id}: {e}")

    bodies = []
    edited = 0
    for row in rows:
        if row.get("rewritten"):
            continue
        original = "\n".join([
            (row.get("original_body") or row.get("original_draft_reply") or "").strip(),
        ]).strip()
        approved = (row.get("body") or row.get("draft_reply") or "").strip()
        if original and approved:
            bodies.append(approved)
            if original != approved:
                edited += 1
    return bodies, edited


def _build_profile(bodies: list[str], edited_count: int) -> str:
    # One approval is too weak to call a preference. Wait for a small,
    # meaningful sample rather than presenting coincidence as a learned voice.
    if len(bodies) < 2:
        return ""

    words = [_word_count(body) for body in bodies]
    average_words = round(sum(words) / len(words))
    average_sentence_words = round(
        sum(_word_count(body) / _sentence_count(body) for body in bodies) / len(bodies)
    )
    openings = {}
    for body in bodies:
        style = _opening_style(body)
        openings[style] = openings.get(style, 0) + 1
    common_opening, common_count = max(openings.items(), key=lambda item: item[1])
    questions = sum("?" in body for body in bodies)

    hints = [f"keep messages around {average_words} words"]
    if average_sentence_words <= 18:
        hints.append("favor short sentences")
    elif average_sentence_words >= 30:
        hints.append("longer explanatory sentences are acceptable")
    if common_count / len(bodies) >= 0.6:
        hints.append(f"usually start with {common_opening}")
    if questions / len(bodies) >= 0.6:
        hints.append("a direct question is commonly used")

    edit_note = f"; {edited_count} were edited" if edited_count else ""
    return (
        f"Observed from {len(bodies)} human approvals in this inbox{edit_note}: "
        + "; ".join(hints)
        + ". Treat this as a soft style preference, never as a fact about the prospect."
    )[:600]


def profile(account_id: str) -> str:
    """Return a short, cached, account-scoped style hint."""
    if not config.VOICE_FEEDBACK_ENABLED:
        return ""
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(account_id)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]
    bodies, edited_count = _human_approved_bodies(account_id)
    result = _build_profile(bodies, edited_count)
    with _CACHE_LOCK:
        _CACHE[account_id] = (now, result)
    return result


def with_profile(account: dict) -> dict:
    """Attach the signal to a copy of an account, never mutate DB state."""
    account_id = str(account.get("id") or "").strip()
    if not account_id:
        return account
    hint = profile(account_id)
    return {**account, "voice_profile": hint} if hint else account


def clear(account_id: str | None = None):
    """Clear cached signals after an edit in tests or a long-lived process."""
    with _CACHE_LOCK:
        if account_id is None:
            _CACHE.clear()
        else:
            _CACHE.pop(account_id, None)
