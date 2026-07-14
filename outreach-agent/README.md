# Outreach Agent

Reads customers from a Google Sheet, sends each a personalized outreach email
via Gmail, notifies you when the batch is done, and watches for replies —
drafting an AI response you review and send yourself.

## 1. Install dependencies

```
pip install -r requirements.txt
```

## 2. Google Cloud setup (Gmail + Sheets access)

1. Go to https://console.cloud.google.com/ and create a new project.
2. Enable the **Gmail API** and **Google Sheets API** for it (APIs & Services > Library).
3. Go to APIs & Services > Credentials > Create Credentials > OAuth client ID.
   - Application type: **Desktop app**
   - If prompted, configure the OAuth consent screen first (External is fine for personal use; add your own email as a test user).
4. Download the credentials JSON and save it as `credentials.json` in this folder.

## 3. Create the Google Sheet

In the **first tab** of the spreadsheet (any tab name is fine — the code
always targets the first tab), add this header row:

| Name | Email | Company | Status | ThreadID | SentAt | EmailBody |
|------|-------|---------|--------|----------|--------|-----------|

Add one row per customer you want to reach out to (Name, Email, Company only —
leave Status/ThreadID/SentAt/EmailBody blank, the scripts fill those in).

Copy the Sheet's ID from its URL:
`https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`

## 4. OpenRouter API key

Sign up at https://openrouter.ai/ and create an API key
(https://openrouter.ai/settings/keys). The default model in `.env.example`,
`openai/gpt-oss-20b:free`, is a free model — no payment needed
to get started. Free models have rate limits (roughly 20 requests/min, 200/day
as of mid-2026), which is plenty for outreach batches of normal size. You can
swap `OPENROUTER_MODEL` for any other model ID from
https://openrouter.ai/models if you want higher quality or fewer limits later.

## 5. Google Calendar booking link

The outreach email's call-to-action is a link where the customer picks a
meeting time themselves — Google Calendar books it automatically, no code
needed:

1. Open https://calendar.google.com/
2. Click **Create** > **Appointment schedule**
3. Set your available hours/days and meeting length
4. Save, then click **"Share this booking page"** and copy the link

## 6. Configure environment

```
cp .env.example .env
```

Fill in:
- `OPENROUTER_API_KEY` — your OpenRouter API key from step 4
- `OPENROUTER_MODEL` — leave as the default free model, or swap for another
- `NOTIFY_EMAIL` — where you want batch/reply notifications sent
- `GOOGLE_SHEET_ID` — from step 3
- `CALENDAR_BOOKING_LINK` — from step 5
- `SENDER_NAME` — the first name the emails are signed with
- `MEETING_PURPOSE` — one sentence on why you want the meeting, e.g.
  "a quick call to see if [product] could help with [pain point]" — this is
  what the AI uses to write a specific, non-generic email instead of filler
- `APP_PASSWORD` — password for the web dashboard (`server.py`) login screen.
  Anyone with this password can trigger real sends from the dashboard, so
  pick something you wouldn't mind rotating.
- `APP_SECRET_KEY` — random key used to sign dashboard login sessions, e.g.
  `python -c "import secrets; print(secrets.token_hex(32))"`. Changing it
  logs everyone out.
- `SUPABASE_URL` / `SUPABASE_SECRET_KEY` — from your Supabase project's
  Settings > API Keys page (`SUPABASE_SECRET_KEY` is the **secret** key,
  not the publishable key — this runs server-side only and needs write
  access). Before running the app, create the reviews table by pasting
  this into the Supabase SQL editor:

  ```sql
  create table reviews (
    id bigint generated always as identity primary key,
    row_index integer not null,
    name text,
    email text,
    thread_id text,
    customer_reply text,
    draft_reply text,
    status text not null default 'pending',
    created_at timestamptz not null default now()
  );
  ```

## 6. Run

Send outreach to all pending rows:
```
python send_outreach.py
```

The first run opens a browser window for Google OAuth consent — sign in with
the Google account you want to send from. This creates `token.json` so you
won't need to log in again.

Watch for replies (checks every 5 minutes; Ctrl+C to stop):
```
python watch_replies.py
```

Or check once and exit:
```
python watch_replies.py --once
```

When a customer replies, you'll get an email with their reply plus an
AI-drafted response. Review it and send it yourself from Gmail — this tool
never sends replies automatically.

## Notes

- Re-running `send_outreach.py` only sends to rows with an empty `Status`
  column, so it's safe to add new rows to the sheet and re-run anytime.
- `watch_replies.py` only checks rows with `Status = Sent`; once a reply is
  detected the row is marked `Replied` and won't be checked again.
