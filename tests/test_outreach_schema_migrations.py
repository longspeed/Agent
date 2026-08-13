from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "outreach-agent"
    / "migrations"
    / "20260812_add_original_draft_columns.sql"
)


def test_existing_projects_can_add_frozen_outreach_copy_columns():
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "add column if not exists original_subject text" in sql
    assert "add column if not exists original_body text" in sql
    assert "notify pgrst, 'reload schema'" in sql
