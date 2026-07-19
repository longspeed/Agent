import time

import requests

import gmail
import usage
from config import (
    CLIPROXY_API_KEY,
    CLIPROXY_BASE_URL,
    CLIPROXY_MODEL,
    OPENROUTER_API_KEY,
    OPENROUTER_MODEL,
)

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

REPLY_SYSTEM = """You draft the reply the sender will send to a prospect who wrote back on a
cold outreach thread. The thread's end goal is a booked meeting via the scheduling link, but
this reply's only job is to respond to what the prospect actually wrote -- a draft that
ignores their words or re-pitches on autopilot is worse than no draft.

Read their newest message carefully and match the reply to what it actually is:

- They want to meet or asked how to book: confirm in one short line and give the scheduling
  link. If they proposed a specific time, accept it and use the link only to lock it in.
  No extra selling -- they already said yes.
- They asked questions: answer every question they asked, in their order, before anything
  else. Use only facts provided in the context or earlier in the thread. If the information
  given doesn't contain the answer (pricing, integrations, customer names, anything), say so
  plainly and offer to cover it on the call -- never guess and never invent details.
- They have an objection or hesitation ("not now", "we already use X", "too busy"): name the
  specific thing they said, respond to it honestly in a sentence if the context gives you a
  real response, then make the smallest possible next ask (a yes/no question, or "worth
  revisiting in <their timeframe>?"). Do not repeat the original pitch.
- They said no / not interested / stop emailing: accept it graciously in one or two
  sentences. Thank them for replying and close the thread politely. No link, no
  counter-offer, no "just in case" pitch.
- Auto-reply or out-of-office: one short line acknowledging it, referencing their return
  date if they gave one. No pitch, no link.
- Wrong person, or they pointed you to someone else: thank them and ask for the hand-off
  ("would you mind forwarding this to <name/role>?"), using the person or role they
  mentioned.

Style:
- Mirror their tone and length: a two-line casual reply gets a short casual answer; a
  formal, detailed email earns a fuller one. When unsure, shorter.
- Reference their actual words where natural, so it reads like a person who listened --
  but never quote their message back at length.
- Include the scheduling link only when they show interest or ask about logistics. If no
  scheduling link is provided, ask for their availability instead.
- No corporate jargon, no manufactured enthusiasm ("Great question!"), no apologizing for
  emailing, no "just following up."
- Plain text only: no subject line, no markdown, no bullet points, no emoji.
- Sign off with just the sender's first name.

Output only the email body, nothing else.
"""


def _chat(system, user_prompt):
    # CLIPROXY_BASE_URL is local-dev-only (see config.py) -- unset in every
    # real deployment, so this always falls through to OpenRouter there.
    using_cliproxy = bool(CLIPROXY_BASE_URL)
    if using_cliproxy:
        url = f"{CLIPROXY_BASE_URL.rstrip('/')}/chat/completions"
        api_key = CLIPROXY_API_KEY
        model = CLIPROXY_MODEL
        label = "CLIProxyAPI"
    else:
        url = API_URL
        api_key = OPENROUTER_API_KEY
        model = OPENROUTER_MODEL
        label = "OpenRouter"

    for attempt in range(MAX_RETRIES):
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=60,
        )
        if response.status_code == 429 and attempt < MAX_RETRIES - 1:
            wait = int(response.headers.get("Retry-After", 10))
            print(f"{label} rate-limited, retrying in {wait}s...")
            time.sleep(wait)
            continue
        response.raise_for_status()
        data = response.json()
        # Meter the pooled key against whichever account is active.
        tokens = (data.get("usage") or {}).get("total_tokens") or 1
        usage.record("llm", tokens, model)
        content = data["choices"][0]["message"]["content"]
        if content is None:
            # Some providers return content=null instead of text on a refusal,
            # moderation block, or transient hiccup -- retry like a 429 rather
            # than crashing with an AttributeError on the caller's .strip().
            finish_reason = data["choices"][0].get("finish_reason")
            if attempt < MAX_RETRIES - 1:
                print(f"{label} returned empty content (finish_reason={finish_reason!r}), retrying...")
                continue
            raise RuntimeError(f"{label} returned empty content after {MAX_RETRIES} attempts (finish_reason={finish_reason!r})")
        return content.strip()


def _sender_context(account):
    return {
        "sender_name": account.get("sender_name") or "the team",
        "meeting_purpose": account.get("meeting_purpose")
        or "a quick intro call to see if there's a fit to work together",
        "calendar_link": (account.get("calendar_booking_link") or "").strip(),
    }


def generate_outreach_email(account, name, company):
    ctx = _sender_context(account)
    if not ctx["calendar_link"]:
        raise RuntimeError("Set a calendar booking link in Settings before sending outreach")

    user_prompt = (
        f"Recipient: {name}"
        + (f" at {company}" if company else "")
        + f"\nSender: {ctx['sender_name']}"
        + f"\nReason for the meeting: {ctx['meeting_purpose']}"
        + f"\nScheduling link to include as the call-to-action: {ctx['calendar_link']}"
        + "\n\nWrite the email."
    )

    text = _chat(OUTREACH_SYSTEM, user_prompt)

    # Split on the first newline, not "\n\n": models regularly omit the blank
    # line after the subject, and partitioning on "\n\n" then swallows the
    # entire body into the subject.
    subject_line, _, body = text.partition("\n")
    subject = subject_line.removeprefix("Subject:").strip()
    return subject, body.strip()


def draft_reply(account, name, company, customer_reply, history=()):
    """history: (from_contact, body) pairs for every earlier message on the
    thread, oldest first (see gmail.get_latest_reply_with_history)."""
    ctx = _sender_context(account)
    conversation = "\n\n".join(
        f"{'They wrote' if from_contact else 'We wrote'}:\n{gmail.strip_quoted(body)}"
        for from_contact, body in history
        if body.strip()
    )
    user_prompt = (
        f"Sender's first name (sign with this): {ctx['sender_name']}\n"
        f"What the sender offers / why they wanted the meeting: {ctx['meeting_purpose']}\n"
        f"Scheduling link: {ctx['calendar_link'] or '(none set)'}\n"
        f"Prospect: {name}"
        + (f" at {company}" if company else "")
        + f"\n\nThe conversation so far, oldest first:\n{conversation}\n\n"
        f"Their newest message -- the one to answer now:\n{gmail.strip_quoted(customer_reply)}\n\n"
        "Draft the sender's reply."
    )
    return _chat(REPLY_SYSTEM, user_prompt)
