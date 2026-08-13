-- One manually-approved follow-up per sent outreach thread.
alter table public.accounts
    add column if not exists follow_up_delay_days integer not null default 3
    check (follow_up_delay_days between 1 and 14);

alter table public.outreach_drafts
    add column if not exists thread_id text,
    add column if not exists follow_up_due_at timestamptz,
    add column if not exists follow_up_status text,
    add column if not exists follow_up_cancel_reason text,
    add column if not exists follow_up_sent_at timestamptz,
    add column if not exists follow_up_replied_at timestamptz;

create index if not exists outreach_follow_up_status_idx
    on public.outreach_drafts (account_id, follow_up_status, follow_up_due_at);

alter table public.reviews
    add column if not exists kind text not null default 'reply',
    add column if not exists source_draft_id bigint
        references public.outreach_drafts(id) on delete cascade;

create unique index if not exists reviews_one_follow_up_idx
    on public.reviews (account_id, source_draft_id) where kind = 'follow_up';

notify pgrst, 'reload schema';
