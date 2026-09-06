begin;
create extension if not exists pgtap with schema extensions;
select plan(55);

select has_table('public', 'accounts', 'accounts exists');
select has_table('public', 'reviews', 'reviews exists');
select has_table('public', 'tracked_threads', 'tracked_threads exists');
select has_table('public', 'commitments', 'commitments exists');
select has_table('public', 'worker_runs', 'worker_runs exists');
select has_table('public', 'worker_events', 'worker_events exists');

select has_column('public', 'reviews', 'sent_at', 'review sends are durably timestamped');
select has_column('public', 'worker_events', 'dedupe_key', 'worker events support idempotency');
select has_column('public', 'commitments', 'estimated_value', 'promise value is persisted');
select has_column('public', 'worker_runs', 'degraded_accounts', 'degraded account count is persisted');
select has_column('public', 'worker_runs', 'degraded_reasons', 'degraded reason codes are persisted');

select ok(
    (select is_nullable = 'YES' from information_schema.columns
     where table_schema = 'public' and table_name = 'reviews' and column_name = 'row_index'),
    'reviews.row_index is nullable'
);
select has_index('public', 'reviews', 'reviews_thread_message_idx', 'review dedupe index exists');
select has_index('public', 'worker_events', 'worker_events_account_dedupe_unique_idx', 'account event dedupe index exists');

select ok((select relrowsecurity from pg_class where oid = 'public.reviews'::regclass), 'reviews RLS enabled');
select ok((select relrowsecurity from pg_class where oid = 'public.commitments'::regclass), 'commitments RLS enabled');
select ok(not has_table_privilege('anon', 'public.reviews', 'select'), 'anon cannot read reviews');
select ok(not has_table_privilege('authenticated', 'public.worker_events', 'select'), 'authenticated cannot read worker events');
select has_index('public', 'worker_events', 'worker_events_run_dedupe_unique_idx', 'run-scoped event dedupe index exists');
select ok((public.sendkeep_schema_contract()->'indexes'->>'worker_events_run_dedupe_unique_idx')::boolean, 'worker run event dedupe index exists');
select ok((public.sendkeep_schema_contract()->'rls'->>'accounts')::boolean, 'accounts RLS enabled');
select ok((public.sendkeep_schema_contract()->'rls'->>'worker_events')::boolean, 'worker events RLS enabled');
select ok((public.sendkeep_schema_contract()->'constraints'->>'commitments_status_check')::boolean, 'commitment statuses constrained');
select ok((public.sendkeep_schema_contract()->>'version') = '20260827000000_sendkeep_baseline', 'schema contract version is current');

select has_table('public', 'send_reservations', 'send reservation ledger exists');
select ok((select relrowsecurity from pg_class where oid = 'public.send_reservations'::regclass), 'send reservations RLS enabled');
select ok(not has_table_privilege('anon', 'public.send_reservations', 'select'), 'anon cannot read send reservations');
select ok(not has_table_privilege('authenticated', 'public.send_reservations', 'insert,update,delete'), 'authenticated cannot mutate send reservations');
select ok(exists (
    select 1 from pg_constraint
    where conrelid = 'public.send_reservations'::regclass
      and contype = 'u'
      and pg_get_constraintdef(oid) ilike '%(account_id, operation_key)%'
), 'operation keys are unique per account');
select ok(exists (
    select 1 from pg_constraint
    where conrelid = 'public.send_reservations'::regclass
      and conname = 'send_reservations_status_check'
), 'reservation statuses are constrained');
select has_index('public', 'send_reservations', 'send_reservations_active_idx', 'active reservation index exists');
select has_index('public', 'outreach_drafts', 'outreach_drafts_active_row_idx', 'active draft row index exists');
select has_index('public', 'outreach_drafts', 'outreach_drafts_first_touch_email_unique_idx', 'one first-touch recipient index exists');
select ok(to_regprocedure('public.try_reserve_send(uuid,text,integer)') is not null, 'atomic reservation RPC exists');
select ok(to_regprocedure('public.has_first_touch_conflict(uuid,text,bigint)') is not null, 'recipient conflict RPC exists');
select ok(not has_function_privilege('anon', 'public.has_first_touch_conflict(uuid,text,bigint)', 'execute'), 'anon cannot probe recipient conflicts');
select ok(has_function_privilege('service_role', 'public.has_first_touch_conflict(uuid,text,bigint)', 'execute'), 'service role can check recipient conflicts');
select ok(to_regprocedure('public.apply_stripe_plan_event(uuid,text,text,text,bigint,text,integer)') is not null, 'Stripe event ordering RPC exists');
select ok((public.atomic_send_contract()->>'version') = '20260901000000_atomic_send_contract', 'atomic send contract version is current');
select ok((public.atomic_send_contract()->>'service_role_execute')::boolean, 'service role can reserve sends');
select ok((public.atomic_send_contract()->>'client_execute_revoked')::boolean, 'clients cannot reserve sends');

select has_column('public', 'commitments', 'timezone', 'promise timezone is persisted');
select has_table('public', 'commitment_events', 'promise transition audit exists');
select ok((select relrowsecurity from pg_class where oid = 'public.commitment_events'::regclass), 'promise event RLS enabled');
select ok(not has_table_privilege('anon', 'public.commitment_events', 'select'), 'anon cannot read promise events');
select ok(not has_table_privilege('authenticated', 'public.commitment_events', 'select'), 'authenticated cannot read promise events');
select ok(has_table_privilege('service_role', 'public.commitment_events', 'select'), 'service role can read promise events');
select has_index('public', 'commitment_events', 'commitment_events_account_commitment_idx', 'promise event lookup index exists');
select ok(exists (
    select 1 from pg_constraint
    where conrelid = 'public.commitment_events'::regclass
      and conname = 'commitment_events_account_id_commitment_id_transition_key_key'
), 'promise transitions are idempotent per account');
select ok(exists (
    select 1 from pg_constraint
    where conrelid = 'public.commitments'::regclass
      and conname = 'commitments_status_check'
      and pg_get_constraintdef(oid) ilike '%scheduled%'
      and pg_get_constraintdef(oid) not ilike '%confirmed%'
), 'promise lifecycle uses the scheduled state');
select ok(to_regprocedure('public.transition_commitment_with_event(uuid,bigint,text,text,timestamp with time zone,timestamp with time zone,text,text)') is not null, 'promise transition RPC exists');
select ok(to_regprocedure('public.mark_commitment_due_with_alert(uuid,bigint)') is not null, 'atomic due transition RPC exists');
select ok((public.promise_ledger_contract()->>'version') = '20260904000000_promise_ledger', 'promise ledger contract version is current');
select ok((public.promise_ledger_contract()->>'service_role_execute')::boolean, 'service role can transition promises');
select ok((public.promise_ledger_contract()->>'client_execute_revoked')::boolean, 'clients cannot transition promises');

select * from finish();
rollback;
