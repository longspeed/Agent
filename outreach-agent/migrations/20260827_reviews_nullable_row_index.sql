-- Durable Gmail tracking can surface a reply without a Google Sheet row.
-- NULL is the truthful value in that case. During a rolling deployment the
-- application may temporarily use zero against an old NOT NULL schema. Sheet
-- contacts begin on row 2, so zero cannot target a real row; normalize it here.
do $$
begin
    if to_regclass('public.reviews') is not null then
        alter table public.reviews alter column row_index drop not null;
        update public.reviews set row_index = null where row_index = 0;
    end if;
end $$;

notify pgrst, 'reload schema';
