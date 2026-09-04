"""Review queue in Supabase, scoped per account. Every query filters on
account_id — combined with RLS on the table, one tenant can never see
another's reviews."""
import difflib
from datetime import datetime, timedelta, timezone

from supabase import create_client

from config import SUPABASE_URL, SUPABASE_SECRET_KEY

TABLE = "reviews"

# Rolling-deploy compatibility only. Sheet data begins on row 2, so zero can
# never address a real contact. New/migrated schemas store NULL; an old schema
# that still has reviews.row_index NOT NULL gets this reserved value until the
# nullable-row migration converts it back to NULL.
LEGACY_NO_SHEET_ROW = 0

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
    return _client


def find_review_id(account_id, thread_id, gmail_message_id, customer_reply=None):
    """Id of an existing review for this Gmail message (any status), or None.
    Keyed on the message id rather than thread_id alone, because a thread can
    carry more than one reply over time and keying on the thread would silently
    swallow every reply after the first.

    It used to be keyed on the reply BODY, which fails three ways at once and
    all three get worse the moment detection walks every message on a thread
    instead of only the newest:

      - Two identical short replies ("ok", "sounds good") on one thread collide,
        so the second is classified as already-reviewed and never surfaces.
      - PostgREST sends .eq() values as GET query parameters. A long quoted
        chain -- or raw HTML, which _extract_body returns when a message has no
        text/plain part -- eventually exceeds the URL limit and 414s. That
        raises inside the per-row try, becomes a row_error, and on a headless
        worker goes to a log nobody reads.
      - Postgres btree caps index entries near 2704 bytes, so the body could
        never carry the unique constraint that makes add_review actually
        race-safe rather than "not airtight".

    Gmail's message id is stable, unique, short, and already in the thread
    payload we fetched, so it fixes all three and costs one column.

    customer_reply is the legacy fallback and is only consulted for rows
    written before gmail_message_id existed (which are NULL and can never match
    an id lookup). Without it, every thread already carrying a review would
    produce one duplicate on the first cycle after the migration. Removable
    once no NULL gmail_message_id rows remain."""
    client = _get_client()
    if gmail_message_id:
        existing = (
            client.table(TABLE)
            .select("id")
            .eq("account_id", account_id)
            .eq("thread_id", thread_id)
            .eq("gmail_message_id", gmail_message_id)
            .limit(1)
            .execute()
        )
        if existing.data:
            return existing.data[0]["id"]

    if customer_reply is None:
        return None
    legacy = (
        client.table(TABLE)
        .select("id")
        .eq("account_id", account_id)
        .eq("thread_id", thread_id)
        .eq("customer_reply", customer_reply)
        .is_("gmail_message_id", "null")
        .limit(1)
        .execute()
    )
    return legacy.data[0]["id"] if legacy.data else None


def add_review(account_id, row_index, name, email, thread_id, customer_reply,
               draft_reply, gmail_message_id=None, status="pending",
               degraded_classification=False, validator_problems=None,
               return_created=False, create_alert=True):
    client = _get_client()
    if create_alert and hasattr(client, "rpc"):
        # Real deployments use the atomic transition. Tiny table-only fakes in
        # unit tests intentionally exercise the legacy insert behavior below.
        result = client.rpc("queue_reply_review_with_alert", {
            "p_account_id": account_id,
            "p_row_index": row_index,
            "p_name": name,
            "p_email": email,
            "p_thread_id": thread_id,
            "p_customer_reply": customer_reply,
            "p_draft_reply": draft_reply,
            "p_gmail_message_id": gmail_message_id,
            "p_status": status,
            "p_degraded_classification": degraded_classification,
            "p_validator_problems": "\n".join(validator_problems) if validator_problems else None,
        }).execute()
        payload = result.data or {}
        review_id = payload.get("id") if isinstance(payload, dict) else None
        created = bool(payload.get("created")) if isinstance(payload, dict) else False
        return (review_id, created) if return_created else review_id
    # Two layers, deliberately. This check-then-insert closes the realistic
    # overlapping-job case (the auto-poll firing while a previous check is still
    # in flight, both reads catching the same message before either write lands)
    # and reads clearly. It is NOT airtight against a true simultaneous race --
    # two processes can both pass it. The unique index on
    # (account_id, thread_id, gmail_message_id) is what actually enforces it,
    # which is why moving the key off the reply body mattered: the body could
    # never be indexed (btree caps entries near 2704 bytes), so this check was
    # the only guard there was.
    #
    # That second layer is what makes the server's /replies/check endpoint and
    # the background worker safe to run concurrently. Both call this.
    existing_id = find_review_id(account_id, thread_id, gmail_message_id, customer_reply)
    if existing_id is not None:
        return (existing_id, False) if return_created else existing_id

    payload = {
            "account_id": account_id,
            "row_index": row_index,
            "name": name,
            "email": email,
            "thread_id": thread_id,
            "customer_reply": customer_reply,
            "draft_reply": draft_reply,
            # Frozen copy of what the model wrote. draft_reply is mutable --
            # mark_sent replaces it with whatever the operator actually sent -- so
            # without this the edit pair is destroyed at send time and cannot be
            # reconstructed. Written once, never updated.
            #
            # None, not "", when there is no draft (drafting failed, or -- for a
            # flagged review -- was never attempted): Phase 6's edit-diff learning
            # reads this column against what the operator actually sent, and ("",
            # "the operator's entire hand-written reply") reads as "the model's
            # output should be replaced wholesale" rather than "the model wrote
            # nothing, this pair teaches nothing."
            "original_draft_reply": draft_reply or None,
            "gmail_message_id": gmail_message_id,
            "status": status,
            "degraded_classification": degraded_classification,
            # What the validator still objected to when this was queued. draft_reply
            # returns its last attempt even when validation fails, so a queued reply
            # can carry real problems -- and before this column the operator had no
            # way to know which draft that was. Stored newline-joined rather than
            # JSON because the only consumer is a human reading it.
            #
            # Explicitly NOT a lane input. The lane is decided by provenance in
            # lane() above, so an empty list here never promotes a reply into the
            # fast path. If it did, the fence's blind spots would silently widen the
            # low-attention surface -- which is the failure mode this whole split
            # exists to remove.
            "validator_problems": "\n".join(validator_problems) if validator_problems else None,
    }

    try:
        result = _get_client().table(TABLE).insert(payload).execute()
        review_id = result.data[0]["id"]
        return (review_id, True) if return_created else review_id
    except Exception as e:
        # The Gmail-only path and the nullable schema must be deployable in
        # either order. An older database rejects the truthful NULL with 23502.
        # Retry once with a value outside the valid Sheet row domain; the
        # migration below later normalizes it back to NULL. Restrict this to the
        # exact column/error pair so unrelated not-null failures still surface.
        error_text = str(e).lower()
        if (
            row_index is None
            and "23502" in error_text
            and "row_index" in error_text
        ):
            compatibility_payload = dict(payload, row_index=LEGACY_NO_SHEET_ROW)
            try:
                result = _get_client().table(TABLE).insert(
                    compatibility_payload
                ).execute()
                review_id = result.data[0]["id"]
                return (review_id, True) if return_created else review_id
            except Exception as retry_error:
                e = retry_error

        # The check-then-insert above closes the realistic overlap but not a
        # true simultaneous race: both jobs read "no review yet", both draft,
        # both insert. When the unique index on (account_id, thread_id,
        # gmail_message_id) rejects the loser, return the winner instead of
        # raising -- a duplicate-detection error must never cost the operator
        # their notification, and two drafts for one reply is exactly what
        # this function exists to prevent. (Postgres unique violation: 23505.)
        if "23505" in str(e) or "duplicate key" in str(e).lower():
            winner = find_review_id(account_id, thread_id, gmail_message_id)
            if winner is not None:
                print(f"Lost an add_review race on {thread_id}/{gmail_message_id}; "
                      "keeping the existing review.")
                return (winner, False) if return_created else winner
        raise e


def add_manual_review(account_id, row_index, name, email, thread_id,
                      customer_reply, draft_reply, gmail_message_id=None,
                      status="pending", degraded_classification=False,
                      validator_problems=None, return_created=False):
    """Create a review initiated by the operator without emailing an alert.

    The Threads composer is already open in front of the operator, so creating
    a notification for the same action would be noise.  This deliberately uses
    add_review's normal message-id dedupe and unique-index race handling; the
    only difference is skipping the outbox RPC.
    """
    return add_review(
        account_id, row_index, name, email, thread_id, customer_reply,
        draft_reply, gmail_message_id=gmail_message_id, status=status,
        degraded_classification=degraded_classification,
        validator_problems=validator_problems,
        return_created=return_created, create_alert=False,
    )


def add_review_with_alert(account_id, row_index, name, email, thread_id,
                          customer_reply, draft_reply, gmail_message_id=None,
                          status="pending", degraded_classification=False,
                          validator_problems=None, return_created=False):
    """Create a Gmail reply review and its delayed alert atomically."""
    return add_review(
        account_id, row_index, name, email, thread_id, customer_reply,
        draft_reply, gmail_message_id=gmail_message_id, status=status,
        degraded_classification=degraded_classification,
        validator_problems=validator_problems, return_created=return_created,
    )


def find_follow_up(account_id, source_draft_id):
    result = (
        _get_client().table(TABLE).select("*")
        .eq("account_id", account_id).eq("source_draft_id", source_draft_id)
        .eq("kind", "follow_up").limit(1).execute()
    )
    return result.data[0] if result.data else None


def add_follow_up(account_id, source_draft_id, row_index, name, email, thread_id,
                  draft_reply, validator_problems=None):
    existing = find_follow_up(account_id, source_draft_id)
    if existing:
        if existing["status"] == "dismissed":
            # The one scheduled follow-up per source is enforced by the partial
            # unique index reviews_one_follow_up_idx, so a dismissed review can
            # never be replaced with a fresh insert -- it must be resurrected.
            # Leaving it dismissed is the stranded-review bug: the source goes
            # queued, the review stays invisible, and the operator never sees
            # the follow-up. Reactivating with the current draft is safe because
            # a dismissed review has not been sent.
            result = _get_client().table(TABLE).update({
                "status": "pending",
                "row_index": row_index,
                "name": name,
                "email": email,
                "thread_id": thread_id,
                "draft_reply": draft_reply,
                "original_draft_reply": draft_reply or None,
                "validator_problems": "\n".join(validator_problems) if validator_problems else None,
            }).eq("account_id", account_id).eq("id", existing["id"]) \
                .eq("status", "dismissed").execute()
            if not result.data:
                current = find_follow_up(account_id, source_draft_id)
                if not current or current.get("status") != "pending":
                    raise RuntimeError("Follow-up review changed while it was being reactivated")
        return existing["id"]
    result = _get_client().table(TABLE).insert({
        "account_id": account_id,
        "source_draft_id": source_draft_id,
        "kind": "follow_up",
        "row_index": row_index,
        "name": name,
        "email": email,
        "thread_id": thread_id,
        "customer_reply": "",
        "draft_reply": draft_reply,
        "original_draft_reply": draft_reply or None,
        "status": "pending",
        "validator_problems": "\n".join(validator_problems) if validator_problems else None,
    }).execute()
    return result.data[0]["id"]


def dismiss_follow_up_for_source(account_id, source_draft_id):
    _get_client().table(TABLE).update({"status": "dismissed"}) \
        .eq("account_id", account_id).eq("source_draft_id", source_draft_id) \
        .eq("kind", "follow_up").eq("status", "pending").execute()


def follow_up_edit_metrics(account_id):
    result = (
        _get_client().table(TABLE)
        .select("status,draft_reply,original_draft_reply,rewritten")
        .eq("account_id", account_id).eq("kind", "follow_up").execute()
    )
    sent = [row for row in result.data if row.get("status") == "sent"]
    minimally_edited = 0
    for row in sent:
        original = (row.get("original_draft_reply") or "").strip()
        approved = (row.get("draft_reply") or "").strip()
        similarity = difflib.SequenceMatcher(None, original, approved).ratio() if original else 0
        if not row.get("rewritten") and similarity >= 0.8:
            minimally_edited += 1
    return {
        "drafted": len(result.data),
        "approved": len(sent),
        "minimally_edited": minimally_edited,
    }


def reply_edit_metrics(account_id):
    """Summarize approval and edit signals for inbound reply drafts.

    Reply drafts remain a full human-review lane. This endpoint is deliberately
    aggregate-only: it exposes counts for the operator's evidence dashboard,
    never the reply text or any cross-account training corpus.
    """
    result = _get_client().table(TABLE).select(
        "status,draft_reply,original_draft_reply,rewritten,kind"
    ).eq("account_id", account_id).execute()
    rows = [row for row in (result.data or []) if row.get("kind") != "follow_up"]
    sent = [row for row in rows if row.get("status") == "sent"]
    human_edited = 0
    model_rewritten = 0
    untouched = 0
    for row in sent:
        original = (row.get("original_draft_reply") or "").strip()
        approved = (row.get("draft_reply") or "").strip()
        if row.get("rewritten"):
            model_rewritten += 1
        elif original and original != approved:
            human_edited += 1
        else:
            untouched += 1
    return {
        "drafted": len(rows),
        "approved": len(sent),
        "human_edited": human_edited,
        "model_rewritten": model_rewritten,
        "untouched": untouched,
    }


def list_sent_edit_pairs(account_id, limit=100):
    """Return bounded reply edit pairs for account-scoped style signals."""
    result = (
        _get_client().table(TABLE)
        .select("draft_reply,original_draft_reply,rewritten,kind")
        .eq("account_id", account_id)
        .eq("status", "sent")
        .neq("kind", "follow_up")
        .limit(limit)
        .execute()
    )
    return result.data or []


def confirm_sender(account_id, review_id, draft_reply="", validator_problems=None):
    """Promotes a flagged (off-sheet sender) review to pending once the
    operator has confirmed the observed sender really is (or represents) the
    contact -- drafting is deferred to this point (see watch_replies.py), so
    this is also where the draft the operator will see gets attached.

    Scoped to `status = flagged` so this can never resurrect a review that
    has since been sent or dismissed. Returns whether a row actually matched
    -- callers must check this, since a review dismissed while a draft was
    being generated is a real, not-corrupting outcome: nothing to attach the
    draft to."""
    result = (
        _get_client().table(TABLE)
        .update({
            "status": "pending",
            "draft_reply": draft_reply,
            "original_draft_reply": draft_reply or None,
            # The draft is attached here rather than at insert, so this is where
            # its unresolved problems get recorded too. Written unconditionally,
            # including as None when the draft is clean -- a stale value from an
            # earlier attempt would mark a good draft suspect, or worse, leave a
            # bad one looking clean.
            "validator_problems": "\n".join(validator_problems) if validator_problems else None,
        })
        .eq("account_id", account_id)
        .eq("id", review_id)
        .eq("status", "flagged")
        .execute()
    )
    return bool(result.data)


# Reply drafts never enter the fast approval path. Unconditionally, and not as
# a policy that can be relaxed by a good eval result.
#
# The reason is provenance, not content. A reply is generated from a prompt that
# contains a stranger's arbitrary text -- the prospect's own email, which anyone
# we cold-emailed can write anything into. An outreach draft is generated from
# the sender's own settings and a row in the sheet they own. That difference is
# a property of where the draft came from, so it can be decided here, once,
# without inspecting a single character of output.
#
# The alternative -- detect the dangerous output and fast-lane the rest -- is a
# wall that cannot be finished. The thing needing detection is not "a URL", it
# is "any string some mail client will render as a tappable link", and that set
# is defined by software we do not control and cannot enumerate. Gmail linkifies
# a bare domain, so an attacker never has to type "https://" because Gmail types
# it for them on the recipient's screen. Mobile Gmail turns phone numbers into
# tel: handlers, which is a published exfiltration vector with no URL in it at
# all. Outlook, Apple Mail and every webmail client have their own rules and
# change them without telling us.
#
# _validate_reply is still worth having and is still improving, but it is
# defence in depth behind this line, never the thing holding it. Provenance does
# not degrade when Gmail ships a new linkifier.
FAST_LANE_ELIGIBLE = False


def lane(review=None):
    """Which approval surface a reply belongs on. Always the full-text one.

    Takes an argument it ignores on purpose: the signature matches
    drafts_db.lane so a caller cannot accidentally treat the two as
    interchangeable, and the ignored parameter is the point -- nothing about
    this particular reply can move it into the fast path."""
    return "full"


# Which statuses the operator is meant to SEE, and which they may ACT on.
# Deliberately two explicit allowlists rather than one rule like "anything not
# sent or dismissed", because visibility does not follow from workflow order and
# the two sets genuinely differ:
#
#   pending             visible, actionable   -- a draft waiting for approval
#   drafting            HIDDEN                -- detected, no draft attached yet.
#                                                Showing it is an empty card;
#                                                hiding it must not mean the
#                                                user is never told a reply
#                                                arrived, so the notification
#                                                fires on detection, not here.
#   flagged             visible, actionable   -- replied from an address not on
#                                                the sheet. Must be visible or
#                                                the confirm-sender question has
#                                                nowhere to be asked.
#   answered_elsewhere  HIDDEN                -- the operator already answered
#                                                from Gmail. The durable row is
#                                                retained for audit, but it is
#                                                resolved work and must not
#                                                inflate the open queue.
#   superseded          HIDDEN                -- a newer message on the thread
#                                                replaced it.
#   sent / dismissed    HIDDEN                -- handled.
#
# "drafting" and "superseded" are not produced yet (drafting: everything
# still drafts synchronously at detection time except the flagged case below,
# which skips straight to a review with no draft rather than a distinct
# hidden state; superseded: no code path marks an older review superseded by
# a newer one on the same thread today). Listed above so their eventual
# arrival is a data change, not a redesign of every query and endpoint that
# touches a review.
VISIBLE_STATUSES = ("pending", "flagged", "send_uncertain")
SENDABLE_STATUSES = ("pending",)


def list_pending_reviews(account_id):
    """Reviews the operator should see, oldest first. Named for its caller's
    intent rather than the status literal -- see VISIBLE_STATUSES."""
    result = (
        _get_client().table(TABLE)
        .select("*")
        .eq("account_id", account_id)
        .in_("status", list(VISIBLE_STATUSES))
        .order("created_at")
        .execute()
    )
    return result.data


def claim_send(account_id, review_id, sent_body=None):
    """Atomically claim a reply and freeze the exact body sent/reconciled."""
    payload = {"status": "sending"}
    if sent_body is not None:
        payload["draft_reply"] = sent_body
    result = (
        _get_client().table(TABLE).update(payload)
        .eq("account_id", account_id).eq("id", review_id)
        .eq("status", "pending").execute()
    )
    return bool(result.data)


def update_claimed_body(account_id, review_id, sent_body):
    """Persist edited follow-up copy after its source row owns the send."""
    result = (
        _get_client().table(TABLE).update({"draft_reply": sent_body})
        .eq("account_id", account_id).eq("id", review_id)
        .eq("status", "pending").execute()
    )
    return bool(result.data)


def release_send_claim(account_id, review_id):
    """Return a claimed reply to pending when no send attempt started."""
    result = (
        _get_client().table(TABLE).update({"status": "pending"})
        .eq("account_id", account_id).eq("id", review_id)
        .eq("status", "sending").execute()
    )
    return bool(result.data)


def mark_send_uncertain(account_id, review_id):
    """Keep an ambiguous Gmail result visible but permanently non-sendable."""
    client = _get_client()
    if hasattr(client, "rpc"):
        result = client.rpc("mark_review_send_uncertain_with_alert", {
            "p_account_id": account_id,
            "p_review_id": review_id,
        }).execute()
        return bool(result.data)
    result = (
        client.table(TABLE).update({"status": "send_uncertain"})
        .eq("account_id", account_id).eq("id", review_id)
        .eq("status", "sending").execute()
    )
    return bool(result.data)


def mark_send_uncertain_with_alert(account_id, review_id):
    result = _get_client().rpc("mark_review_send_uncertain_with_alert", {
        "p_account_id": account_id,
        "p_review_id": review_id,
    }).execute()
    return bool(result.data)


def list_reviews_stuck_in_send(account_id):
    """Normal replies trapped in the transient ``sending`` claim.

    ``sending`` sits between claim_send and (mark_sent | mark_send_uncertain).
    A crashed worker process or an exhausted retry budget in that window
    leaves the row invisible (not in VISIBLE_STATUSES) and permanently
    unsendable (not in SENDABLE_STATUSES) -- a delivered reply recorded
    nowhere. watch_replies._reconcile_review_sends owns recovering them;
    only reply-kind rows qualify because follow-up claims are fenced through
    drafts_db.follow_up_status instead."""
    result = (
        _get_client().table(TABLE)
        .select("*")
        .eq("account_id", account_id)
        .eq("kind", "reply")
        .eq("status", "sending")
        .order("created_at")
        .execute()
    )
    return result.data


def mark_stuck_send_visible(account_id, review_id):
    """Surface a stranded claim without weakening the duplicate fence.

    sending -> send_uncertain, CAS on sending exactly like its siblings. The
    card becomes a visible "Check Gmail" record the operator can trust but
    never resend -- strictly better than an invisible row, and never an
    automatic retry."""
    return mark_send_uncertain(account_id, review_id)


def mark_stuck_send_visible_with_alert(account_id, review_id):
    return mark_send_uncertain_with_alert(account_id, review_id)


def get_review(account_id, review_id):
    result = (
        _get_client().table(TABLE)
        .select("*")
        .eq("account_id", account_id)
        .eq("id", review_id)
        .execute()
    )
    return result.data[0] if result.data else None


def latest_reply_for_thread(account_id, thread_id):
    """Return the newest non-follow-up review for a Gmail thread."""
    try:
        result = (
            _get_client().table(TABLE).select("status,gmail_message_id,created_at")
            .eq("account_id", account_id).eq("thread_id", thread_id)
            .neq("kind", "follow_up").order("created_at", desc=True)
            .limit(1).execute()
        )
        return result.data[0] if result.data else None
    except Exception as e:
        print(f"Could not load reply status for {thread_id}: {e}")
        return None


def list_sent_bodies_for_thread(account_id, thread_id):
    """Bodies of every reply this account already SENT on a thread, newest
    last. Feeds watch_replies' echo guard: when the "newest message" on a
    thread is verbatim something we ourselves sent, it is not a customer
    reply -- drafting an answer to our own words is how one inbox ended up
    with a four-turn AI conversation signed by invented people. Sent bodies
    live in draft_reply (mark_sent overwrites the draft with what actually
    went out), so that column is the source."""
    try:
        result = (
            _get_client().table(TABLE)
            .select("draft_reply")
            .eq("account_id", account_id)
            .eq("thread_id", thread_id)
            .eq("status", "sent")
            .execute()
        )
    except Exception as e:
        print(f"Could not list sent bodies for echo guard on {thread_id}: {e}")
        return []
    return [
        (row.get("draft_reply") or "")
        for row in (result.data or [])
        if (row.get("draft_reply") or "").strip()
    ]


def mark_sent(account_id, review_id, sent_body):
    """Records the reply that actually went out. Leaves original_draft_reply
    alone on purpose -- the diff between the two columns is what the model got
    wrong about this sender's voice, and overwriting both would destroy it."""
    _get_client().table(TABLE).update(
        {
            "status": "sent",
            "draft_reply": sent_body,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("account_id", account_id).eq("id", review_id).execute()


def count_sent_last_24_hours(account_id):
    """Count reply and follow-up review sends in the rolling cap window."""
    since = datetime.now(timezone.utc) - timedelta(days=1)
    result = (
        _get_client().table(TABLE).select("id", count="exact")
        .eq("account_id", account_id).eq("status", "sent")
        .gte("sent_at", since.isoformat()).limit(1).execute()
    )
    return result.count or 0


def mark_rewritten(account_id, review_id):
    """Records that an AI rewrite touched this reply before it was sent, for
    Phase 6's future edit-diff corpus to exclude or label -- otherwise the
    diff between original_draft_reply and the sent copy is the model's own
    rewrite, not the human's voice.

    Deliberately a SEPARATE, best-effort write from mark_sent, never in the
    same update and never called before it -- a failed write there means a
    review stays "pending" after already being sent, and a second click from
    the operator is a second reply to a prospect. This column must never be
    able to cause that. Matches usage.record's own convention: never break
    the action being observed."""
    try:
        _get_client().table(TABLE).update({"rewritten": True}) \
            .eq("account_id", account_id).eq("id", review_id).execute()
    except Exception as e:
        print(f"Could not record the rewritten flag for review {review_id}: {e}")


def dismiss(account_id, review_id):
    _get_client().table(TABLE).update(
        {"status": "dismissed"}
    ).eq("account_id", account_id).eq("id", review_id).execute()
