"""Pre-flight domain authentication check for the address an account sends from.

Answers one question before the first campaign goes out: when this mail arrives,
can the receiving server tell it was authorised? That is SPF, DKIM and DMARC.
Nothing here sends or changes anything -- it is a read-only lookup surfaced as
advice, because an unauthenticated domain is the single largest cause of cold
mail landing in spam, and the customer will blame us for it, not their DNS.

Resolution goes over DNS-over-HTTPS through `requests` rather than a resolver
library: it adds no dependency (requests already backs every other outbound
call) and behaves identically on Windows, where system resolver tooling and
stdlib support for TXT lookups are both absent."""

import re

import requests

DOH_URL = "https://dns.google/resolve"

# Mailbox providers whose domain we do not control and must not lecture the
# user about -- Google publishes SPF/DKIM/DMARC for these already.
FREE_PROVIDERS = {"gmail.com", "googlemail.com"}

# The selector Google Workspace publishes DKIM under. A custom domain could use
# a different one, so its absence is reported as "not found", never as "you
# have no DKIM" -- see _dkim_check.
GOOGLE_DKIM_SELECTOR = "google._domainkey"


def _txt_records(name):
    """TXT strings published at a name.

    Returns None when the lookup itself failed, which is deliberately distinct
    from [] (the lookup succeeded and nothing is published). Telling someone
    "no SPF record" because a network blip ate the query would be a false alarm
    about a domain that is in fact configured correctly."""
    try:
        response = requests.get(DOH_URL, params={"name": name, "type": "TXT"}, timeout=10)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return None
    records = []
    for answer in payload.get("Answer") or []:
        raw = (answer.get("data") or "").strip()
        # Long TXT values arrive as several quoted chunks that must be joined --
        # SPF and DKIM records routinely exceed the 255-byte per-string limit.
        joined = "".join(re.findall(r'"([^"]*)"', raw))
        records.append(joined or raw.strip('"'))
    return [r for r in records if r]


def _finding(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def _spf_check(domain):
    records = _txt_records(domain)
    if records is None:
        return _finding("SPF", "unknown", "Could not reach DNS to check SPF. Try again.")
    spf = next((r for r in records if r.lower().startswith("v=spf1")), "")
    if not spf:
        return _finding(
            "SPF", "missing",
            f"No SPF record on {domain}. Receiving servers cannot confirm you are "
            "allowed to send from this domain. Publish a TXT record starting with "
            "v=spf1 that includes your mail provider.",
        )
    if "_spf.google.com" not in spf:
        return _finding(
            "SPF", "warning",
            "An SPF record exists but does not authorise Google. Since you send "
            "through Gmail, add include:_spf.google.com to it or your mail can "
            "fail SPF.",
        )
    return _finding("SPF", "ok", "SPF is published and authorises Google.")


def _dkim_check(domain):
    records = _txt_records(f"{GOOGLE_DKIM_SELECTOR}.{domain}")
    if records is None:
        return _finding("DKIM", "unknown", "Could not reach DNS to check DKIM. Try again.")
    for record in records:
        key = re.search(r"\bp\s*=\s*([A-Za-z0-9+/=]*)", record)
        if key is None:
            continue
        if not key.group(1):
            # RFC 6376: an empty p= is a REVOKED key, not a present one. It is
            # worse than publishing nothing, because it actively tells
            # receivers to distrust anything signed with this selector.
            return _finding(
                "DKIM", "missing",
                f"The DKIM key at {GOOGLE_DKIM_SELECTOR}.{domain} is published but revoked "
                "(its p= value is empty), so mail signed with it will fail. Generate a new "
                "key in Google Admin and publish it.",
            )
        return _finding("DKIM", "ok", "DKIM is published under Google's selector.")

    return _finding(
        "DKIM", "warning",
        f"No DKIM key found at {GOOGLE_DKIM_SELECTOR}.{domain}. If you sign with a "
        "different selector this is fine; otherwise turn on DKIM signing in "
        "Google Admin and publish the key it gives you.",
    )


def _dmarc_check(domain):
    records = _txt_records(f"_dmarc.{domain}")
    if records is None:
        return _finding("DMARC", "unknown", "Could not reach DNS to check DMARC. Try again.")
    dmarc = next((r for r in records if r.lower().startswith("v=dmarc1")), "")
    if not dmarc:
        return _finding(
            "DMARC", "missing",
            f"No DMARC record on {domain}. Gmail and Yahoo now require one for bulk "
            "senders. Publish a TXT record at _dmarc.{domain} starting with "
            "v=DMARC1; p=none to begin monitoring.".replace("{domain}", domain),
        )
    policy = re.search(r"\bp\s*=\s*(none|quarantine|reject)", dmarc, re.I)
    return _finding(
        "DMARC", "ok",
        f"DMARC is published (policy: {policy.group(1).lower() if policy else 'unspecified'}).",
    )


def check_domain(domain):
    """Read-only SPF/DKIM/DMARC report for one sending domain."""
    domain = (domain or "").strip().lower().lstrip("@")
    if not domain:
        return {
            "domain": "", "managed": False, "findings": [],
            "summary": "No sending address connected yet.",
        }

    if domain in FREE_PROVIDERS:
        return {
            "domain": domain,
            "managed": True,
            "findings": [],
            "summary": (
                f"{domain} is managed by Google, so SPF, DKIM and DMARC are already "
                "published and there is nothing for you to configure. Be aware that "
                "cold outreach from a free address still lands in spam far more often "
                "than mail from your own domain."
            ),
        }

    findings = [_spf_check(domain), _dkim_check(domain), _dmarc_check(domain)]
    broken = [f for f in findings if f["status"] in ("missing", "warning")]
    unknown = [f for f in findings if f["status"] == "unknown"]
    if broken:
        summary = f"{len(broken)} of 3 checks need attention before you send."
    elif unknown:
        summary = "Could not complete the DNS checks. Try again in a moment."
    else:
        summary = "SPF, DKIM and DMARC all look correct."
    return {"domain": domain, "managed": False, "findings": findings, "summary": summary}
