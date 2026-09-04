begin;

-- Repair installations where an older, non-unique index already used the
-- canonical reply-dedupe index name.  `create ... if not exists` cannot fix
-- that shape, so the baseline migration alone leaves reply insertion races
-- unprotected.
--
-- This migration is data-preserving.  It replaces only the index.  The
-- preflight block fails closed if duplicate Gmail message identities exist,
-- rather than deleting or merging review rows implicitly.
do $$
begin
    if exists (
        select 1
        from public.reviews
        where gmail_message_id is not null
        group by account_id, thread_id, gmail_message_id
        having count(*) > 1
    ) then
        raise exception using
            errcode = '23505',
            message = 'Cannot install reviews_thread_message_idx: duplicate Gmail message identities exist';
    end if;
end $$;

drop index if exists public.reviews_thread_message_idx;

create unique index reviews_thread_message_idx
    on public.reviews (account_id, thread_id, gmail_message_id)
    where gmail_message_id is not null;

notify pgrst, 'reload schema';

commit;
