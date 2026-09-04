"""Conservative extraction of concrete promises from inbound replies.

This first pass intentionally prefers precision over recall. It only creates a
candidate when the sender uses a clear first-person commitment such as
"I'll send the deck Tuesday". Candidates never send mail automatically and
always require confirmation before they become scheduled work.
"""
from datetime import datetime, timedelta, timezone
import calendar
import re


_COMMITMENT_RE = re.compile(
    r"\b(?:(?:i|we)\s+(?:will|can|plan to|am going to|are going to)|"
    r"(?:i|we)'ll)\s+"
    r"(?P<action>[^.!?\n]{3,160})",
    re.IGNORECASE,
)
_DUE_RE = re.compile(
    r"\b(?P<phrase>today|tomorrow|this\s+week|next\s+week|"
    r"in\s+\d+\s+(?:day|days|week|weeks)|"
    r"(?:on|by)\s+(?:next\s+)?(?:monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday)|"
    r"(?:on|by)\s+\d{1,2}(?:st|nd|rd|th)?\s+"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)(?:\s+\d{4})?)\b",
    re.IGNORECASE,
)
_WEEKDAYS = {name.lower(): index for index, name in enumerate(calendar.day_name)}
_MONTHS = {name.lower(): index for index, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name[:3].lower(): index for name, index in _MONTHS.items()})
_DANGLING_ACTION_WORDS = {
    "a", "an", "and", "for", "of", "or", "the", "to", "with", "your",
}


def _is_concrete_action(action):
    """Reject truncated promise fragments before they enter the review queue."""
    words = re.findall(r"[a-z0-9']+", (action or "").lower())
    if len(words) < 2 or words[-1] in _DANGLING_ACTION_WORDS:
        return False
    # A verb plus only determiners/pronouns is grammar, not a deliverable.
    meaningful_tail = [
        word for word in words[1:]
        if word not in _DANGLING_ACTION_WORDS and word not in {"it", "this", "that"}
    ]
    return bool(meaningful_tail)


def _reference(value=None):
    # Gmail message helpers serialize timestamps at the API boundary so they
    # can be stored in review payloads. Accept that ISO-8601 representation
    # here as well as a datetime; otherwise every real reply reaches
    # `value.tzinfo` with a string and the additive commitment pass fails after
    # the reply itself has already been queued.
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            value = None
    if not isinstance(value, datetime):
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _parse_due(text, reference):
    match = _DUE_RE.search(text)
    if not match:
        return None, "", 0.62

    phrase = re.sub(r"\s+", " ", match.group("phrase").strip())
    lower = phrase.lower()
    base = reference.replace(hour=9, minute=0, second=0, microsecond=0)
    if lower == "today":
        return base, phrase, 0.91
    if lower == "tomorrow":
        return base + timedelta(days=1), phrase, 0.91
    if lower in {"this week", "next week"}:
        days = 7 if lower == "next week" else max(1, 4 - reference.weekday())
        return base + timedelta(days=days), phrase, 0.78

    relative = re.fullmatch(r"in (\d+) (day|days|week|weeks)", lower)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        days = amount * 7 if unit.startswith("week") else amount
        return base + timedelta(days=days), phrase, 0.86

    weekday_match = re.search(
        r"(?:on|by)\s+(?:(next)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)",
        lower,
    )
    if weekday_match:
        target = _WEEKDAYS[weekday_match.group(2)]
        delta = (target - reference.weekday()) % 7
        if delta == 0:
            delta = 7
        return base + timedelta(days=delta), phrase, 0.88

    date_match = re.search(
        r"(?:on|by)\s+(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)(?:\s+(\d{4}))?",
        lower,
    )
    if date_match:
        day = int(date_match.group(1))
        month = _MONTHS.get(date_match.group(2)[:3])
        year = int(date_match.group(3) or reference.year)
        if month:
            if not date_match.group(3) and (year, month, day) < (
                reference.year, reference.month, reference.day
            ):
                year += 1
            try:
                return base.replace(year=year, month=month, day=day), phrase, 0.94
            except ValueError:
                pass

    return None, phrase, 0.68


def extract_commitments(text, reference_at=None, actor="contact"):
    """Return conservative, reviewable commitment candidates from one message."""
    text = (text or "").strip()
    if not text:
        return []
    reference = _reference(reference_at)
    results = []
    seen = set()
    for match in _COMMITMENT_RE.finditer(text):
        action = re.sub(r"\s+", " ", match.group("action").strip(" ,;:"))
        if len(action) < 3 or not _is_concrete_action(action):
            continue
        # Resolve a date from the same promise sentence, not from the whole
        # reply. A contact can promise two different actions on two different
        # dates; borrowing the first date for every candidate would create a
        # confident-looking but wrong reminder.
        due_at, due_text, due_confidence = _parse_due(match.group(0), reference)
        key = (action.lower(), due_text.lower())
        if key in seen:
            continue
        seen.add(key)
        evidence = match.group(0).strip()
        if due_text and due_text.lower() not in evidence.lower():
            evidence = f"{evidence} ({due_text})"
        results.append({
            "actor": actor if actor in {"contact", "operator"} else "contact",
            "commitment_type": "promised_action",
            "action_text": action,
            "due_at": due_at.isoformat() if due_at else None,
            "due_text": due_text,
            "evidence": evidence,
            "confidence": round(min(0.96, 0.72 + due_confidence * 0.25), 3),
            "status": "detected",
        })
    return results
