-- Durable worker liveness and account-level event history.
create table if not exists public.worker_runs (
    id bigint generated always as identity primary key,
    host_id text not null default '',
    status text not null,
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    checked_accounts integer not null default 0,
    skipped_accounts integer not null default 0,
    failed_accounts integer not null default 0,
    elapsed_seconds numeric not null default 0,
    error text,
    created_at timestamptz not null default now()
);

create index if not exists worker_runs_started_idx
    on public.worker_runs (started_at desc);

create table if not exists public.worker_events (
    id bigint generated always as identity primary key,
    run_id bigint references public.worker_runs(id) on delete cascade,
    account_id uuid references public.accounts(id) on delete cascade,
    event_type text not null,
    status text not null,
    details jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create index if not exists worker_events_account_created_idx
    on public.worker_events (account_id, created_at desc);

notify pgrst, 'reload schema';
