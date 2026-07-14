import time

import requests

from config import OPENROUTER_API_KEY, OPENROUTER_MODEL, CALENDAR_BOOKING_LINK, MEETING_PURPOSE, SENDER_NAME

API_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_RETRIES = 4

OUTREACH_SYSTEM = """You are an expert SDR (sales development rep) who writes cold outreach
emails that get replies. You know the craft: short, specific, no fluff, no buzzwords,
no "I hope this email finds you well," no generic flattery. One clear idea, one clear ask.

Rules:
- 3-5 sentences total. Every sentence earns its place.
- Never invent facts about the recipient, their company, or any event/interaction that didn't
  happen. You are only given a name, and optionally a company name -- do not fabricate anything
  beyond that (no fake shared connections, events, or details you weren't given).
- Open by addressing the recipient by name and, only if a company name was given, naturally
  mention it. If no company was given, keep the opener general but still warm and direct --
  do not pretend to know something about them.
- State the reason for the meeting in one plain sentence: what's in it for them, not a feature list.
- Make the ask a single, low-friction sentence pointing to the scheduling link. Frame it as
  "grab 15 minutes" or similar, not "let's hop on a call to discuss synergies."
- No corporate jargon: avoid "synergy," "circle back," "touch base," "leverage," "reach out"
  (ironic, but avoid it in the body), "excited to," "passionate about."
- Plain text only, no markdown, no bullet points, no emoji.
- Sign off with just the sender's first name, no "Best regards," no company boilerplate signature.

Output exactly two parts, nothing else:
Line 1: "Subject: <subject line>" (subject under 8 words, specific, not clickbait, no emoji)
Then a blank line, then the email body.
"""

REPLY_SYSTEM = """You draft a reply to a customer who responded to a cold outreach email
that asked to book a meeting via a scheduling link. Be helpful, concise, and match their tone.
Output only the plain-text email body, no subject line, no markdown, no corporate jargon.
If they asked a question you can't answer with the information given, say so honestly and
offer to cover it on the call. If they haven't booked yet, gently point them back to the
scheduling link. Sign off with just the sender's first name.
"""


def _chat(system, user_prompt):
    for attempt in range(MAX_RETRIES):
        response = requests.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=60,
        )
        if response.status_code == 429 and attempt < MAX_RETRIES - 1:
            wait = int(response.headers.get("Retry-After", 10))
            print(f"OpenRouter rate-limited, retrying in {wait}s...")
            time.sleep(wait)
            continue
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()


def generate_outreach_email(name, company):
    user_prompt = (
        f"Recipient: {name}"
        + (f" at {company}" if company else "")
        + f"\nSender: {SENDER_NAME}"
        + f"\nReason for the meeting: {MEETING_PURPOSE}"
        + f"\nScheduling link to include as the call-to-action: {CALENDAR_BOOKING_LINK}"
        + "\n\nWrite the email."
    )

    text = _chat(OUTREACH_SYSTEM, user_prompt)

    subject_line, _, body = text.partition("\n\n")
    subject = subject_line.removeprefix("Subject:").strip()
    return subject, body.strip()


def draft_reply(original_email, customer_reply):
    user_prompt = (
        f"Sender: {SENDER_NAME}\n"
        f"Scheduling link: {CALENDAR_BOOKING_LINK}\n\n"
        f"Our original outreach email:\n{original_email}\n\n"
        f"Customer's reply:\n{customer_reply}\n\n"
        "Draft our reply."
    )
    return _chat(REPLY_SYSTEM, user_prompt)
