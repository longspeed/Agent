-- 2026-08-23: Enforce review dedupe at the database level.
--
-- reviews_db.add_review's check-then-insert is not airtight against a true
-- simultaneous race (the server's /replies/check endpoint and the background
-- worker both call it; on one live inbox this produced two reviews for the
-- same inbound message five seconds apart, each with its own AI draft).
-- The unique partial index below is the actual enforcement: Postgres treats
-- NULLs as distinct, so the `where gmail_message_id is not null` predicate
-- keeps legacy rows (written before the column existed) from colliding while
-- making every new row race-safe. add_review now also catches the 23505
-- violation and returns the winning row instead of raising.

create unique index if not exists reviews_thread_message_idx
on public.reviews (account_id, thread_id, gmail_message_id)
where gmail_message_id is not null;
