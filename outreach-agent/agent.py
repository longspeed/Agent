import difflib
import re
import time

import requests

import gmail
import providers
import usage
from config import DEFAULT_MEETING_PURPOSE

MAX_RETRIES = 4
# Ceiling on how long one provider may park a request. Free tiers answer a
# blown daily quota with Retry-After values in the thousands of seconds; sleeping
# on that holds a web request open for an hour to no purpose. Past this, the
# provider counts as unavailable and the next one in the chain gets the call.
MAX_RETRY_WAIT = 30

OUTREACH_SYSTEM = """You are a senior SDR who writes cold emails that get replied to. You
write like one person emailing one person -- not like a template with the name swapped in.

What makes these work: a real reason for writing to THIS person, one concrete idea, and an
ask so small it is easier to say yes than to think about it.

Truth rules (these outrank everything else):
- You are given a name, sometimes a company, sometimes one researched note about them, a
  description of what the sender offers, and the goal of the email. That is the entirety of
  what you know.
- Never invent anything about the recipient: role, tools, headcount, funding, recent posts,
  events you both attended, mutual connections, or any prior interaction. None of it.
- Never invent anything about the sender either: no customer names, no percentages, no case
  studies, no pricing, no timelines. If it was not given to you, it does not go in the email.

Structure, 3-5 sentences total:
1. Open with why you are writing to them specifically. If a research note was given, that is
   your reason -- state it plainly, no flattery. If there is no note, be straightforward
   about writing cold; never fake familiarity.
2. One sentence on what the sender offers, framed as what it changes for someone in their
   position. Not a feature list, not adjectives.
3. One specific, low-friction ask that fits the sender's goal -- it will NOT always be a
   meeting. If a call-to-action link is given, name the next step in plain words and put the
   link in as a bare URL (book a time, start a free trial, see a demo, grab the guide,
   whatever the goal is). If no link is given, ask them to reply. Keep the commitment small:
   it should be easier to say yes than to think about it. Never use markdown for the link.

Voice:
- Write the ENTIRE email in ONE consistent language: English by default, or the language the
  sender's own instructions ask for. Never MIX languages or inject a stray word, token, or
  character from a different script mid-sentence -- a lone Cyrillic/Georgian/CJK/Arabic
  character in an otherwise-English email is always a bug, not a translation.
- Plain, direct, unexcited. Short words. Contractions are fine.
- Do not open with "I", and do not spend the first sentence talking about the sender.
- No flattery ("love what you're building"), no manufactured enthusiasm, no apologising for
  emailing.
- Every sentence must carry a concrete specific: a named problem, a number, a real outcome,
  or the researched fact you were given. Empty filler is banned -- "bring clarity to your
  decisions", "a brief meeting to discuss", "slot something in", "pick your brain", "see if
  there's a fit", "discuss how we can help". If you have nothing specific to say, the goal
  you were given is too vague; write the most concrete honest version you can, never filler.
- Banned outright: "I hope this email finds you well", "quick question", "circle back",
  "touch base", "synergy", "leverage", "reach out", "reaching out", "excited to", "passionate
  about", "just following up", "game-changer", "seamless", "cutting-edge", "best-in-class",
  "revolutionize", and the "it's not just X, it's Y" construction.
- No em dashes and no en dashes. Use a comma, a full stop, or a semicolon.
- Plain text only: no markdown, no bullets, no emoji, no bold.
- No placeholders, ever. Never write [Name], {{company}}, <role>, or "Your Name". If you do
  not know something, leave it out of the sentence entirely.
- Sign off with the sender's first name on its own line. If a company name was given, put it
  on the next line. Nothing else: no "Best regards", no title, no phone number.

Subject line:
- Under 8 words, sentence case, specific to the reason you are writing.
- It should read like something a colleague would send, not a campaign.
- No emoji, no clickbait, no ALL CAPS, not "Quick question", and not just the company name.

Output exactly this and nothing else:
Subject: <the subject line>
<blank line>
<the email body>
"""

REPLY_SYSTEM = """You draft the reply the sender will send to a prospect who wrote back on a
cold outreach thread. The thread has a goal the sender set -- it might be a call, a demo, a
trial signup, a reply, or something else -- but this reply's only job is to respond to what
the prospect actually wrote. A draft that ignores their words or re-pitches on autopilot is
worse than no draft.

Read their newest message carefully and match the reply to what it actually is:

- They want to move forward or asked how: confirm in one short line and give the call-to-action
  link if there is one. If they proposed a specific time and the goal is a meeting, accept it
  and use the link only to lock it in. No extra selling -- they already said yes.
- They asked questions: answer every question they asked, in their order, before anything
  else. Use only facts provided in the context or earlier in the thread. If the information
  given doesn't contain the answer (pricing, integrations, customer names, anything), say so
  plainly and offer to cover it live or in a follow-up -- never guess and never invent details.
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
- Include the call-to-action link only when they show interest or ask about next steps. If
  there is no link, tell them the next step in a plain sentence instead.
- No corporate jargon, no manufactured enthusiasm ("Great question!"), no apologizing for
  emailing, no "just following up."
- Plain text only: no subject line, no markdown, no bullet points, no emoji.
- Sign off with just the sender's first name.

Output only the email body, nothing else.
"""


class ProviderUnavailable(Exception):
    """One provider could not produce text. Never fatal on its own -- the caller
    moves to the next provider in the chain."""


def _post(provider, system, user_prompt, purpose, may_wait):
    """One provider's full attempt budget. Returns the generated text, or raises
    ProviderUnavailable so the chain can move on.

    may_wait is False while another provider is still untried: waiting out a
    rate limit is only worth it when there is nothing else to ask."""
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(
                provider.endpoint,
                headers={
                    "Authorization": f"Bearer {provider.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": provider.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_prompt},
                    ],
                },
                timeout=60,
            )
        except requests.RequestException as e:
            raise ProviderUnavailable(f"request failed: {e}") from e

        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", 10) or 10)
            if not may_wait or wait > MAX_RETRY_WAIT or attempt == MAX_RETRIES - 1:
                raise ProviderUnavailable(f"rate-limited (Retry-After: {wait}s)")
            print(f"{provider.display} rate-limited, retrying in {wait}s...")
            time.sleep(wait)
            continue
        if not response.ok:
            # A bad key, a retired model id, or a provider outage. All three are
            # someone else's endpoint being unusable, not a reason to fail the
            # draft while another provider is configured.
            raise ProviderUnavailable(f"HTTP {response.status_code}: {response.text[:200]}")

        data = response.json()
        # Meter the pooled key against whichever account is active. Input and
        # output tokens are recorded separately because they're priced
        # differently -- a reply draft carries a whole thread as input, so its
        # cost profile is nothing like a first-touch email's. Recorded against
        # the model that actually served the call, so the numbers stay honest
        # when the chain falls through to a different provider.
        call_usage = data.get("usage") or {}
        prompt_tokens = call_usage.get("prompt_tokens")
        completion_tokens = call_usage.get("completion_tokens")
        usage.record_llm(
            purpose,
            provider.model,
            prompt_tokens,
            completion_tokens,
            total_tokens=call_usage.get("total_tokens"),
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise ProviderUnavailable(f"unexpected response shape: {e}") from e
        if content is None:
            # Some providers return content=null instead of text on a refusal,
            # moderation block, or transient hiccup -- retry like a 429 rather
            # than crashing with an AttributeError on the caller's .strip().
            finish_reason = data["choices"][0].get("finish_reason")
            if attempt < MAX_RETRIES - 1:
                print(f"{provider.display} returned empty content (finish_reason={finish_reason!r}), retrying...")
                continue
            raise ProviderUnavailable(f"empty content after {MAX_RETRIES} attempts (finish_reason={finish_reason!r})")
        return content.strip()

    raise ProviderUnavailable(f"no usable response after {MAX_RETRIES} attempts")


def _chat(system, user_prompt, purpose):
    """Sends one generation to the first provider that can serve it.

    The chain is purpose-dependent: a reply draft carries the prospect's own
    words and is barred from free-tier endpoints while a paid one exists. See
    providers.chain_for."""
    chain = providers.chain_for(purpose)
    failures = []
    for i, provider in enumerate(chain):
        try:
            return _post(provider, system, user_prompt, purpose, may_wait=i == len(chain) - 1)
        except ProviderUnavailable as e:
            failures.append(f"{provider.display}: {e}")
            print(f"{provider.display} unavailable ({e}); trying the next provider.")
    raise RuntimeError(
        f"No LLM provider could draft this ({purpose}). " + " | ".join(failures)
    )


def _sender_context(account):
    return {
        "sender_name": account.get("sender_name") or "the team",
        "sender_company": (account.get("sender_company") or "").strip(),
        "meeting_purpose": (account.get("meeting_purpose") or "").strip(),
        "calendar_link": (account.get("calendar_booking_link") or "").strip(),
        # Free-form sender preferences (tone, things to always mention/avoid,
        # length, language). Capped so it can't balloon the prompt or be used to
        # bury the system rules under a wall of text.
        "custom_instructions": (account.get("custom_instructions") or "").strip()[:600],
    }


def _custom_instructions_block(instructions):
    """Renders the sender's custom instructions as a bounded, clearly-fenced
    prompt section. Framed as preferences that never override the hard rules --
    and the validator enforces those rules regardless of what lands here, so a
    sender cannot prompt their way past plain-text/opt-out/no-placeholder."""
    if not instructions:
        return ""
    return (
        "\n\nThe sender gave these instructions for how they want their emails written. "
        "Follow them for tone, content, length, and language, but NEVER at the expense of "
        "the rules in the system message (the truth rules, plain text, the unsubscribe line, "
        "no placeholders, one consistent script):\n"
        f'"""{instructions}"""'
    )


def _rewrite_instruction_block(instruction):
    """Renders an operator's "ask AI to rewrite" instruction as a bounded,
    clearly-fenced prompt section -- the same shape as
    _custom_instructions_block, for the same reason. The operator isn't an
    attacker (this isn't the injection case), but an unfenced instruction
    like "make it more impressive" is an open invitation to invent a case
    study, a statistic, or a claim that isn't in the current draft --
    _validate_outreach/_validate_reply check banned phrases, placeholders,
    and formatting, never truth, so fabrication has no other gate."""
    return (
        "\n\nThe sender asked for this change. Apply it for tone, content, and length, "
        "but NEVER invent a fact, statistic, case study, or claim that isn't already in "
        "the current draft or the context above, and never at the expense of the rules "
        "in the system message:\n"
        f'"""{instruction}"""'
    )


# Tolerant of everything models actually emit here: "**Subject:**", "subject -",
# a quoted subject, an en/em dash separator. The strict prefix strip this
# replaced only matched a literal "Subject:".
_SUBJECT_LINE = re.compile(
    r"^\s*(?:\*\*|__|#{1,3}\s*)?\s*subject\s*(?:\*\*|__)?\s*[:\-–—]\s*(.+?)\s*$",
    re.IGNORECASE,
)


def _split_subject(text):
    """Returns (subject_or_None, body).

    When the first line is not a recognisable subject line the body is returned
    WHOLE and subject is None. That asymmetry is the point: the previous
    implementation always consumed line 1, so any generation that omitted the
    "Subject:" prefix silently lost its opening sentence into the subject
    header. Losing the subject is recoverable (we retry); losing a sentence out
    of the middle of a sent email is not.
    """
    first, _, rest = text.partition("\n")
    match = _SUBJECT_LINE.match(first)
    if not match:
        return None, text.strip()
    # "**Subject:** hi" puts the closing markdown after the colon, so it lands
    # in the capture -- strip any stray asterisks/underscores and quotes off it.
    subject = match.group(1).strip().strip("*_").strip().strip('"').strip("'")
    return subject, rest.strip()


# Phrases that mark an email as machine-written to anyone who reads cold email.
# Kept to things that are always wrong in this context, because every entry
# here costs a regeneration.
_BANNED_PHRASES = (
    "hope this email finds you well", "hope this finds you well",
    "hope you're doing well", "hope you are doing well",
    "quick question", "circle back", "touch base", "synergy", "synergies",
    "leverage", "reach out", "reaching out", "reached out", "excited to",
    "passionate about", "just following up", "game-changer", "game changer",
    "cutting-edge", "best-in-class", "revolutionize", "revolutionise",
    "seamless", "i wanted to reach", "let's hop on",
)

# Empty, say-nothing filler -- the tell of an email with no real reason behind
# it. Every one of these is a phrase that fills space while conveying zero
# specifics; each showed up in outputs that read as generic mail-merge.
_FILLER_PHRASES = (
    "bring clarity", "slot something in", "pick your brain",
    "hop on a quick call", "brief meeting", "brief chat", "brief call",
    "upcoming decisions", "see if there's a fit", "see if there is a fit",
    "wanted to connect", "wanted to introduce myself", "discuss how we can help",
    "explore how we can", "explore synergies", "the right person",
)

# Any character outside Latin script + common typographic punctuation. A weak
# model sometimes injects a token from another script mid-sentence
# ("Reaching out დღის ..." -- real output from the free model); no legitimate
# cold email to a Latin-script recipient should contain them. Catches Cyrillic,
# Georgian, Greek, CJK, Arabic, Hebrew, Devanagari, emoji, etc. in one shot.
# Allowed: Basic Latin + Latin-1/Extended-A/B (\x00-ɏ), the punctuation
# block that holds dashes/curly quotes/ellipsis (‐-‧), euro, trademark.
_NON_LATIN = re.compile("[^\x00-ɏ‐-‧€™]")

# [Name], {{company}}, <role>, "Your Name" -- an unfilled template escaping into
# a real inbox is the single most damaging thing that can come out of this.
_PLACEHOLDER = re.compile(
    r"\[[^\]\n]{0,40}\]|\{\{|\}\}|<[a-z][a-z _-]{1,28}>|your name|your company|\bxyz\b",
    re.IGNORECASE,
)

# Markdown the prompt forbids but models emit anyway: bold, headings, links,
# and bulleted lines.
_MARKDOWN = re.compile(r"\*\*|__|\]\(|^\s*[-*+]\s+|^\s*#{1,6}\s+", re.MULTILINE)

_MIN_BODY_CHARS = 120
_MAX_BODY_CHARS = 1400
_MAX_SUBJECT_CHARS = 90

# --- Reply validation ------------------------------------------------------
# Deliberately its own constants. _validate_outreach's cannot be reused here and
# the reasons are specific, not stylistic:
#
#   _BANNED_PHRASES holds "reach out", "reaching out", "reached out" -- correct
#   for a cold email, wrong for a reply, because REPLY_SYSTEM says to reference
#   their actual words and a prospect who wrote "thanks for reaching out" should
#   get a reply that mirrors it. Reusing the list would reject the right answer
#   and regenerate it into a worse one.
#
#   _MIN_BODY_CHARS is 120. The correct reply to "not interested" is about 60
#   characters and REPLY_SYSTEM explicitly asks for one or two sentences with no
#   counter-offer. The floor would force a regeneration away from the right
#   answer.
#
#   The calendar link is a hard requirement for outreach and a conditional one
#   here -- REPLY_SYSTEM says include it "only when they show interest".
#
# The rule for what belongs here: enforce what REPLY_SYSTEM actually asks for,
# plus what cannot be walked back once sent. A validator that checks something
# the prompt never required puts the regeneration loop in a fight with the
# prompt, and the loop loses twice and then strands the reply.
_REPLY_MIN_BODY_CHARS = 20
_REPLY_MAX_BODY_CHARS = 1200

# Only phrases REPLY_SYSTEM names outright.
_REPLY_BANNED_PHRASES = (
    "just following up", "great question", "thanks for your patience",
    "sorry to bother", "sorry for the interruption",
)

# Anything a mail client will turn into a tappable link. Note the framing: not
# "a URL". The recipient's client decides what becomes clickable, and Gmail
# linkifies a bare domain -- so an attacker never has to type "https://",
# because Gmail types it for them on the other end. A scheme-only pattern was
# therefore checking for the one form the attacker has no reason to use.
#
# Three branches: scheme'd, www-prefixed, and a bare domain followed by a path.
# The path requirement on the third keeps ordinary prose ("we support Xero. Also
# ...") from matching, at the cost of missing a bare domain with no path.
#
# KNOWN OPEN, deliberately not attempted here: mobile Gmail turns phone numbers
# into tel: handlers, which is a published exfiltration vector containing no URL
# at all. Outlook, Apple Mail and every webmail client have their own
# linkification rules and change them without notice. This set cannot be
# enumerated from inside this process, which is why the actual control is
# provenance -- replies never enter the fast approval lane, whatever this
# pattern does or does not catch (reviews_db.lane). What follows is defence in
# depth, and it is worth having precisely because it is not what holds the line.
_URL = re.compile(
    r"""(?ix)
    (?: https?://[^\s<>"')\]]+                      # scheme'd
      | www\.[^\s<>"')\]]+                          # no scheme, www-prefixed
      | \b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.[a-z]{2,24}/[^\s<>"')\]]*
    )                                               # bare domain WITH a path
    """
)


def _link_key(raw):
    """A link reduced to what a mail client will actually dial out to, so the
    sender's own address matches whether or not the model wrote the scheme.
    Without this, a model writing 'cal.com/x' for a configured
    'https://cal.com/x' gets flagged as a foreign link."""
    return (raw or "").strip().rstrip(".,;:)]}\"'").lower() \
        .removeprefix("https://").removeprefix("http://").removeprefix("www.").rstrip("/")


def _validate_reply(body, calendar_link=""):
    """Problems with a drafted reply; empty means it is safe to queue.

    The reply path had no mechanical gate at all before this: outreach got a
    validator and a two-attempt regeneration loop, while the path carrying a
    stranger's words straight into the prompt returned the model's first
    response unchecked. The defences were inverted relative to the trust."""
    problems = []
    stripped = body.strip()
    lowered = stripped.lower()

    if len(stripped) < _REPLY_MIN_BODY_CHARS:
        problems.append(f"The reply is only {len(stripped)} characters; it reads as truncated.")
    elif len(stripped) > _REPLY_MAX_BODY_CHARS:
        problems.append(
            f"The reply is {len(stripped)} characters. Match their length; when unsure, shorter."
        )

    # The injection outcome that actually costs something: a link the prospect
    # planted, approved at a glance and sent under the sender's name.
    allowed = {_link_key(calendar_link)} if _link_key(calendar_link) else set()
    foreign = [u for u in _URL.findall(stripped) if _link_key(u) not in allowed]
    if foreign:
        problems.append(
            "Remove these links -- the only URL allowed in a reply is the sender's own "
            "call-to-action link: " + ", ".join(sorted(set(foreign))[:3]) + "."
        )

    stray = _NON_LATIN.findall(stripped)
    if stray:
        uniq = "".join(dict.fromkeys(stray))
        problems.append(
            f"Remove non-English/garbled characters and emoji ({uniq[:15]!r}); "
            "write the whole reply in plain English."
        )

    placeholder = _PLACEHOLDER.search(stripped)
    if placeholder:
        problems.append(f"Unfilled placeholder in the reply: {placeholder.group(0)!r}.")

    if _MARKDOWN.search(stripped):
        problems.append("Remove markdown formatting; this is a plain-text reply.")

    # REPLY_SYSTEM: "no subject line". A model that emits one produces a reply
    # whose first line is furniture.
    if lowered.startswith("subject:"):
        problems.append("Remove the subject line; a reply is body only.")

    banned = [p for p in _REPLY_BANNED_PHRASES if p in lowered]
    if banned:
        problems.append("Remove these phrases: " + ", ".join(banned) + ".")

    return problems


def _validate_outreach(subject, body, calendar_link, unsubscribe_url=""):
    """Returns a list of human-readable problems with a generated email; empty
    means it is safe to queue. Every check here guards something that cannot be
    walked back once the message leaves Gmail."""
    problems = []
    lowered = body.lower()

    if not subject:
        problems.append('No "Subject:" line was produced.')
    elif len(subject) > _MAX_SUBJECT_CHARS:
        problems.append(f"Subject is {len(subject)} characters; keep it under {_MAX_SUBJECT_CHARS}.")
    elif "subject" in subject.lower()[:8]:
        problems.append(f'Subject still contains its own label: {subject!r}.')

    if len(body) < _MIN_BODY_CHARS:
        problems.append(f"Body is only {len(body)} characters; it reads as truncated.")
    elif len(body) > _MAX_BODY_CHARS:
        problems.append(f"Body is {len(body)} characters; cut it to 3-5 sentences.")

    # The entire call-to-action is this link. A model that paraphrases it
    # ("book a time on my calendar") produces a plausible email with no way to act.
    if calendar_link and calendar_link not in body:
        problems.append("The scheduling link is missing from the body; include it verbatim.")

    if unsubscribe_url and unsubscribe_url not in body:
        problems.append("The unsubscribe line is missing from the body.")

    # Garbled/foreign-script tokens a weak model injects mid-sentence. Checked
    # against the subject too. This is a hard fail: a single such character
    # makes the whole email look broken.
    stray = _NON_LATIN.findall((subject or "") + "\n" + body)
    if stray:
        uniq = "".join(dict.fromkeys(stray))
        problems.append(
            f"Remove non-English/garbled characters ({uniq[:15]!r}); write the whole email in plain English."
        )

    found = [p for p in _BANNED_PHRASES if p in lowered]
    if found:
        problems.append("Remove these banned phrases: " + ", ".join(found) + ".")

    filler = [p for p in _FILLER_PHRASES if p in lowered]
    if filler:
        problems.append(
            "Cut this empty filler and say something concrete instead (a specific problem, "
            "number, or outcome): " + ", ".join(filler) + "."
        )

    if "—" in body or "–" in body:
        problems.append("Remove the em/en dashes; use a comma or a full stop.")

    placeholder = _PLACEHOLDER.search(body)
    if placeholder:
        problems.append(f"Unfilled placeholder in the body: {placeholder.group(0)!r}.")

    if _MARKDOWN.search(body):
        problems.append("Remove markdown formatting; this is a plain-text email.")

    return problems


def _opt_out_line(unsubscribe_url):
    """Appended in code, never generated. A one-click opt-out is a legal
    obligation in most of the jurisdictions this app is used from, and a link
    the model retypes is a link it will eventually mangle."""
    return f"Not the right time? Unsubscribe here and I won't email again: {unsubscribe_url}"


def _strip_opt_out_line(body, unsubscribe_url):
    """Removes the opt-out line before a rewrite ever shows the body to the
    model -- the same reasoning as _opt_out_line's own comment, applied to
    the one other place a stored body reaches a prompt. Content-based, not
    suffix-based: the operator may have hand-edited the body since it was
    generated, so the line is not guaranteed to still be the exact trailing
    suffix generate_outreach_email appended (extra text after it, an extra
    blank line, whitespace drift) -- a suffix-only match would silently
    no-op on any of those and let the model see (and retype, and mangle)
    the line anyway. Drops every line containing the literal
    unsubscribe_url substring and the surrounding blank lines; a no-op when
    unsubscribe_url is falsy or isn't present at all."""
    if not unsubscribe_url or unsubscribe_url not in body:
        return body
    kept = [line for line in body.split("\n") if unsubscribe_url not in line]
    return "\n".join(kept).strip()


# A floor against near-emptiness only -- deliberately NOT calibrated to catch
# "To have a meeting with our team" (31 chars), the live example that started
# this fix. That case cannot be reached by raising this number: an existing,
# intentional test (test_account_send_blockers_requires_real_sender_name)
# requires a 17-character purpose ("sell CLIs to devs") to pass, and any
# floor above 17 breaks it. Since the
# 31-char reported case is *longer* than that legitimate short example, no
# length floor can block one without also blocking the other -- length is not
# a proxy for specificity, in either direction. Set just above single-word/
# stub answers ("hi", "meeting", "sync up"), which is the only thing a length
# check can honestly claim to catch. Blocker copy must say "add more detail",
# never imply quality was checked, or a guard that only measures length ends
# up implying a judgment it never made. The reported case is a disclosed,
# known gap -- see TODOS.md -- not silently pretended-away.
MIN_MEETING_PURPOSE_LENGTH = 15

# How similar a purpose can be to DEFAULT_MEETING_PURPOSE (59 chars) before
# it is treated as a reword of the default rather than a real answer. This
# check and MIN_MEETING_PURPOSE_LENGTH catch DISJOINT failure modes, not
# overlapping ones: the default itself is 59 characters, well above the
# floor, so only this check catches it. Do not remove either one -- each
# blocks something the other misses entirely.
_MEETING_PURPOSE_SIMILARITY_THRESHOLD = 0.8


def _is_near_duplicate_of_default(purpose: str) -> bool:
    """True if `purpose` is a light reword of DEFAULT_MEETING_PURPOSE rather
    than a distinct answer. difflib's ratio is 2*matches/(len(a)+len(b)) --
    a paraphrase-of-the-default tolerance, not a typo tolerance."""
    ratio = difflib.SequenceMatcher(
        None, purpose.lower(), DEFAULT_MEETING_PURPOSE.lower()
    ).ratio()
    return ratio >= _MEETING_PURPOSE_SIMILARITY_THRESHOLD


def account_send_blockers(account):
    """Settings that must be filled before any outreach can be generated, as a
    list of human-readable reasons (empty = ready). The campaign preview shows
    these so the batch button can be disabled with an explanation, instead of
    letting the operator kick off a run that fails every row at generation
    time. generate_outreach_email enforces the same condition itself, so the
    gate holds even if a caller skips the preview.

    The call-to-action link is intentionally NOT required: outreach doesn't have
    to drive to a booking page -- with no link the email simply asks for a
    reply. Only a real, non-generic goal is mandatory."""
    ctx = _sender_context(account)
    blockers = []
    # An unset sender name defaults to "the team" (see _sender_context), which
    # signs every email like a faceless bot -- the fastest way to get a cold
    # email deleted. Require a real name.
    if not ctx["sender_name"] or ctx["sender_name"].strip().lower() == "the team":
        blockers.append(
            "Add your first name in Settings - outreach is signed 'the team' until you do, "
            "which reads as a bot."
        )
    meeting_purpose = ctx["meeting_purpose"]
    if not meeting_purpose:
        blockers.append(
            "Describe what you're reaching out about in Settings - the default text is too "
            "generic to write a useful email from."
        )
    elif len(meeting_purpose) < MIN_MEETING_PURPOSE_LENGTH:
        blockers.append(
            "Add more detail to what you're reaching out about in Settings - a specific "
            "offer, audience, or outcome. Short purposes produce empty-sounding emails."
        )
    elif _is_near_duplicate_of_default(meeting_purpose):
        blockers.append(
            "What you're reaching out about in Settings is too close to the default text - "
            "describe your own offer, audience, or outcome instead."
        )
    return blockers


def outreach_prompt(account, name, company, lead_reason=""):
    """The first-attempt user prompt for one first-touch email.

    Split out of generate_outreach_email so eval_models.py can measure the real
    prompt rather than an approximation of it -- a model comparison run against
    a paraphrase of production is worth nothing."""
    ctx = _sender_context(account)
    base_prompt = (
        f"Recipient: {name}"
        + (f" at {company}" if company else "")
        + f"\nSender: {ctx['sender_name']}"
        + (f"\nSender's company (use in the signature): {ctx['sender_company']}" if ctx["sender_company"] else "")
        + f"\nWhat the sender offers and wants to happen (the goal): {ctx['meeting_purpose']}"
    )
    if ctx["calendar_link"]:
        base_prompt += (
            f"\nCall-to-action link to include verbatim as the next step: {ctx['calendar_link']}"
        )
    else:
        base_prompt += (
            "\nNo call-to-action link is set: make the ask a simple reply to this email, "
            "not a link."
        )
    if lead_reason:
        # Sourced by leads.py from web search, so it is the one verified thing
        # we know about this person -- and also machine-extracted, hence the
        # explicit ceiling on how far it may be pushed.
        base_prompt += (
            f"\n\nResearch note about this person (the reason you are writing to them): {lead_reason}"
            "\nUse this as your opening reason for writing. Do not extrapolate past it, do not"
            " restate it as something you personally saw or read, and do not treat it as a"
            " relationship you already have."
        )
    base_prompt += _custom_instructions_block(ctx["custom_instructions"])
    base_prompt += "\n\nWrite the email."
    return base_prompt


def retry_prompt(base_prompt, previous_text, problems):
    """Second pass: show the model its own output and exactly what was wrong with
    it. Far more reliable than re-rolling the same prompt."""
    return (
        base_prompt
        + "\n\nYour previous attempt was rejected:\n---\n"
        + previous_text
        + "\n---\nFix all of these and output the corrected email in the required format:\n"
        + "\n".join(f"- {p}" for p in problems)
    )


def generate_outreach_email(account, name, company, lead_reason="", unsubscribe_url=""):
    ctx = _sender_context(account)
    # An untouched default goal gives the model no honest way to write "here's
    # what's in it for you" -- refuse rather than send content-free mail. The
    # CTA link is optional (see account_send_blockers).
    blockers = account_send_blockers(account)
    if blockers:
        raise RuntimeError(" ".join(blockers))

    base_prompt = outreach_prompt(account, name, company, lead_reason)

    attempts = []
    for attempt in range(2):
        user_prompt = base_prompt
        if attempts:
            user_prompt = retry_prompt(base_prompt, attempts[-1]["text"], attempts[-1]["problems"])

        text = _chat(OUTREACH_SYSTEM, user_prompt, usage.DRAFT_EMAIL)
        subject, body = _split_subject(text)
        if unsubscribe_url:
            body = f"{body}\n\n{_opt_out_line(unsubscribe_url)}"

        problems = _validate_outreach(subject, body, ctx["calendar_link"], unsubscribe_url)
        if not problems:
            return subject, body
        attempts.append({"text": text, "problems": problems})

    # Raising leaves the sheet row untouched and surfaces in the batch's failed
    # list, matching how every other per-contact failure behaves: nothing was
    # sent, so the row stays safe to retry.
    raise RuntimeError(
        "Could not generate a usable email for "
        f"{name or 'this contact'} after 2 attempts: " + " ".join(attempts[-1]["problems"])
    )


def rewrite_outreach_prompt(account, name, company, subject, body, instruction):
    """The user prompt for one outreach rewrite. body must already have its
    opt-out line stripped (see rewrite_outreach_email) -- it's never
    mentioned here, so there's nothing prompting the model to invent one."""
    ctx = _sender_context(account)
    return (
        f"Sender's first name (sign with this): {ctx['sender_name']}\n"
        f"What the sender offers and the goal of the outreach: {ctx['meeting_purpose'] or DEFAULT_MEETING_PURPOSE}\n"
        f"Call-to-action link: {ctx['calendar_link'] or '(none set)'}\n"
        f"Prospect: {name}" + (f" at {company}" if company else "")
        + f"\n\nHere is the current drafted email:\n---\nSubject: {subject}\n\n{body}\n---"
        + _rewrite_instruction_block(instruction)
        + _custom_instructions_block(ctx["custom_instructions"])
        + '\n\nApply the change and output the complete revised email, in the same '
        '"Subject: ..." plus body format.'
    )


def rewrite_outreach_email(account, name, company, subject, body, instruction, unsubscribe_url=""):
    """Rewrites an already-drafted outreach email per the operator's
    instruction. subject/body are the operator's CURRENT textarea contents
    (passed in by the caller, not re-fetched from drafts_db) -- a rewrite
    must revise what's actually in front of them, including any hand-edit
    they haven't sent yet.

    Same 2-attempt validate-and-retry shape as generate_outreach_email, and
    raises the same way on failure: nothing was written yet, so failing
    leaves the existing draft untouched in the browser."""
    ctx = _sender_context(account)
    stripped_body = _strip_opt_out_line(body, unsubscribe_url) if unsubscribe_url else body
    base_prompt = rewrite_outreach_prompt(account, name, company, subject, stripped_body, instruction)

    attempts = []
    for attempt in range(2):
        user_prompt = base_prompt
        if attempts:
            user_prompt = retry_prompt(base_prompt, attempts[-1]["text"], attempts[-1]["problems"])

        text = _chat(OUTREACH_SYSTEM, user_prompt, usage.DRAFT_EMAIL)
        new_subject, new_body = _split_subject(text)
        # Re-appended in code, never generated -- see _opt_out_line. The
        # "not already there" guard mirrors send_prepared_draft's own
        # defensive check: belt and suspenders behind the search-based
        # strip above, not a substitute for it.
        if unsubscribe_url and unsubscribe_url not in new_body:
            new_body = f"{new_body}\n\n{_opt_out_line(unsubscribe_url)}"

        problems = _validate_outreach(new_subject, new_body, ctx["calendar_link"], unsubscribe_url)
        if not problems:
            return new_subject, new_body
        attempts.append({"text": text, "problems": problems})

    raise RuntimeError(
        "Could not rewrite this email after 2 attempts: " + " ".join(attempts[-1]["problems"])
    )


def reply_prompt(account, name, company, customer_reply, history=()):
    """The user prompt for one reply draft. Split out of draft_reply for the
    same reason outreach_prompt is separate: eval_models scores production's
    exact prompt rather than an approximation that drifts away from it.

    history: (from_contact, body) pairs for every earlier message on the thread,
    oldest first (see gmail.get_latest_reply_with_history)."""
    ctx = _sender_context(account)
    conversation = "\n\n".join(
        f"{'They wrote' if from_contact else 'We wrote'}:\n{gmail.strip_quoted(body)}"
        for from_contact, body in history
        if body.strip()
    )
    return (
        f"Sender's first name (sign with this): {ctx['sender_name']}\n"
        # Unlike outreach, a reply is never blocked on this being filled in:
        # the prospect already engaged, and leaving them hanging because a
        # settings field is generic would be the worse failure.
        f"What the sender offers and the goal of the outreach: {ctx['meeting_purpose'] or DEFAULT_MEETING_PURPOSE}\n"
        f"Call-to-action link: {ctx['calendar_link'] or '(none set)'}\n"
        f"Prospect: {name}"
        + (f" at {company}" if company else "")
        + f"\n\nThe conversation so far, oldest first:\n{conversation}\n\n"
        f"Their newest message -- the one to answer now:\n{gmail.strip_quoted(customer_reply)}"
        + _custom_instructions_block(ctx["custom_instructions"])
        + "\n\nDraft the sender's reply."
    )


def reply_problems(account, draft):
    """The validator's unresolved objections to a drafted reply, as a list.

    draft_reply returns its last attempt even when validation still fails --
    losing the notification is worse than queueing a flawed draft -- so by
    design a reply can reach the queue carrying problems. This is how the caller
    finds out which ones, without changing draft_reply's return shape and its
    eight call sites.

    Recorded on the review so the operator is told WHY a draft is suspect rather
    than left to spot it. The problems do not decide the lane: a reply is
    full-text regardless, because that is settled by where the prompt's text
    came from, not by what the validator managed to catch in the output."""
    return _validate_reply(draft or "", _sender_context(account)["calendar_link"])


def draft_reply(account, name, company, customer_reply, history=()):
    """history: (from_contact, body) pairs for every earlier message on the
    thread, oldest first (see gmail.get_latest_reply_with_history)."""
    ctx = _sender_context(account)
    user_prompt = reply_prompt(account, name, company, customer_reply, history)

    # Same two-attempt shape as generate_outreach_email: show the model its own
    # output and exactly what was wrong with it, which is far more reliable than
    # re-rolling the same prompt.
    #
    # Unlike outreach, this returns the last attempt rather than raising when
    # both fail. A raised reply-draft used to mean no review row, which meant
    # the operator was never told a prospect had written back -- the draft is a
    # convenience, the notification is the product. The caller queues whatever
    # comes back and the operator edits it; returning a flawed draft they can
    # see beats a clean exception they cannot.
    attempts = []
    for attempt in range(2):
        prompt = user_prompt
        if attempts:
            prompt = retry_prompt(user_prompt, attempts[-1]["text"], attempts[-1]["problems"])
        text = _chat(REPLY_SYSTEM, prompt, usage.DRAFT_REPLY)
        problems = _validate_reply(text, ctx["calendar_link"])
        if not problems:
            return text
        attempts.append({"text": text, "problems": problems})
        print(f"Reply draft attempt {attempt + 1} rejected: {'; '.join(problems)}")

    print(
        "Reply draft still has problems after 2 attempts; queueing it for the "
        "operator to fix rather than losing the reply."
    )
    return attempts[-1]["text"]


def rewrite_reply_prompt(account, name, company, customer_reply, draft_text, instruction):
    """The user prompt for one reply rewrite. Deliberately lighter than
    reply_prompt: no reconstructed thread history (that needs a live Gmail
    read plus own_addresses, see gmail.get_history_before) -- just the
    message being answered and the current draft, which is enough context
    for a tone/length adjustment."""
    ctx = _sender_context(account)
    return (
        f"Sender's first name (sign with this): {ctx['sender_name']}\n"
        f"What the sender offers and the goal of the outreach: {ctx['meeting_purpose'] or DEFAULT_MEETING_PURPOSE}\n"
        f"Call-to-action link: {ctx['calendar_link'] or '(none set)'}\n"
        f"Prospect: {name}" + (f" at {company}" if company else "")
        + f"\n\nTheir message we're responding to:\n{gmail.strip_quoted(customer_reply)}\n\n"
        f"Here is the current drafted reply:\n---\n{draft_text}\n---"
        + _rewrite_instruction_block(instruction)
        + _custom_instructions_block(ctx["custom_instructions"])
        + "\n\nApply the change and output the complete revised reply."
    )


def rewrite_reply(account, name, company, customer_reply, draft_text, instruction):
    """Rewrites an already-drafted reply per the operator's instruction.
    draft_text is the operator's CURRENT textarea contents (passed in by the
    caller, not re-fetched from reviews_db) -- see rewrite_outreach_email
    for why.

    Unlike draft_reply, this RAISES rather than returning a best-effort last
    attempt on failure. draft_reply's "never raise" choice exists because
    failing there would mean no review row at all, and the operator would
    never learn a prospect replied -- the draft is a convenience, the
    notification is the product. Here the review already exists with its
    current draft intact, so failing a rewrite loses nothing, and a clear
    error is more honest than silently keeping a possibly-broken attempt."""
    ctx = _sender_context(account)
    base_prompt = rewrite_reply_prompt(account, name, company, customer_reply, draft_text, instruction)

    attempts = []
    for attempt in range(2):
        prompt = base_prompt
        if attempts:
            prompt = retry_prompt(base_prompt, attempts[-1]["text"], attempts[-1]["problems"])
        text = _chat(REPLY_SYSTEM, prompt, usage.DRAFT_REPLY)
        problems = _validate_reply(text, ctx["calendar_link"])
        if not problems:
            return text
        attempts.append({"text": text, "problems": problems})

    raise RuntimeError(
        "Could not rewrite this reply after 2 attempts: " + " ".join(attempts[-1]["problems"])
    )
