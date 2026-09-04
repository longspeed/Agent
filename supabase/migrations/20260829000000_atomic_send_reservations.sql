-- Serialize every irreversible Gmail send against the rolling account cap.
create table if not exists public.send_reservations (
    id bigint generated always as identity primary key,
    account_id uuid not null references public.accounts(id) on delete cascade,
    operation_key text not null,
    status text not null default 'reserved',
    created_at timestamptz not null default now(),
    unique (account_id, operation_key),
    constraint send_reservations_status_check
        check (status in ('reserved', 'completed'))
);

create index if not exists send_reservations_active_idx
    on public.send_reservations (account_id, created_at)
    where status = 'reserved';

alter table public.send_reservations enable row level security;
revoke all on table public.send_reservations from public, anon, authenticated;
grant all on table public.send_reservations to service_role;
grant usage, select on sequence public.send_reservations_id_seq to service_role;

create or replace function public.try_reserve_send(
    p_account_id uuid,
    p_operation_key text,
    p_limit integer
) returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_used bigint;
begin
    if p_limit < 1 or p_operation_key is null or btrim(p_operation_key) = '' then
        return false;
    end if;

    -- One account, one cap decision at a time, across every web/worker process.
    perform pg_advisory_xact_lock(hashtextextended(p_account_id::text, 0));

    if exists (
        select 1 from public.send_reservations
        where account_id = p_account_id and operation_key = p_operation_key
    ) then
        return false;
    end if;

    select
        (select count(*) from public.outreach_drafts
         where account_id = p_account_id and status = 'sent'
           and sent_at >= now() - interval '24 hours')
      + (select count(*) from public.reviews
         where account_id = p_account_id and status = 'sent'
           and sent_at >= now() - interval '24 hours')
      + (select count(*) from public.send_reservations
         where account_id = p_account_id and status = 'reserved'
           and created_at >= now() - interval '24 hours')
      into v_used;

    if v_used >= p_limit then
        return false;
    end if;

    insert into public.send_reservations(account_id, operation_key)
    values (p_account_id, p_operation_key);
    return true;
exception
    when unique_violation then
        return false;
end;
$$;

revoke all on function public.try_reserve_send(uuid, text, integer)
    from public, anon, authenticated;
grant execute on function public.try_reserve_send(uuid, text, integer)
    to service_role;

-- A row in `sending` or `send_uncertain` still owns this sheet row.  Allowing
-- a new pending draft there would re-open the duplicate-send path.
drop index if exists public.outreach_drafts_pending_row_idx;
create unique index outreach_drafts_active_row_idx
    on public.outreach_drafts (account_id, row_index)
    where status in ('pending', 'sending', 'send_uncertain');

-- Stripe event ordering and subscription binding. A delayed cancellation for
-- an old subscription must not downgrade a newer paid subscription.
alter table public.accounts
    add column if not exists stripe_subscription_id text,
    add column if not exists stripe_status text,
    add column if not exists stripe_last_event_created bigint,
    add column if not exists stripe_last_event_id text,
    add column if not exists stripe_last_event_rank integer not null default 0;

create or replace function public.apply_stripe_plan_event(
    p_account_id uuid,
    p_plan text,
    p_subscription_id text,
    p_status text,
    p_event_created bigint,
    p_event_id text,
    p_event_rank integer
) returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
    current_row public.accounts%rowtype;
begin
    perform pg_advisory_xact_lock(hashtextextended(p_account_id::text, 1));
    select * into current_row from public.accounts where id = p_account_id for update;
    if not found then return 'account_missing'; end if;
    if current_row.stripe_last_event_id = p_event_id then return 'duplicate'; end if;
    if current_row.stripe_last_event_created is not null and (
        p_event_created < current_row.stripe_last_event_created or
        (p_event_created = current_row.stripe_last_event_created and
         p_event_rank < current_row.stripe_last_event_rank)
    ) then return 'stale'; end if;
    if current_row.stripe_subscription_id is not null
       and p_subscription_id is not null
       and current_row.stripe_subscription_id <> p_subscription_id
       and p_event_rank < 20 then
        return 'subscription_mismatch';
    end if;

    update public.accounts set
        plan = p_plan,
        stripe_subscription_id = coalesce(p_subscription_id, stripe_subscription_id),
        stripe_status = p_status,
        stripe_last_event_created = p_event_created,
        stripe_last_event_id = p_event_id,
        stripe_last_event_rank = p_event_rank
    where id = p_account_id;
    return 'applied';
end;
$$;

revoke all on function public.apply_stripe_plan_event(uuid, text, text, text, bigint, text, integer)
    from public, anon, authenticated;
grant execute on function public.apply_stripe_plan_event(uuid, text, text, text, bigint, text, integer)
    to service_role;
