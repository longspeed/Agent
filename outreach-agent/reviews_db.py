"""Review queue in Supabase, scoped per account. Every query filters on
account_id — combined with RLS on the table, one tenant can never see
another's reviews."""
from supabase import create_client

from config import SUPABASE_URL, SUPABASE_SECRET_KEY

TABLE = "reviews"

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
               degraded_classification=False):
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
        return existing_id

    result = _get_client().table(TABLE).insert({
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
    }).execute()
    return result.data[0]["id"]


def confirm_sender(account_id, review_id, draft_reply=""):
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
        })
        .eq("account_id", account_id)
        .eq("id", review_id)
        .eq("status", "flagged")
        .execute()
    )
    return bool(result.data)


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
#   answered_elsewhere  visible, NOT sendable -- the operator already answered
#                                                from Gmail. Worth knowing,
#                                                nothing to send. Dismissible.
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
VISIBLE_STATUSES = ("pending", "flagged", "answered_elsewhere")
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


def get_review(account_id, review_id):
    result = (
        _get_client().table(TABLE)
        .select("*")
        .eq("account_id", account_id)
        .eq("id", review_id)
        .execute()
    )
    return result.data[0] if result.data else None


def mark_sent(account_id, review_id, sent_body):
    """Records the reply that actually went out. Leaves original_draft_reply
    alone on purpose -- the diff between the two columns is what the model got
    wrong about this sender's voice, and overwriting both would destroy it."""
    _get_client().table(TABLE).update(
        {"status": "sent", "draft_reply": sent_body}
    ).eq("account_id", account_id).eq("id", review_id).execute()


def dismiss(account_id, review_id):
    _get_client().table(TABLE).update(
        {"status": "dismissed"}
    ).eq("account_id", account_id).eq("id", review_id).execute()
