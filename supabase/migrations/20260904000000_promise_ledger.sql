-- Promise Ledger foundation.
--
-- This migration preserves every existing commitment. The only data rewrite is
-- the user-facing state rename from `confirmed` (and the legacy `queued`
-- synonym) to `scheduled`. State changes and their audit records are committed
-- atomically through one protected RPC.

alter table public.commitments
    add column if not exists timezone text not null default 'UTC';

-- The legacy constraint does not allow `scheduled`, so it must be removed
-- before rewriting existing `confirmed`/`queued` rows.
alter table public.commitments
    drop constraint if exists commitments_status_check;

update public.commitments
set status = 'scheduled', updated_at = now()
where status in ('confirmed', 'queued');

alter table public.commitments
    add constraint commitments_status_check check (
        status in ('detected', 'scheduled', 'due', 'completed', 'dismissed', 'expired')
    );

create table if not exists public.commitment_events (
    id bigint generated always as identity primary key,
    account_id uuid not null references public.accounts(id) on delete cascade,
    commitment_id bigint not null references public.commitments(id) on delete cascade,
    transition_key text not null,
    from_status text,
    to_status text not null,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    unique (account_id, commitment_id, transition_key)
);

create index if not exists commitment_events_account_commitment_idx
    on public.commitment_events (account_id, commitment_id, created_at desc);

alter table public.commitment_events enable row level security;
revoke all on table public.commitment_events from public, anon, authenticated;
grant select on table public.commitment_events to service_role;

insert into public.commitment_events (
    account_id, commitment_id, transition_key, from_status, to_status, metadata, created_at
)
select
    account_id,
    id,
    'backfill:' || status,
    null,
    status,
    jsonb_strip_nulls(jsonb_build_object(
        'due_at', due_at,
        'reminder_at', reminder_at,
        'timezone', timezone,
        'migration', '20260904000000_promise_ledger'
    )),
    coalesce(updated_at, created_at, now())
from public.commitments
on conflict (account_id, commitment_id, transition_key) do nothing;

create or replace function public.transition_commitment_with_event(
    p_account_id uuid,
    p_commitment_id bigint,
    p_to_status text,
    p_transition_key text,
    p_due_at timestamptz default null,
    p_reminder_at timestamptz default null,
    p_timezone text default null,
    p_dismissal_reason text default null
) returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_row public.commitments%rowtype;
    v_from_status text;
begin
    if p_transition_key is null or btrim(p_transition_key) = '' then
        raise exception 'transition_key is required';
    end if;

    select * into v_row
    from public.commitments
    where account_id = p_account_id and id = p_commitment_id
    for update;

    if not found then
        return null;
    end if;

    -- A retried request returns the authoritative row without applying a
    -- second transition or creating a second audit event.
    if exists (
        select 1 from public.commitment_events
        where account_id = p_account_id
          and commitment_id = p_commitment_id
          and transition_key = p_transition_key
    ) then
        return to_jsonb(v_row);
    end if;

    v_from_status := v_row.status;
    if not (
        (v_from_status = 'detected' and p_to_status in ('scheduled', 'dismissed')) or
        (v_from_status = 'scheduled' and p_to_status in ('due', 'dismissed', 'expired')) or
        (v_from_status = 'due' and p_to_status in ('completed', 'dismissed'))
    ) then
        raise exception 'unsupported commitment transition: % -> %', v_from_status, p_to_status;
    end if;

    update public.commitments
    set status = p_to_status,
        due_at = coalesce(p_due_at, due_at),
        reminder_at = coalesce(p_reminder_at, reminder_at, p_due_at, due_at),
        timezone = coalesce(nullif(btrim(p_timezone), ''), timezone, 'UTC'),
        confirmed_at = case
            when p_to_status = 'scheduled' then coalesce(confirmed_at, now())
            else confirmed_at
        end,
        completed_at = case
            when p_to_status = 'completed' then coalesce(completed_at, now())
            else completed_at
        end,
        dismissal_reason = case
            when p_to_status = 'dismissed' then p_dismissal_reason
            else dismissal_reason
        end,
        updated_at = now()
    where account_id = p_account_id and id = p_commitment_id
    returning * into v_row;

    insert into public.commitment_events (
        account_id, commitment_id, transition_key, from_status, to_status, metadata
    ) values (
        p_account_id,
        p_commitment_id,
        p_transition_key,
        v_from_status,
        p_to_status,
        jsonb_strip_nulls(jsonb_build_object(
            'due_at', v_row.due_at,
            'reminder_at', v_row.reminder_at,
            'timezone', v_row.timezone,
            'dismissal_reason', v_row.dismissal_reason
        ))
    );

    return to_jsonb(v_row);
end;
$$;

revoke all on function public.transition_commitment_with_event(
    uuid, bigint, text, text, timestamptz, timestamptz, text, text
) from public, anon, authenticated;
grant execute on function public.transition_commitment_with_event(
    uuid, bigint, text, text, timestamptz, timestamptz, text, text
) to service_role;

-- Keep the worker's due transition and its alert atomic while moving to the
-- new `scheduled` state and recording the ledger event.
create or replace function public.mark_commitment_due_with_alert(
    p_account_id uuid,
    p_commitment_id bigint
) returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_result jsonb;
begin
    select public.transition_commitment_with_event(
        p_account_id,
        p_commitment_id,
        'due',
        'worker-due:' || p_commitment_id::text,
        null,
        null,
        null,
        null
    ) into v_result;

    if v_result is null or v_result->>'status' <> 'due' then
        return false;
    end if;

    insert into public.notification_outbox (
        account_id, event_type, source_type, source_id, dedupe_key
    ) values (
        p_account_id, 'promise_due', 'commitment', p_commitment_id::text,
        'promise-due:' || p_commitment_id::text
    ) on conflict (account_id, dedupe_key) do nothing;

    return true;
exception
    when raise_exception then
        -- A promise that is no longer scheduled is normal stale worker work.
        return false;
end;
$$;

revoke all on function public.mark_commitment_due_with_alert(uuid, bigint)
    from public, anon, authenticated;
grant execute on function public.mark_commitment_due_with_alert(uuid, bigint)
    to service_role;

create or replace function public.promise_ledger_contract()
returns jsonb
language sql
security definer
set search_path = ''
as $$
    select jsonb_build_object(
        'version', '20260904000000_promise_ledger',
        'timezone_column', exists (
            select 1 from information_schema.columns
            where table_schema = 'public' and table_name = 'commitments'
              and column_name = 'timezone'
        ),
        'events_table', to_regclass('public.commitment_events') is not null,
        'events_rls', coalesce((
            select relrowsecurity from pg_class
            where oid = to_regclass('public.commitment_events')
        ), false),
        'events_index', exists (
            select 1 from pg_indexes
            where schemaname = 'public'
              and indexname = 'commitment_events_account_commitment_idx'
        ),
        'events_unique', exists (
            select 1 from pg_constraint
            where conrelid = to_regclass('public.commitment_events')
              and conname = 'commitment_events_account_id_commitment_id_transition_key_key'
        ),
        'transition_rpc', to_regprocedure(
            'public.transition_commitment_with_event(uuid,bigint,text,text,timestamp with time zone,timestamp with time zone,text,text)'
        ) is not null,
        'due_rpc', to_regprocedure(
            'public.mark_commitment_due_with_alert(uuid,bigint)'
        ) is not null,
        'events_service_role_select', has_table_privilege(
            'service_role', 'public.commitment_events', 'SELECT'
        ),
        'client_execute_revoked',
            not has_function_privilege(
                'anon',
                'public.transition_commitment_with_event(uuid,bigint,text,text,timestamp with time zone,timestamp with time zone,text,text)',
                'EXECUTE'
            ) and not has_function_privilege(
                'authenticated',
                'public.transition_commitment_with_event(uuid,bigint,text,text,timestamp with time zone,timestamp with time zone,text,text)',
                'EXECUTE'
            ) and not has_function_privilege(
                'anon', 'public.mark_commitment_due_with_alert(uuid,bigint)', 'EXECUTE'
            ) and not has_function_privilege(
                'authenticated', 'public.mark_commitment_due_with_alert(uuid,bigint)', 'EXECUTE'
            ),
        'service_role_execute',
            has_function_privilege(
                'service_role',
                'public.transition_commitment_with_event(uuid,bigint,text,text,timestamp with time zone,timestamp with time zone,text,text)',
                'EXECUTE'
            ) and has_function_privilege(
                'service_role', 'public.mark_commitment_due_with_alert(uuid,bigint)', 'EXECUTE'
            )
    );
$$;

revoke all on function public.promise_ledger_contract() from public, anon, authenticated;
grant execute on function public.promise_ledger_contract() to service_role;

notify pgrst, 'reload schema';
