# Getting started with Sendkeep

> **Content source of record** for the in-app page at `/getting-started`
> (built from `app/src/GettingStartedPage.tsx`). Approved 2026-07-23. When you
> change one, change the other: this file is what gets reviewed, the component
> is what ships.
>
> Limits shown in the app are read live from `/api/plan` rather than
> hardcoded, so the numbers below are the defaults and may differ per instance.

Sendkeep emails a list of leads from your own Gmail and handles the replies.
Bring a list you already have, or let it find one for you.

It runs two agents against one Google Sheet of leads:

- **The outreach agent** (core) emails the leads you approved, from your own
  Gmail, then watches for replies and drafts responses for you to approve.
- **The lead sourcing agent** (optional) finds people matching a description
  you write and adds them to your sheet for review.

**You do not need the lead sourcing agent.** If you already have a list,
whether that is a CSV export, a spreadsheet a colleague sent you, or a sheet
you built by hand, the outreach agent works on its own. Point it at your
prepared sheet and never open the Leads page. Everything from step 5 onwards
applies exactly the same either way.

You stay in the loop at both points that matter: no lead gets emailed until you
approve it, and no reply gets sent until you read it.

Setup takes about ten minutes. You need a Google account, and a calendar
booking link (Cal.com, Calendly, or similar) if you want meetings to be
bookable from your emails.

---

## Step 1 — Create your account

Go to `/login` and click **Continue with Google**. That's the whole account —
there's no separate password to set.

> **If the instance owner is still in Google's testing program**, they need to
> add your Google account as a test user in Google Cloud Console before step 2
> will work. Ask them if you get an "access blocked" screen.

---

## Step 2 — Connect your Google account

Open **Settings** and click **Connect Google**. You will be asked to grant two
things:

- **Gmail** — so outreach is sent from your own address, and replies land in
  your own inbox.
- **Sheets** — so the agents can read your lead sheet and write statuses back.

Your token is encrypted before it is stored, and it is never written to disk.

**Expect a scary screen.** While Sendkeep is in Google's testing program,
Google shows "This app hasn't been verified." Click **Advanced**, then **Go to
Sendkeep** to continue. This is normal for an app that has not yet completed
Google's verification review.

When it works, the Google account card reads "Connected — Gmail and Sheets are
authorized."

---

## Step 3 — Set up your lead sheet

Sendkeep reads and writes one Google Sheet. It has to be laid out a specific
way, because every column is addressed by position.

### The fastest way: start from the template

On the Settings page, under **Lead sheet**, download the starter sheet as
**Excel** (`.xlsx`) or **CSV**. Both have the header row already correct and
one example row per status. The Excel version adds a "How to use" tab
explaining each column, which a CSV cannot carry.

1. Drag the file into Google Drive.
2. Open it and choose **File → Save as Google Sheets**.
3. Delete the five example rows. Keep row 1.
4. Add your own leads: fill in **Name**, **Email**, and **Company**, and leave
   the rest blank.
5. Back in Settings, click **Choose sheet** and pick it.

The example rows are safe to leave in by accident. Every one of them carries a
Status, and they all use `@example.com` addresses, which can never reach a real
mailbox. But delete them anyway so your numbers stay honest.

### If you already have a sheet

Row 1 must contain these nine labels, in this exact order, in columns A to I:

```
Name | Email | Company | Status | ThreadID | SentAt | EmailBody | LeadReason | EmailConfidence
```

Paste that into row 1 of your existing sheet and you are done. You do not need
to start over.

**Why Sendkeep is strict about this.** It refuses to write any status until
all nine labels are present. Columns D through I belong to the app, and the
full header is your explicit consent that it may write there. Without that
check, connecting a sheet where you keep your own notes in column E would
silently overwrite them.

### If your leads are in a CSV

A CRM export, a list someone sent you, anything already in CSV works. Give it
the same nine-column header, then bring it into Google Sheets:

1. In Google Drive, choose **New → File upload**, or from an existing sheet
   **File → Import**.
2. Import it as the **first tab**. If the import lands your data on a second
   tab, Sendkeep will not see it.
3. Check the Status column before connecting. See the warning below.
4. In Settings, click **Choose sheet** and pick it.

> **A CSV export almost always has an empty Status column, and empty means
> queued.** Import 500 contacts that way and all 500 are lined up to be
> emailed. Before you connect the sheet, fill the Status column with anything
> (`Needs verification` works), then clear it row by row as you approve. The
> daily cap limits the damage, but it does not prevent it.

Sendkeep never reads the CSV file itself. Leads are read through the Google
Sheets API, so the file has to become a Sheet first. Editing the original CSV
afterwards changes nothing, and exporting the Sheet back to CSV does not feed
anything back in.

Only the **first tab** is read, so you can keep other tabs for your own working
notes.

### Picking the sheet

Use **Choose sheet** to browse the spreadsheets in your Drive, or click **Or
paste a Sheet ID/URL manually** and paste either the full Sheets URL or just
the ID.

---

## Step 4 — Fill in your outreach settings

Still on Settings:

| Field | What it does |
|---|---|
| **Sender first name** | Signs every outreach email |
| **Calendar booking link** | Dropped into emails so people can book you directly |
| **Meeting purpose** | One sentence, used in every email, describing what you want from the call |
| **Notification email** | Where batch summaries and reply alerts go |

Write the meeting purpose as a natural continuation of "I'd like to set up...".
For example: *a quick intro call to see if there's a fit to work together*.

Click **Save settings**. The onboarding checklist at the top of the page ticks
off as you complete each piece.

---

## Step 5 — Add leads

Two ways in, and you can mix them freely in one sheet.

### Option A: bring a list you already have

This is the common case, and it needs no agent at all. Put your people in the
sheet yourself: fill in **Name**, **Email** and **Company**, and leave
**Status** blank on anyone you are happy to email. Blank Status means
"approved, send this."

Paste them in by hand, or import a CSV as described in step 3. The outreach
agent does not care where the rows came from. Rows you added and rows the
sourcing agent found are treated identically from here on.

> If you are pasting in a large list, fill the Status column first and clear it
> as you approve. Every row you leave blank is queued to send.

### Option B: let the lead sourcing agent find them

Go to **Leads**. Describe who you are looking for in plain language, for
example *founders of seed-stage fintech startups in the UK*, and set how many
you want (1 to 25). Click search.

The agent researches the web and appends what it finds to your sheet with:

- **Status** set to `Needs verification`, so nothing can be emailed yet
- **LeadReason** explaining why it thinks this person fits
- **EmailConfidence** marked `verified` or `unverified`, telling you whether it
  found the address stated outright or had to guess the pattern

Searches are limited to 10 per hour.

---

## Step 6 — Review and approve

*Only applies to leads the sourcing agent found. If you brought your own list,
you already approved them by leaving Status blank, so skip to step 7.*

Back on the **Leads** page you see everything waiting for review. For each one
you get the name, company, the reason the agent picked them, and the email
confidence.

- **Approve** clears the Status, which puts the lead in the send queue.
- **Discard** marks it `Discarded`, and it is never emailed.

Pay attention to `unverified` confidence. That means the address is a pattern
guess, and sending to it risks a bounce.

---

## Step 7 — Send your first batch

Go to **Outreach**. Before anything sends you get a preview showing how many
contacts are eligible right now, how many already went out in the last 24
hours, and how many are being held back by the daily cap.

Sending requires an explicit confirmation. Nothing goes out on a single stray
click.

For each contact the agent writes the email, sends it from your Gmail, and
immediately marks the row `Sent` with the thread ID, the timestamp, and the
body it used. It sends four at a time. When the batch finishes you get a
summary at your notification email.

**The daily cap is 25 emails per 24 hours by default** (the instance owner can
change it). This is a deliberate rate limit, not a licensing limit. Sending
large volumes from a fresh address is how domains get flagged as spam. You can
also run at most 5 batches per hour.

---

## Step 8 — Handle replies

Click **Check replies** on the Outreach page. The agent looks at every thread
it has sent, finds new responses, strips out the quoted history, and drafts a
reply for each one.

Drafts land in a review queue. For each you can **edit the text and send**, or
**dismiss** it without sending. Sent replies go out in the original thread, so
the conversation stays intact for the person receiving it.

Nothing is ever sent to a prospect without you reading it first.

You can also check a single contact with a read-only "has this person replied
yet?" check, which drafts nothing and writes nothing.

---

## Reference: what the Status column means

Status is the control switch for the whole system. It is the single most
important column to understand.

| Status | Meaning |
|---|---|
| *(blank)* | **Queued.** Will be emailed on the next run |
| `Needs verification` | Found by the lead agent, waiting for your approval |
| `Ready for review` | Same, also accepted by the review queue |
| `Sent` | Already emailed. SentAt and ThreadID record when and which thread |
| `Replied` | They answered. The reply is waiting for you in Outreach |
| `Discarded` | Skipped. Never emailed |

**A blank Status is the only thing that makes a row eligible to send.** If you
paste in a list of 500 contacts with an empty Status column, all 500 are queued
the moment you save. Add a status first, then clear it row by row as you
approve.

## Reference: who owns which column

| Column | Filled in by |
|---|---|
| Name, Email, Company | You |
| Status | Both you and Sendkeep |
| ThreadID, SentAt, EmailBody | Sendkeep, when it sends |
| LeadReason, EmailConfidence | The lead sourcing agent |

## Reference: limits

| Limit | Value |
|---|---|
| Outreach emails | 25 per 24 hours (configurable per instance) |
| Send batches | 5 per hour |
| Lead searches | 10 per hour |
| Leads per search | 1 to 25 |
| Reply checks | 45 per hour |

---

## Troubleshooting

**"Before Sendkeep can write statuses, row 1 must contain the full header."**
Your sheet is missing one or more of the nine labels, or they are out of order.
Paste the full header into row 1, or download the starter sheet from Settings.

**"The sheet's header row doesn't match what Sendkeep expects."** Same cause,
caught on a read instead of a write. The message shows what it found so you can
compare.

**"This account hasn't connected a Google Sheet yet."** Pick a sheet in
Settings.

**"Could not load your sheets."** Usually the Drive API is not enabled on the
instance, or your Google token expired. Try **Reconnect** on the Settings page.

**Google says "This app hasn't been verified."** Expected while Sendkeep is in
Google's testing program. Click **Advanced**, then **Go to Sendkeep**.

**"Access blocked" from Google.** Your Google account has not been added as a
test user yet. Ask whoever runs the instance.

**A row was emailed that you did not expect.** Check its Status was not blank.
Blank means queued. This is the most common surprise.

**An email sent but the row still looks unsent.** Sendkeep marks the row
straight after sending, and if that write fails it raises an error that says
the email *was* sent, so you can fix the row by hand instead of emailing the
person twice. Mark it `Sent` yourself.

---

## What Sendkeep will never do

- Email anyone whose row you have not approved
- Send a reply you have not read
- Edit the Name, Email, or Company you typed. Status updates only ever touch
  columns D, E, F, G, and I. The lead sourcing agent adds new rows below your
  data, but it never rewrites a row you created
- Write anything at all until row 1 carries the full header
- Exceed the daily send cap
