from pathlib import Path


def test_settings_save_shows_success_before_refresh_can_fail():
    # Regression: ISSUE-008 — successful settings saves had no visible confirmation
    # Found by /qa on 2026-08-20
    # Report: qa-reports/qa-report-daytonaproxy01-net-2026-08-20.md
    source = Path("app/src/SettingsPage.tsx").read_text(encoding="utf-8")
    save_block = source[source.index("if (res.ok) {"):source.index("    } else {", source.index("if (res.ok) {"))]

    assert "setSaveStatus('Saved.')" in save_block
    assert save_block.index("setSaveStatus('Saved.')") < save_block.index("await load()")
    assert "Saved, but the refreshed settings are unavailable" in save_block
