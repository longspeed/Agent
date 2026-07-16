# Agent
A website consists of Agents that works on specific tasks

## Sharing Agent Hub with another person

Agent Hub is multi-tenant: one running instance serves any number of
customers, each with their own login, their own connected Gmail, their own
Google Sheet, and their own review queue and usage metering in Supabase.

To give someone access, just send them the URL — they onboard themselves
with no code or config changes:

1. **Sign up** at `/signup` (their own email + password).
2. **Connect Google** from the Settings page — an in-browser OAuth consent
   for their Gmail and Sheets. Their token is stored encrypted on their
   account row; it never touches disk. (While the app's Google OAuth consent
   screen is in "Testing" status, you must first add their Google account as
   a test user in Google Cloud Console → APIs & Services → OAuth consent
   screen.)
3. **Fill in Settings**: their Google Sheet ID, sender name, calendar
   booking link, meeting purpose, and notification email.

Everything they see and send is scoped to their account — reviews and usage
rows carry an `account_id` in Supabase, background jobs are only visible to
the account that started them, and no tenant can read another's data.

Setup for the instance itself (shared OAuth client, Supabase schema, API
keys) is documented in `outreach-agent/README.md`.
