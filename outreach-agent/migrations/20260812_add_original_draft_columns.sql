-- Existing projects created before the edit-diff capture feature are missing
-- these columns even though fresh installs include them in the base schema.
-- Idempotent so it is safe to run on both old and current projects.
alter table public.outreach_drafts
    add column if not exists original_subject text;

alter table public.outreach_drafts
    add column if not exists original_body text;

-- Make the new columns visible immediately to PostgREST/Supabase REST clients.
notify pgrst, 'reload schema';
