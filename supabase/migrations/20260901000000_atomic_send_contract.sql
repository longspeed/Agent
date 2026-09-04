begin;

-- A first-touch email is a one-time operation. Continuing an existing
-- relationship belongs on its Gmail thread, never in a second Sheet row.
-- Fail closed on historical duplicates; production cleanup requires an
-- explicit operator decision and must never happen inside a migration.
do $$
begin
    if exists (
        select 1
        from public.outreach_drafts
        where status in ('pending', 'sending', 'send_uncertain', 'sent')
          and btrim(email) <> ''
        group by account_id, lower(btrim(email))
        having count(*) > 1
    ) then
        raise exception using
            errcode = '23505',
            message = 'Cannot enforce one first-touch per recipient: duplicate active or sent recipients exist';
    end if;
end $$;

create unique index if not exists outreach_drafts_first_touch_email_unique_idx
    on public.outreach_drafts (account_id, lower(btrim(email)))
    where status in ('pending', 'sending', 'send_uncertain', 'sent')
      and btrim(email) <> '';

-- Keep the runtime duplicate check identical to the index predicate. This is
-- deliberately service-role-only: browser clients must not be able to probe
-- whether another account has contacted an address.
create or replace function public.has_first_touch_conflict(
    p_account_id uuid,
    p_email text,
    p_exclude_draft_id bigint default null
)
returns boolean
language sql
security definer
set search_path = pg_catalog, public
as $$
    select exists (
        select 1
        from public.outreach_drafts
        where account_id = p_account_id
          and lower(btrim(email)) = lower(btrim(coalesce(p_email, '')))
          and status in ('pending', 'sending', 'send_uncertain', 'sent')
          and (p_exclude_draft_id is null or id <> p_exclude_draft_id)
    );
$$;

create or replace function public.atomic_send_contract()
returns jsonb
language sql
security definer
set search_path = pg_catalog, public
as $$
    select jsonb_build_object(
        'version', '20260901000000_atomic_send_contract',
        'table', to_regclass('public.send_reservations') is not null,
        'rls', coalesce((
            select relrowsecurity
            from pg_class
            where oid = to_regclass('public.send_reservations')
        ), false),
        'client_table_access_revoked',
            coalesce(not has_table_privilege(
                'anon', to_regclass('public.send_reservations'),
                'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
            ), false)
            and coalesce(not has_table_privilege(
                'authenticated', to_regclass('public.send_reservations'),
                'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
            ), false),
        'operation_unique', exists (
            select 1
            from pg_constraint
            where conrelid = to_regclass('public.send_reservations')
              and contype = 'u'
              and pg_get_constraintdef(oid) ilike '%(account_id, operation_key)%'
        ),
        'status_constraint', exists (
            select 1
            from pg_constraint
            where conrelid = to_regclass('public.send_reservations')
              and conname = 'send_reservations_status_check'
        ),
        'active_index', exists (
            select 1 from pg_indexes
            where schemaname = 'public'
              and indexname = 'send_reservations_active_idx'
              and indexdef ilike '%(account_id, created_at)%'
              and indexdef ilike '%status = %reserved%'
        ),
        'active_row_index', exists (
            select 1
            from pg_index i
            join pg_class c on c.oid = i.indexrelid
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'public'
              and c.relname = 'outreach_drafts_active_row_idx'
              and i.indisunique
              and pg_get_indexdef(i.indexrelid) ilike '%(account_id, row_index)%'
              and pg_get_indexdef(i.indexrelid) ilike '%send_uncertain%'
        ),
        'recipient_index', exists (
            select 1
            from pg_index i
            join pg_class c on c.oid = i.indexrelid
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'public'
              and c.relname = 'outreach_drafts_first_touch_email_unique_idx'
              and i.indisunique
              and pg_get_indexdef(i.indexrelid) ilike '%lower(btrim(email))%'
              and pg_get_indexdef(i.indexrelid) ilike '%send_uncertain%'
              and pg_get_indexdef(i.indexrelid) ilike '%sent%'
        ),
        'reservation_rpc',
            to_regprocedure('public.try_reserve_send(uuid,text,integer)') is not null,
        'duplicate_check_rpc',
            to_regprocedure('public.has_first_touch_conflict(uuid,text,bigint)') is not null,
        'stripe_rpc',
            to_regprocedure('public.apply_stripe_plan_event(uuid,text,text,text,bigint,text,integer)') is not null,
        'service_role_execute',
            coalesce(has_function_privilege(
                'service_role',
                to_regprocedure('public.try_reserve_send(uuid,text,integer)'),
                'EXECUTE'
            ), false)
            and coalesce(has_function_privilege(
                'service_role',
                to_regprocedure('public.has_first_touch_conflict(uuid,text,bigint)'),
                'EXECUTE'
            ), false)
            and coalesce(has_function_privilege(
                'service_role',
                to_regprocedure('public.atomic_send_contract()'),
                'EXECUTE'
            ), false),
        'client_execute_revoked',
            coalesce(not has_function_privilege(
                'anon',
                to_regprocedure('public.try_reserve_send(uuid,text,integer)'),
                'EXECUTE'
            ), false)
            and coalesce(not has_function_privilege(
                'authenticated',
                to_regprocedure('public.try_reserve_send(uuid,text,integer)'),
                'EXECUTE'
            ), false)
            and coalesce(not has_function_privilege(
                'anon',
                to_regprocedure('public.has_first_touch_conflict(uuid,text,bigint)'),
                'EXECUTE'
            ), false)
            and coalesce(not has_function_privilege(
                'authenticated',
                to_regprocedure('public.has_first_touch_conflict(uuid,text,bigint)'),
                'EXECUTE'
            ), false)
            and coalesce(not has_function_privilege(
                'anon',
                to_regprocedure('public.atomic_send_contract()'),
                'EXECUTE'
            ), false)
            and coalesce(not has_function_privilege(
                'authenticated',
                to_regprocedure('public.atomic_send_contract()'),
                'EXECUTE'
            ), false)
    );
$$;

revoke all on function public.atomic_send_contract() from public, anon, authenticated;
grant execute on function public.atomic_send_contract() to service_role;
revoke all on function public.has_first_touch_conflict(uuid,text,bigint)
    from public, anon, authenticated;
grant execute on function public.has_first_touch_conflict(uuid,text,bigint)
    to service_role;

notify pgrst, 'reload schema';

commit;
