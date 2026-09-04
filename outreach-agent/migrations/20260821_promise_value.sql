-- Optional operator-entered value for the promise graph.
-- This is never inferred from email text and never changes send behavior.
alter table if exists public.commitments
    add column if not exists estimated_value numeric;

do $$
begin
    if to_regclass('public.commitments') is not null then
        execute $comment$comment on column public.commitments.estimated_value is
            'Optional operator-entered pipeline value in account currency; never inferred from email.'$comment$;
    end if;
end $$;

notify pgrst, 'reload schema';
