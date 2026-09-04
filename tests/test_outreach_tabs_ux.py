from pathlib import Path


SOURCE = (Path(__file__).resolve().parents[1] / "static" / "outreach.html").read_text(encoding="utf-8")


def test_outreach_tabs_are_query_backed_and_accessible():
    for tab in ("inbox", "promises", "follow-ups", "threads"):
        assert f"/outreach?tab={tab}" in SOURCE
        assert f'data-outreach-tab-panel="{tab}"' in SOURCE

    # Old deep links remain safe, but analytics and first-touch furniture no
    # longer return as a fifth operator-facing lane.
    assert "LEGACY_OUTREACH_TABS" in SOURCE
    assert "replies: 'threads'" in SOURCE
    assert "drafts: 'threads'" in SOURCE
    assert "more: 'threads'" in SOURCE
    assert "const OUTREACH_TABS = ['inbox', 'promises', 'follow-ups', 'threads'];" in SOURCE

    assert 'role="tablist"' in SOURCE
    assert 'role="tab"' in SOURCE
    assert 'aria-selected' in SOURCE
    assert 'aria-controls' in SOURCE
    assert "URLSearchParams" in SOURCE
    assert "history.pushState" in SOURCE
    assert "popstate" in SOURCE
    assert "OUTREACH_TABS" in SOURCE
    rail = SOURCE.split('role="tablist"', 1)[1].split('</nav>', 1)[0]
    assert 'data-outreach-tab-link="replies"' not in rail
    assert 'data-outreach-tab-link="drafts"' not in rail
    assert 'data-outreach-tab-link="more"' not in rail


def test_outreach_tab_panels_preserve_queue_hooks_and_split_review_kinds():
    for element_id in (
        "campaigns-body",
        "drafts-list",
        "commitments-list",
        "reviews-list",
        "follow-ups-list",
        "send-batch-btn",
        "check-replies-btn",
        "deal-view",
    ):
        assert f'id="{element_id}"' in SOURCE

    assert "review.kind !== 'follow_up'" in SOURCE
    assert "review.kind === 'follow_up'" in SOURCE
    assert ".outreach-tab-panel[hidden]" in SOURCE
    assert "overflow-x:auto" in SOURCE
    assert "min-height:44px" in SOURCE
    assert "Gmail or Gemini remains the reply surface" in SOURCE
    assert "one follow-up reminder" in SOURCE


def test_outreach_has_one_copy_of_action_hooks():
    for element_id in ("send-batch-btn", "check-replies-btn", "reviews-list", "follow-ups-list"):
        assert SOURCE.count(f'id="{element_id}"') == 1


def test_reply_queue_exposes_safe_ignore_action_without_gmail_delete():
    assert "Ignore reply" in SOURCE
    assert "Ignoring…" in SOURCE
    assert "removed from Sendkeep, but the original Gmail message will not be deleted" in SOURCE
    assert "/api/outreach/replies/${r.id}/dismiss" in SOURCE
    assert "/api/outreach/replies/${r.id}/delete" not in SOURCE
    assert "Cancel follow-up" in SOURCE
