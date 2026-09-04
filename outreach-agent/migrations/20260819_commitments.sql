-- Conservative commitment candidates extracted from inbound replies.
-- Detection never sends mail. An operator must confirm a candidate before it
-- becomes scheduled work.
create table if not exists public.commitments (
    id bigint generated always as identity primary key,
    account_id uuid not null references public.accounts(id) on delete cascade,
    thread_id text not null,
    source_message_id text not null,
    actor text not null check (actor in ('contact', 'operator')),
    commitment_type text not null,
    action_text text not null,
    due_at timestamptz,
    due_text text not null default '',
    evidence text not null default '',
    confidence numeric not null check (confidence >= 0 and confidence <= 1),
    status text not null default 'detected'
        check (status in ('detected', 'confirmed', 'due', 'queued',
                          'completed', 'dismissed', 'expired')),
    reminder_at timestamptz,
    confirmed_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (account_id, source_message_id, commitment_type, action_text)
);

create index if not exists commitments_account_status_idx
    on public.commitments (account_id, status, reminder_at);

notify pgrst, 'reload schema';
