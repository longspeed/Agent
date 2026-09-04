-- Pilot alert delivery: durable, tenant-scoped, and safe to retry.
-- Product state and its alert are written by the same transaction RPC. The
-- email provider is deliberately outside that transaction and is protected by
-- a lease, claim token, and provider idempotency key.

create table if not exists public.notification_outbox (
    id uuid primary key default gen_random_uuid(),
    account_id uuid not null references public.accounts(id) on delete cascade,
    event_type text not null,
    source_type text not null,
    source_id text not null,
    dedupe_key text not null,
    status text not null default 'pending',
    available_at timestamptz not null default now(),
    claim_token uuid,
    claim_expires_at timestamptz,
    attempt_count integer not null default 0,
    first_attempt_at timestamptz,
    provider_message_id text,
    last_error_code text,
    delivered_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (account_id, dedupe_key),
    constraint notification_outbox_event_type_check check (
        event_type in (
            'reply_unhandled', 'promise_due', 'send_uncertain',
            'follow_up_send_uncertain',
            'gmail_disconnected', 'gmail_recovered', 'test_alert'
        )
    ),
    constraint notification_outbox_source_type_check check (
        source_type in ('review', 'commitment', 'account', 'test')
    ),
    constraint notification_outbox_status_check check (
        status in (
            'pending', 'sending', 'retry', 'delivered', 'cancelled',
            'failed', 'delivery_uncertain'
        )
    )
);

create index if not exists notification_outbox_ready_idx
    on public.notification_outbox (available_at, created_at)
    where status in ('pending', 'retry');
create index if not exists notification_outbox_account_idx
    on public.notification_outbox (account_id, created_at desc);

alter table public.notification_outbox enable row level security;
revoke all on table public.notification_outbox from public, anon, authenticated;
grant all on table public.notification_outbox to service_role;

create or replace function public.queue_reply_review_with_alert(
    p_account_id uuid,
    p_row_index integer,
    p_name text,
    p_email text,
    p_thread_id text,
    p_customer_reply text,
    p_draft_reply text,
    p_gmail_message_id text,
    p_status text,
    p_degraded_classification boolean,
    p_validator_problems text
) returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_review_id bigint;
    v_created boolean := false;
begin
    if p_status not in ('pending', 'flagged', 'answered_elsewhere') then
        raise exception 'unsupported review status';
    end if;

    insert into public.reviews (
        account_id, row_index, name, email, thread_id, customer_reply,
        draft_reply, original_draft_reply, gmail_message_id, status,
        degraded_classification, validator_problems
    ) values (
        p_account_id, p_row_index, coalesce(p_name, ''), coalesce(p_email, ''),
        coalesce(p_thread_id, ''), coalesce(p_customer_reply, ''),
        coalesce(p_draft_reply, ''), nullif(p_draft_reply, ''),
        p_gmail_message_id, p_status, coalesce(p_degraded_classification, false),
        p_validator_problems
    )
    on conflict (account_id, thread_id, gmail_message_id)
        where gmail_message_id is not null
    do nothing
    returning id into v_review_id;

    if v_review_id is not null then
        v_created := true;
    else
        select id into v_review_id
        from public.reviews
        where account_id = p_account_id
          and thread_id = p_thread_id
          and gmail_message_id = p_gmail_message_id
        limit 1;
    end if;

    if v_created and p_status in ('pending', 'flagged') then
        insert into public.notification_outbox (
            account_id, event_type, source_type, source_id, dedupe_key,
            available_at
        ) values (
            p_account_id, 'reply_unhandled', 'review', v_review_id::text,
            'reply:' || v_review_id::text, now() + interval '2 hours'
        ) on conflict (account_id, dedupe_key) do nothing;
    end if;

    return jsonb_build_object('id', v_review_id, 'created', v_created);
end;
$$;

create or replace function public.mark_commitment_due_with_alert(
    p_account_id uuid,
    p_commitment_id bigint
) returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_changed boolean;
begin
    update public.commitments
    set status = 'due', updated_at = now()
    where account_id = p_account_id and id = p_commitment_id
      and status = 'confirmed'
    returning true into v_changed;

    if coalesce(v_changed, false) then
        insert into public.notification_outbox (
            account_id, event_type, source_type, source_id, dedupe_key
        ) values (
            p_account_id, 'promise_due', 'commitment', p_commitment_id::text,
            'promise-due:' || p_commitment_id::text
        ) on conflict (account_id, dedupe_key) do nothing;
    end if;
    return coalesce(v_changed, false);
end;
$$;

create or replace function public.mark_review_send_uncertain_with_alert(
    p_account_id uuid,
    p_review_id bigint
) returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_changed boolean;
begin
    update public.reviews
    set status = 'send_uncertain'
    where account_id = p_account_id and id = p_review_id
      and status = 'sending'
    returning true into v_changed;

    if coalesce(v_changed, false) then
        insert into public.notification_outbox (
            account_id, event_type, source_type, source_id, dedupe_key
        ) values (
            p_account_id, 'send_uncertain', 'review', p_review_id::text,
            'send-uncertain:' || p_review_id::text
        ) on conflict (account_id, dedupe_key) do nothing;
    end if;
    return coalesce(v_changed, false);
end;
$$;

create or replace function public.claim_follow_up_send_with_alert(
    p_account_id uuid,
    p_draft_id bigint
) returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_review_id bigint;
    v_changed boolean;
begin
    select id into v_review_id
    from public.reviews
    where account_id = p_account_id
      and source_draft_id = p_draft_id
      and kind = 'follow_up'
      and status = 'pending'
    order by id desc
    limit 1
    for update;

    if v_review_id is null then
        return false;
    end if;

    update public.outreach_drafts
    set follow_up_status = 'sending'
    where account_id = p_account_id and id = p_draft_id
      and follow_up_status = 'queued'
    returning true into v_changed;

    if coalesce(v_changed, false) then
        -- The alert is created with the irreversible send claim, not after the
        -- Gmail request. A successful send or reconciliation makes it stale
        -- before delivery; a crash or ambiguous result leaves it actionable.
        insert into public.notification_outbox (
            account_id, event_type, source_type, source_id, dedupe_key,
            available_at
        ) values (
            p_account_id, 'follow_up_send_uncertain', 'review', v_review_id::text,
            'follow-up-send-uncertain:' || v_review_id::text,
            now() + interval '5 minutes'
        ) on conflict (account_id, dedupe_key) do nothing;
    end if;

    return coalesce(v_changed, false);
end;
$$;

create or replace function public.record_account_monitoring_state(
    p_account_id uuid,
    p_error text,
    p_alert_disconnected boolean default false
) returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_previous_error text;
    v_disconnect_id uuid;
    v_disconnect_status text;
begin
    select last_error into v_previous_error
    from public.accounts where id = p_account_id for update;

    update public.accounts
    set worker_heartbeat_at = now(), last_error = nullif(left(coalesce(p_error, ''), 500), '')
    where id = p_account_id;

    if p_alert_disconnected and p_error is not null and btrim(p_error) <> '' then
        insert into public.notification_outbox (
            account_id, event_type, source_type, source_id, dedupe_key,
            available_at
        ) values (
            p_account_id, 'gmail_disconnected', 'account', p_account_id::text,
            'gmail-disconnected:open', now() + interval '30 minutes'
        ) on conflict (account_id, dedupe_key) do update
          set updated_at = now()
          where public.notification_outbox.status in ('pending', 'retry');
    elsif p_error is null or btrim(p_error) = '' then
        select id, status into v_disconnect_id, v_disconnect_status
        from public.notification_outbox
        where account_id = p_account_id
          and dedupe_key = 'gmail-disconnected:open'
        limit 1
        for update;

        update public.notification_outbox
        set status = 'cancelled', claim_token = null, claim_expires_at = null,
            updated_at = now()
        where account_id = p_account_id
          and dedupe_key = 'gmail-disconnected:open'
          and status in ('pending', 'retry', 'sending');

        -- Free the stable "open incident" key for a future disconnect. The
        -- provider idempotency key is the immutable event id, so rotating this
        -- database-only dedupe key cannot cause another send.
        update public.notification_outbox
        set dedupe_key = 'gmail-disconnected:closed:' || id::text,
            updated_at = now()
        where id = v_disconnect_id;

        if v_previous_error is not null
           and v_disconnect_status in ('delivered', 'sending') then
            insert into public.notification_outbox (
                account_id, event_type, source_type, source_id, dedupe_key,
                available_at
            ) values (
                p_account_id, 'gmail_recovered', 'account', p_account_id::text,
                'gmail-recovered:' || v_disconnect_id::text,
                case when v_disconnect_status = 'sending'
                    then now() + interval '1 minute' else now() end
            ) on conflict (account_id, dedupe_key) do nothing;
        end if;
    end if;
end;
$$;

create or replace function public.queue_test_notification(p_account_id uuid)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_id uuid := gen_random_uuid();
begin
    insert into public.notification_outbox (
        id, account_id, event_type, source_type, source_id, dedupe_key
    ) values (
        v_id, p_account_id, 'test_alert', 'test', v_id::text,
        'test:' || v_id::text
    );
    return v_id;
end;
$$;

create or replace function public.claim_notification_batch(
    p_limit integer default 25,
    p_lease_seconds integer default 60,
    p_account_id uuid default null
) returns setof public.notification_outbox
language plpgsql
security definer
set search_path = ''
as $$
begin
    -- Never issue a new provider request after its idempotency window. An
    -- operator must reconcile these rare rows instead of risking a duplicate.
    update public.notification_outbox
    set status = 'delivery_uncertain', updated_at = now(),
        last_error_code = 'idempotency_window_expired'
    where status = 'retry'
      and first_attempt_at < now() - interval '23 hours'
      and (p_account_id is null or account_id = p_account_id);

    return query
    with candidates as (
        select id
        from public.notification_outbox
        where status in ('pending', 'retry')
          and available_at <= now()
          and (p_account_id is null or account_id = p_account_id)
        order by available_at, created_at
        for update skip locked
        limit greatest(1, least(coalesce(p_limit, 25), 100))
    )
    update public.notification_outbox o
    set status = 'sending',
        claim_token = gen_random_uuid(),
        claim_expires_at = now() + make_interval(secs => greatest(15, least(coalesce(p_lease_seconds, 60), 600))),
        attempt_count = attempt_count + 1,
        first_attempt_at = coalesce(first_attempt_at, now()),
        updated_at = now()
    from candidates c
    where o.id = c.id
    returning o.*;
end;
$$;

create or replace function public.claim_notification_event(
    p_account_id uuid,
    p_event_id uuid,
    p_lease_seconds integer default 60
) returns setof public.notification_outbox
language plpgsql
security definer
set search_path = ''
as $$
begin
    update public.notification_outbox
    set status = 'delivery_uncertain', updated_at = now(),
        last_error_code = 'idempotency_window_expired'
    where account_id = p_account_id and id = p_event_id
      and status = 'retry'
      and first_attempt_at < now() - interval '23 hours';

    return query
    update public.notification_outbox
    set status = 'sending',
        claim_token = gen_random_uuid(),
        claim_expires_at = now() + make_interval(secs => greatest(15, least(coalesce(p_lease_seconds, 60), 600))),
        attempt_count = attempt_count + 1,
        first_attempt_at = coalesce(first_attempt_at, now()),
        updated_at = now()
    where account_id = p_account_id and id = p_event_id
      and status in ('pending', 'retry')
      and available_at <= now()
    returning *;
end;
$$;

create or replace function public.finalize_notification_delivery(
    p_event_id uuid,
    p_claim_token uuid,
    p_outcome text,
    p_provider_message_id text default null,
    p_error_code text default null,
    p_retry_at timestamptz default null
) returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_changed boolean;
begin
    if p_outcome not in ('delivered', 'retry', 'cancelled', 'failed', 'delivery_uncertain') then
        raise exception 'unsupported notification outcome';
    end if;

    update public.notification_outbox
    set status = p_outcome,
        provider_message_id = coalesce(p_provider_message_id, provider_message_id),
        last_error_code = nullif(left(coalesce(p_error_code, ''), 80), ''),
        available_at = case when p_outcome = 'retry'
            then coalesce(p_retry_at, now() + interval '5 minutes') else available_at end,
        delivered_at = case when p_outcome = 'delivered' then now() else delivered_at end,
        claim_token = null,
        claim_expires_at = null,
        updated_at = now()
    where id = p_event_id and claim_token = p_claim_token and status = 'sending'
    returning true into v_changed;
    return coalesce(v_changed, false);
end;
$$;

create or replace function public.recover_expired_notification_claims(
    p_account_id uuid default null
)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_count integer;
begin
    update public.notification_outbox
    set status = case
            when first_attempt_at < now() - interval '23 hours'
                then 'delivery_uncertain'
            else 'retry'
        end,
        available_at = now(), claim_token = null, claim_expires_at = null,
        last_error_code = 'lease_expired', updated_at = now()
    where status = 'sending' and claim_expires_at < now()
      and (p_account_id is null or account_id = p_account_id);
    get diagnostics v_count = row_count;
    return v_count;
end;
$$;

create or replace function public.notification_outbox_contract()
returns jsonb
language sql
security definer
set search_path = pg_catalog, public
as $$
    select jsonb_build_object(
        'version', '20260830000000_notification_outbox',
        'table', to_regclass('public.notification_outbox') is not null,
        'rls', coalesce((select relrowsecurity from pg_class where oid = to_regclass('public.notification_outbox')), false),
        'ready_index', exists (select 1 from pg_indexes where schemaname = 'public' and indexname = 'notification_outbox_ready_idx'),
        'dedupe_constraint', exists (select 1 from pg_constraint where conrelid = to_regclass('public.notification_outbox') and conname = 'notification_outbox_account_id_dedupe_key_key'),
        'status_constraint', exists (select 1 from pg_constraint where conrelid = to_regclass('public.notification_outbox') and conname = 'notification_outbox_status_check'),
        'event_constraint', exists (select 1 from pg_constraint where conrelid = to_regclass('public.notification_outbox') and conname = 'notification_outbox_event_type_check'),
        'source_constraint', exists (select 1 from pg_constraint where conrelid = to_regclass('public.notification_outbox') and conname = 'notification_outbox_source_type_check'),
        'rpcs',
            to_regprocedure('public.queue_reply_review_with_alert(uuid,integer,text,text,text,text,text,text,text,boolean,text)') is not null
            and to_regprocedure('public.mark_commitment_due_with_alert(uuid,bigint)') is not null
            and to_regprocedure('public.mark_review_send_uncertain_with_alert(uuid,bigint)') is not null
            and to_regprocedure('public.claim_follow_up_send_with_alert(uuid,bigint)') is not null
            and to_regprocedure('public.record_account_monitoring_state(uuid,text,boolean)') is not null
            and to_regprocedure('public.queue_test_notification(uuid)') is not null
            and to_regprocedure('public.claim_notification_batch(integer,integer,uuid)') is not null
            and to_regprocedure('public.claim_notification_event(uuid,uuid,integer)') is not null
            and to_regprocedure('public.finalize_notification_delivery(uuid,uuid,text,text,text,timestamp with time zone)') is not null
            and to_regprocedure('public.recover_expired_notification_claims(uuid)') is not null,
        'service_role_execute',
            has_function_privilege('service_role', 'public.claim_notification_batch(integer,integer,uuid)', 'EXECUTE')
            and has_function_privilege('service_role', 'public.claim_notification_event(uuid,uuid,integer)', 'EXECUTE')
            and has_function_privilege('service_role', 'public.finalize_notification_delivery(uuid,uuid,text,text,text,timestamp with time zone)', 'EXECUTE'),
        'client_execute_revoked',
            not has_function_privilege('anon', 'public.claim_notification_batch(integer,integer,uuid)', 'EXECUTE')
            and not has_function_privilege('authenticated', 'public.claim_notification_batch(integer,integer,uuid)', 'EXECUTE')
            and not has_function_privilege('anon', 'public.claim_notification_event(uuid,uuid,integer)', 'EXECUTE')
            and not has_function_privilege('authenticated', 'public.claim_notification_event(uuid,uuid,integer)', 'EXECUTE')
    );
$$;

do $$
declare
    signature text;
begin
    foreach signature in array array[
        'public.queue_reply_review_with_alert(uuid,integer,text,text,text,text,text,text,text,boolean,text)',
        'public.mark_commitment_due_with_alert(uuid,bigint)',
        'public.mark_review_send_uncertain_with_alert(uuid,bigint)',
        'public.claim_follow_up_send_with_alert(uuid,bigint)',
        'public.record_account_monitoring_state(uuid,text,boolean)',
        'public.queue_test_notification(uuid)',
        'public.claim_notification_batch(integer,integer,uuid)',
        'public.claim_notification_event(uuid,uuid,integer)',
        'public.finalize_notification_delivery(uuid,uuid,text,text,text,timestamp with time zone)',
        'public.recover_expired_notification_claims(uuid)',
        'public.notification_outbox_contract()'
    ] loop
        execute format('revoke all on function %s from public, anon, authenticated', signature);
        execute format('grant execute on function %s to service_role', signature);
    end loop;
end $$;

notify pgrst, 'reload schema';
