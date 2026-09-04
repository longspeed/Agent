-- Gmail threads remain watched even when their Google Sheet mirror row changes.
create table if not exists public.tracked_threads (
    id bigint generated always as identity primary key,
    account_id uuid not null references public.accounts(id) on delete cascade,
    thread_id text not null,
    email text not null default '',
    name text not null default '',
    company text not null default '',
    row_index integer,
    source text not null,
    status text not null default 'active',
    last_contact_message_id text,
    last_checked_at timestamptz,
    last_error text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (account_id, thread_id)
);

create index if not exists tracked_threads_account_status_idx
    on public.tracked_threads (account_id, status);

notify pgrst, 'reload schema';
