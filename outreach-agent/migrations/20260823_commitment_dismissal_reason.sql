-- Distinguish extraction errors from ordinary promise cleanup.
-- Only explicit `incorrect` decisions count against promise precision.
alter table if exists public.commitments
    add column if not exists dismissal_reason text;

do $$
begin
    if to_regclass('public.commitments') is not null then
        alter table public.commitments
            drop constraint if exists commitments_dismissal_reason_check;
        alter table public.commitments
            add constraint commitments_dismissal_reason_check
            check (dismissal_reason is null or dismissal_reason in ('incorrect', 'not_applicable'));
        execute $comment$comment on column public.commitments.dismissal_reason is
            'Operator judgment: incorrect extraction or non-model-related cleanup.'$comment$;
    end if;
end $$;

notify pgrst, 'reload schema';
