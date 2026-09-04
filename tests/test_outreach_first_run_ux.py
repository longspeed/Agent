from pathlib import Path


def test_outreach_first_run_explains_discovery_and_keeps_queue_hooks():
    source = (Path(__file__).resolve().parents[1] / "static" / "outreach.html").read_text(encoding="utf-8")

    # The first screen must answer the empty-queue question without sending
    # the operator back to documentation.
    assert "bounded window of recent Gmail Sent threads" in source
    assert "refreshes about every 30 minutes" in source
    assert "if (monitoring === 'not_connected')" in source
    assert "campaignPreview === null && !campaignsCache.length" not in source
    assert 'id="discovery-guide"' in source
    assert 'href="/settings"' in source

    # The redesign is presentation-only: existing runtime selectors remain
    # available for the live queue and its safety controls.
    for element_id in (
        "send-batch-btn",
        "campaign-safety",
        "reply-monitoring-status",
        "campaigns-body",
        "drafts-list",
        "commitments-list",
        "reviews-list",
    ):
        assert f'id="{element_id}"' in source


def test_outreach_motion_uses_native_reduced_motion_without_runtime_dependency():
    source = (Path(__file__).resolve().parents[1] / "static" / "outreach.html").read_text(encoding="utf-8")

    assert "gsap" not in source.lower()
    assert "prefers-reduced-motion: reduce" in source
    assert "initOutreachMotion" not in source
    assert 'href="/static/legacy-tailwind.css?v=20260904"' in source
