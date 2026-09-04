-- Hardening for the durable reply-desk tables.
-- These tables are accessed by the backend with the service role only. They
-- must not be exposed through Supabase's browser-facing API roles.
do $$
begin
    if to_regclass('public.reviews') is not null then
        alter table public.reviews add column if not exists sent_at timestamptz;
    end if;
    if to_regclass('public.worker_events') is not null then
        alter table public.worker_events add column if not exists dedupe_key text;
        create unique index if not exists worker_events_dedupe_idx
            on public.worker_events (account_id, event_type, dedupe_key)
            where dedupe_key is not null;
    end if;
    if to_regclass('public.commitments') is not null then
        alter table public.commitments enable row level security;
        revoke all on table public.commitments from anon, authenticated;
    end if;
    if to_regclass('public.tracked_threads') is not null then
        alter table public.tracked_threads enable row level security;
        revoke all on table public.tracked_threads from anon, authenticated;
    end if;
    if to_regclass('public.worker_runs') is not null then
        alter table public.worker_runs enable row level security;
        revoke all on table public.worker_runs from anon, authenticated;
    end if;
    if to_regclass('public.worker_events') is not null then
        alter table public.worker_events enable row level security;
        revoke all on table public.worker_events from anon, authenticated;
    end if;
end $$;

notify pgrst, 'reload schema';
