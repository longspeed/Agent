-- Preserve the only durable evidence for an in-flight or ambiguous Gmail send.
-- This repeats the function body because 20260831000000 may already be applied
-- on a pilot project. New projects receive the same definition twice safely.
create or replace function public.delete_tracked_contact(
    p_account_id uuid,
    p_tracked_id bigint
) returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
    target public.tracked_threads%rowtype;
begin
    select * into target
    from public.tracked_threads
    where account_id = p_account_id and id = p_tracked_id
    for update;

    if not found or target.status = 'removed' then
        return jsonb_build_object('ok', false, 'reason', 'not_found');
    end if;

    if exists (
        select 1 from public.reviews
        where account_id = p_account_id
          and thread_id = target.thread_id
          and status = 'send_uncertain'
    ) then
        return jsonb_build_object('ok', false, 'reason', 'send_uncertain');
    end if;
    if exists (
        select 1 from public.reviews
        where account_id = p_account_id
          and thread_id = target.thread_id
          and status = 'sending'
    ) or exists (
        select 1 from public.outreach_drafts
        where account_id = p_account_id
          and thread_id = target.thread_id
          and follow_up_status = 'sending'
    ) then
        return jsonb_build_object('ok', false, 'reason', 'send_in_progress');
    end if;

    if to_regclass('public.notification_outbox') is not null then
        update public.notification_outbox
           set status = 'cancelled', claim_token = null, claim_expires_at = null,
               updated_at = now()
         where account_id = p_account_id
           and status in ('pending', 'sending', 'retry')
           and (
               (source_type = 'review' and source_id in (
                   select id::text from public.reviews
                   where account_id = p_account_id and thread_id = target.thread_id
               ))
               or
               (source_type = 'commitment' and source_id in (
                   select id::text from public.commitments
                   where account_id = p_account_id and thread_id = target.thread_id
               ))
           );
    end if;

    delete from public.reviews
     where account_id = p_account_id and thread_id = target.thread_id;
    delete from public.commitments
     where account_id = p_account_id and thread_id = target.thread_id;
    delete from public.outreach_drafts
     where account_id = p_account_id
       and (thread_id = target.thread_id
            or (target.email <> '' and lower(email) = lower(target.email)));

    update public.tracked_threads
       set email = '', name = '', company = '', row_index = null,
           source = 'user_deleted', status = 'removed',
           last_contact_message_id = null, last_checked_at = null,
           last_error = null, updated_at = now()
     where account_id = p_account_id and id = p_tracked_id;

    return jsonb_build_object('ok', true);
end;
$$;

revoke all on function public.delete_tracked_contact(uuid, bigint) from public, anon, authenticated;
grant execute on function public.delete_tracked_contact(uuid, bigint) to service_role;

notify pgrst, 'reload schema';
