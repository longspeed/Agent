"""Closing the bounce loop: turn a delivery failure into a suppression, a
visible sheet status, and -- past a threshold -- an automatic sending pause.

Kept out of watch_replies.py because the same rules gate the *send* path:
prepare_drafts() and send_all_prepared() both refuse to run for an account
whose bounce rate has reached the point where continuing puts the customer's
sending domain at risk. Detection lives in gmail.find_bounce(); this module
owns what we do about it.

The hard/transient distinction gmail.find_bounce() draws has to survive all the
way through to the threshold, not just to the suppression call. A 4.x.x is a
greylist or a full mailbox -- it says nothing about the address being bad, so it
must not suppress, must not count toward the pause, and must not end the row's
reply checks."""

import accounts_db
import drafts_db
import sheets
import suppressions_db

# Permanent, 5.x.x. The address is bad: suppressed, never retried.
STATUS_BOUNCED = "Bounced"
# Transient, 4.x.x. The address is probably fine and the mail may still land, so
# this is a separate status: it stays in reply-check scope (see
# sheets.get_reply_check_rows) and stays out of the bounce-rate numerator.
STATUS_DELAYED = "Delayed"

# Mailbox providers start reading sustained bounce rates of a few percent as a
# spam signal, so pause below that rather than at the point of damage.
PAUSE_THRESHOLD = 0.05
# ...but not off a handful of sends. One bounce out of three is 33% and means
# nothing; pausing a brand-new account on it would be a worse bug than the one
# this guard exists to prevent.
MIN_SENDS_FOR_PAUSE = 20
# Providers judge recent behaviour, so the rate has to as well. Over an
# account's lifetime, 5,000 clean historical sends hide 200 consecutive bounces
# today at 3.8% -- under the threshold, while the domain burns. A freshly
# imported garbage list is the one thing this module exists to catch, and it is
# exactly what a lifetime denominator is best at concealing.
WINDOW_DAYS = 30

# Every status a row can hold once it has actually been emailed lives in
# sheets.SENT_STATUSES, because the send path's duplicate guard and the
# reply-check scope have to agree with this denominator. All of them count in the
# denominator; only STATUS_BOUNCED counts in the numerator.
_SENT_STATUSES = sheets.SENT_STATUSES


class SendingPaused(RuntimeError):
    """Raised on the send path when the account's bounce rate is unsafe."""


def record(account, row_index, email, bounce):
    """Files one detected bounce.

    Only a permanent (5.x.x) failure suppresses the address. A transient 4.x.x
    is a full mailbox, a greylist, or a server having a bad afternoon --
    permanently blocking a real prospect over one would lose a lead we could
    still reach. Both kinds mark the row so the customer can see what happened
    in their own sheet, but with different statuses, because everything
    downstream of here needs to keep telling them apart."""
    permanent = bounce["permanent"]
    # Suppression first, sheet second. This row is the durable record -- it both
    # protects the address from the next campaign and is the count the pause
    # threshold is measured against. A failing sheet write must not be able to
    # lose it; add() is idempotent, so the retry on the next poll is harmless.
    if permanent:
        suppressions_db.add(account["id"], email, source=HARD_BOUNCE_SOURCE)
    status = STATUS_BOUNCED if permanent else STATUS_DELAYED
    sheets.update_row(account, row_index, status=status, expect_email=email)


HARD_BOUNCE_SOURCE = "hard_bounce"


def _acknowledged(account):
    """How many hard bounces the operator has already seen and accepted.

    Reads as 0 on a database that predates the column, which makes the pause
    behave exactly as it did before the acknowledgement existed rather than
    failing."""
    try:
        return int(account.get("bounce_ack_count") or 0)
    except (TypeError, ValueError):
        return 0


def _hard_bounces(account, cutoff):
    """(total, within_window) hard bounces for this account, from the suppression
    list rather than from "Bounced" sheet rows.

    That distinction is the whole point, and it applies to BOTH numbers. Every
    input to the pause decision has to come from data the operator cannot edit,
    because the only lever that lowers a sheet-derived bounce rate is deleting
    rows -- on a product that sells a full audit trail.

    Measured off the sheet, two separate gestures disabled the guard silently.
    Deleting some Bounced rows dropped the count below an acknowledgement
    watermark, so it could not re-arm for another hundred bounces. Deleting all of
    them zeroed the numerator outright, so the rate read 0% and the guard never
    even consulted the watermark. The second one is why the numerator moved here
    too, not just the watermark.

    Suppression rows are append-only, written by record() on every permanent
    failure, and unreachable from a spreadsheet. One caveat: add() is idempotent
    per address, so an address that unsubscribed before it bounced has no
    hard_bounce row and is not counted. That undercounts by design -- a suppressed
    address cannot be emailed again, so it cannot damage the domain further."""
    stamps = suppressions_db.list_source_timestamps(account["id"], HARD_BOUNCE_SOURCE)
    return len(stamps), sum(1 for at in stamps if at >= cutoff)


def _unacknowledged(account, recorded):
    """Hard bounces recorded since the last acknowledgement."""
    ack = _acknowledged(account)
    if ack > recorded:
        # Impossible in normal operation: the watermark is only ever set to a
        # recorded count, and that count only grows. Higher means the suppression
        # list was pruned, or the watermark was saved when sheet rows were the
        # counter. Either way it no longer describes this account, and a number
        # we cannot trust must not be able to hold the guard down -- so treat it
        # as nothing acknowledged. One click repairs it; clamping to the recorded
        # count instead would re-clamp on every read and never re-arm at all.
        return recorded
    return recorded - ack


def _sheet_view(rows):
    """(sent, hard_bounced) as the operator's own SHEET reports them.

    Display only, for explaining a discrepancy. Never an input to the verdict --
    every gating value below comes from our own append-only records, because the
    sheet moved the rate in BOTH directions. Deleting Bounced rows zeroed the
    numerator; pasting rows that carry a status and a date inflated the
    denominator until three real bounces against 25 real sends read as 2.4%
    instead of 12%. The accidental version of that is worse than the deliberate
    one: a blank status is what makes a row eligible, so marking rows "Sent" is
    the natural way to exclude leads from a campaign, and 200 excluded rows
    diluted the rate to nothing."""
    sent = bounced = 0
    for _, row in rows:
        status = row[sheets.COL_STATUS].strip()
        if status not in _SENT_STATUSES:
            continue
        sent += 1
        bounced += status == STATUS_BOUNCED
    return sent, bounced


def stats(account, rows=None):
    """Hard-bounce rate over the last WINDOW_DAYS of sending.

    Both terms of the ratio come from our own records: the numerator from
    append-only hard-bounce suppressions, the denominator from the send log
    (outreach_drafts, one row per email that Gmail accepted). There is no sheet
    input to the verdict and no date-parsing fallback -- the log always carries a
    timestamp, which is what let the "all time" branch go. That branch existed for
    sheets with no SentAt values and was where dilution was easiest.

    rows is optional and now only supplies the display comparison. Omitting it
    means the send path performs no sheet read for this guard at all."""
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    sent = drafts_db.count_sent_since(account["id"], cutoff)
    recorded, bounced = _hard_bounces(account, cutoff)
    # Two append-only sources counted independently, so a bounce recorded just
    # outside the send window could otherwise exceed the sends inside it. Capping
    # keeps the reported rate at 100% rather than something nonsensical above it;
    # it never lowers the rate.
    bounced = min(bounced, sent) if sent else bounced

    rate = bounced / sent if sent else 0.0
    unsafe = sent >= MIN_SENDS_FOR_PAUSE and rate > PAUSE_THRESHOLD
    sheet_sent, sheet_bounced = _sheet_view(rows) if rows is not None else (None, None)
    return {
        "sent": sent,
        "bounced": bounced,
        "rate": rate,
        "window": f"last {WINDOW_DAYS} days",
        # What the operator's own sheet shows, or None when it was not read.
        # Reported so the UI can explain a discrepancy; never an input above.
        "sheet_sent": sheet_sent,
        "sheet_bounced": sheet_bounced,
        "hard_bounces_recorded": recorded,
        "acknowledged": _acknowledged(account),
        "threshold": PAUSE_THRESHOLD,
        # How many sends the guard still needs before it can pause at all. The
        # send log starts empty for accounts whose history predates it, so this is
        # the honest answer to "why didn't it pause" rather than a silent zero.
        "min_sends_to_arm": MIN_SENDS_FOR_PAUSE,
        "armed": sent >= MIN_SENDS_FOR_PAUSE,
        # Split on purpose: acknowledged is not the same as healthy, and the UI
        # should be able to say so.
        "unsafe": unsafe,
        "paused": unsafe and _unacknowledged(account, recorded) > 0,
    }



def pause_reason(account, rows=None, current=None):
    """The blocker string for the UI, or "" when sending is safe. Matches the
    shape of agent.account_send_blockers() so the campaign preview can show
    this the same way it shows every other reason the button is disabled.

    current accepts an already-computed stats() dict so a caller showing both
    (the campaign preview) computes it once.

    The remediation is deliberately not "delete the bad rows". Deleting them is
    the only thing that would lower the rate, and this is a product that sells a
    full audit trail -- a compliance guard whose fix is tampering with the log is
    not a guard. Every hard bounce here is already suppressed and cannot be
    retried, so acknowledging is a real answer, not a bypass."""
    current = current if current is not None else stats(account, rows)
    if not current["paused"]:
        return ""
    reason = (
        f"Sending is paused: {current['bounced']} of {current['sent']} emails sent in the "
        f"{current['window']} bounced ({current['rate']:.0%}), above the "
        f"{PAUSE_THRESHOLD:.0%} safety threshold. Those addresses are already suppressed "
        "and will not be retried. Nothing has been deleted -- review them in your sheet, "
        "then acknowledge to keep sending."
    )
    if sheet_disagrees(current):
        # Otherwise this reads as the guard firing for no reason, and the obvious
        # next guess is to delete more rows.
        reason += (
            f" Your sheet shows {current['sheet_bounced']} bounced rows, but we have "
            f"{current['hard_bounces_recorded']} on record; the count that matters is "
            "ours, so removing rows will not lift this."
        )
    return reason


def acknowledge(account):
    """Records that the operator has seen the current bounces and chose to
    continue. Sending resumes until a new hard bounce lands, so a genuinely
    deteriorating list re-pauses instead of being waved through once.

    The watermark is the suppression-list count, not the sheet's -- see
    _hard_bounces_recorded for why that difference decides whether this is an
    acknowledgement or an off switch."""
    current = stats(account)
    recorded = current["hard_bounces_recorded"]
    accounts_db.set_bounce_ack(account["id"], recorded)
    return {**current, "acknowledged": recorded, "paused": False}


def sheet_disagrees(current):
    """True when the operator's sheet shows fewer hard bounces than we recorded.

    Not an accusation -- rows get deleted for ordinary reasons. It is what lets
    the UI explain why a pause is in force when the sheet looks clean, instead of
    the guard appearing to fire at random. False when the sheet was not read."""
    if current["sheet_bounced"] is None:
        return False
    return current["sheet_bounced"] < current["hard_bounces_recorded"]


def assert_sendable(account, rows=None):
    reason = pause_reason(account, rows)
    if reason:
        raise SendingPaused(reason)
