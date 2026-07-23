import { useEffect, useState, type ReactNode } from 'react'
import { NavBar } from './components/NavBar'

// Content source of record is docs/getting-started.md. Keep the two in step
// when either changes -- the doc is what gets reviewed, this is what ships.

interface Plan {
  daily_send_limit: number
  lead_searches_per_hour: number
}

const SECTIONS = [
  { id: 'overview', label: 'What it does' },
  { id: 'step-1', label: '1. Create your account' },
  { id: 'step-2', label: '2. Connect Google' },
  { id: 'step-3', label: '3. Set up your lead sheet' },
  { id: 'step-4', label: '4. Outreach settings' },
  { id: 'step-5', label: '5. Add leads' },
  { id: 'step-6', label: '6. Review and approve' },
  { id: 'step-7', label: '7. Send your first batch' },
  { id: 'step-8', label: '8. Handle replies' },
  { id: 'status', label: 'The Status column' },
  { id: 'columns', label: 'Who owns which column' },
  { id: 'limits', label: 'Limits' },
  { id: 'troubleshooting', label: 'Troubleshooting' },
]

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section id={id} className="scroll-mt-24 mb-14">
      <h2 className="font-display text-xl mb-4 text-cream" style={{ letterSpacing: '-0.015em' }}>
        {title}
      </h2>
      <div className="space-y-4 text-sm leading-relaxed text-sand">{children}</div>
    </section>
  )
}

function Callout({ tone = 'note', children }: { tone?: 'note' | 'warn'; children: ReactNode }) {
  const accent = tone === 'warn' ? 'var(--color-gold)' : 'var(--color-steel)'
  return (
    <div
      className="rounded-lg px-4 py-3 text-sm leading-relaxed"
      style={{
        background: 'var(--color-floating)',
        borderLeft: `2px solid ${accent}`,
        color: 'var(--color-sand)',
      }}
    >
      {children}
    </div>
  )
}

function Table({ head, rows }: { head: string[]; rows: ReactNode[][] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-line">
      <table className="w-full text-sm border-collapse">
        <thead>
          <tr style={{ background: 'var(--color-floating)' }}>
            {head.map((h) => (
              <th
                key={h}
                className="text-left font-medium px-4 py-2.5 text-cream whitespace-nowrap border-b border-line"
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className={i ? 'border-t border-line' : ''}>
              {row.map((cell, j) => (
                <td key={j} className="px-4 py-2.5 align-top text-sand">
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Code({ children }: { children: ReactNode }) {
  return (
    <code
      className="rounded px-1.5 py-0.5 text-[0.85em]"
      style={{ background: 'var(--color-floating)', color: 'var(--color-cream)' }}
    >
      {children}
    </code>
  )
}

function Steps({ items }: { items: ReactNode[] }) {
  return (
    <ol className="space-y-2.5">
      {items.map((item, i) => (
        <li key={i} className="flex gap-3">
          <span
            className="shrink-0 mt-0.5 w-5 h-5 rounded-full flex items-center justify-center text-[11px] font-medium"
            style={{ background: 'var(--color-floating)', color: 'var(--color-accent)' }}
          >
            {i + 1}
          </span>
          <span className="flex-1">{item}</span>
        </li>
      ))}
    </ol>
  )
}

export function GettingStartedPage() {
  const [plan, setPlan] = useState<Plan | null>(null)

  useEffect(() => {
    // Limits are read live rather than hardcoded: DAILY_SEND_LIMIT is an env
    // var, so a written-in number would drift per deployment.
    fetch('/api/plan')
      .then((r) => (r.ok ? r.json() : null))
      .then(setPlan)
      .catch(() => setPlan(null))
  }, [])

  const dailyCap = plan ? `${plan.daily_send_limit} emails` : 'a fixed number of emails'
  const searchCap = plan ? `${plan.lead_searches_per_hour} per hour` : 'a fixed number per hour'

  return (
    <>
      <NavBar breadcrumb="Getting started" />

      <main className="relative max-w-5xl mx-auto px-6 py-10">
        <div className="mb-10">
          <h1 className="font-display text-3xl mb-3 text-cream" style={{ letterSpacing: '-0.02em' }}>
            Getting started with Agent Hub
          </h1>
          <p className="text-sm leading-relaxed text-sand max-w-2xl">
            Agent Hub emails a list of leads from your own Gmail and handles the replies. Bring a
            list you already have, or let it find one for you. Setup takes about ten minutes: you
            need a Google account, and a calendar booking link (Cal.com, Calendly, or similar) if
            you want meetings bookable straight from your emails.
          </p>
        </div>

        <div className="flex gap-10">
          {/* Sticky contents rail, desktop only */}
          <nav className="hidden lg:block w-56 shrink-0">
            <div className="sticky top-24">
              <p className="text-xs font-medium mb-3 text-mute uppercase tracking-wider">Contents</p>
              <ul className="space-y-1.5 text-xs">
                {SECTIONS.map((s) => (
                  <li key={s.id}>
                    <a
                      href={`#${s.id}`}
                      className="block py-0.5 text-mute transition-colors duration-200 ease-spring hover:text-cream focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 rounded"
                    >
                      {s.label}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          </nav>

          <div className="min-w-0 flex-1">
            <Section id="overview" title="What Agent Hub does">
              <p>Agent Hub runs two agents against one Google Sheet of leads.</p>
              <div className="grid sm:grid-cols-2 gap-3">
                <div className="rounded-lg p-4 bg-elevated border border-line">
                  <div className="flex items-baseline justify-between gap-2 mb-1.5">
                    <h3 className="text-cream font-medium text-sm">Outreach agent</h3>
                    <span className="text-[10px] uppercase tracking-wider text-mute">Core</span>
                  </div>
                  <p className="text-xs leading-relaxed">
                    Emails the leads you approved, from your own Gmail, then watches for replies and
                    drafts responses for you.
                  </p>
                </div>
                <div className="rounded-lg p-4 bg-elevated border border-line">
                  <div className="flex items-baseline justify-between gap-2 mb-1.5">
                    <h3 className="text-cream font-medium text-sm">Lead sourcing agent</h3>
                    <span className="text-[10px] uppercase tracking-wider text-mute">Optional</span>
                  </div>
                  <p className="text-xs leading-relaxed">
                    Finds people matching a description you write, and adds them to your sheet for
                    review. Skip it entirely if you already know who you want to reach.
                  </p>
                </div>
              </div>
              <Callout>
                <strong className="text-cream">You do not need the lead sourcing agent.</strong> If
                you already have a list, whether that is a CSV export, a spreadsheet a colleague sent
                you, or a sheet you built by hand, the outreach agent works on its own. Point it at
                your prepared sheet and never open the Leads page. Everything from step 5 onwards
                applies exactly the same either way.
              </Callout>
              <p>
                You stay in the loop at both points that matter: no lead gets emailed until you
                approve it, and no reply gets sent until you read it.
              </p>
            </Section>

            <Section id="step-1" title="1. Create your account">
              <p>
                Go to <Code>/signup</Code> and register with your email and a password of at least 8
                characters. You can also use <strong className="text-cream">Sign in with Google</strong>,
                which skips the password entirely.
              </p>
              <Callout>
                If the person running this instance is still in Google's testing program, they need
                to add your Google account as a test user before step 2 will work. Ask them if you
                hit an "access blocked" screen.
              </Callout>
            </Section>

            <Section id="step-2" title="2. Connect your Google account">
              <p>
                Open <strong className="text-cream">Settings</strong> and click{' '}
                <strong className="text-cream">Connect Google</strong>. You grant two things: Gmail,
                so outreach sends from your own address and replies land in your own inbox; and
                Sheets, so the agents can read your lead sheet and write statuses back.
              </p>
              <p>Your token is encrypted before it is stored, and never written to disk.</p>
              <Callout tone="warn">
                <strong className="text-cream">Expect a scary screen.</strong> While Agent Hub is in
                Google's testing program, Google shows "This app hasn't been verified." Click{' '}
                <strong className="text-cream">Advanced</strong>, then{' '}
                <strong className="text-cream">Go to Agent Hub</strong>. This is normal for an app
                that has not yet completed Google's verification review.
              </Callout>
            </Section>

            <Section id="step-3" title="3. Set up your lead sheet">
              <p>
                Agent Hub reads and writes one Google Sheet. It has to be laid out a specific way,
                because every column is addressed by position.
              </p>

              <h3 className="text-cream font-medium pt-2">The fastest way: start from the template</h3>
              <p>
                On the Settings page, under <strong className="text-cream">Lead sheet</strong>, download
                the starter sheet as{' '}
                <a
                  href="/api/lead-sheet-template.xlsx"
                  download
                  className="text-accent underline underline-offset-[3px] decoration-accent/40 rounded-[3px] transition-colors duration-200 ease-spring hover:text-accent-hover hover:decoration-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 active:opacity-85"
                >
                  Excel
                </a>{' '}
                or{' '}
                <a
                  href="/api/lead-sheet-template.csv"
                  download
                  className="text-accent underline underline-offset-[3px] decoration-accent/40 rounded-[3px] transition-colors duration-200 ease-spring hover:text-accent-hover hover:decoration-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 active:opacity-85"
                >
                  CSV
                </a>
                . Both have the header row already correct and one example row per status. The Excel
                version adds a "How to use" tab explaining each column, which a CSV cannot carry.
              </p>
              <Steps
                items={[
                  'Drag the file into Google Drive.',
                  <>
                    Open it and choose <strong className="text-cream">File → Save as Google Sheets</strong>.
                  </>,
                  'Delete the five example rows. Keep row 1.',
                  <>
                    Add your leads: fill in <strong className="text-cream">Name</strong>,{' '}
                    <strong className="text-cream">Email</strong> and{' '}
                    <strong className="text-cream">Company</strong>, and leave the rest blank.
                  </>,
                  <>
                    Back in Settings, click <strong className="text-cream">Choose sheet</strong> and
                    pick it.
                  </>,
                ]}
              />
              <p>
                The example rows are safe to leave in by accident. Every one carries a Status, and
                they all use <Code>@example.com</Code> addresses, which can never reach a real
                mailbox. Delete them anyway so your numbers stay honest.
              </p>

              <h3 className="text-cream font-medium pt-2">If you already have a sheet</h3>
              <p>Row 1 must contain these nine labels, in this order, in columns A to I:</p>
              <div
                className="rounded-lg px-4 py-3 text-xs overflow-x-auto"
                style={{ background: 'var(--color-floating)', color: 'var(--color-cream)' }}
              >
                <code className="whitespace-nowrap">
                  Name | Email | Company | Status | ThreadID | SentAt | EmailBody | LeadReason |
                  EmailConfidence
                </code>
              </div>
              <p>
                Paste that into row 1 of your existing sheet and you are done. You do not need to
                start over.
              </p>
              <Callout>
                <strong className="text-cream">Why Agent Hub is strict about this.</strong> It
                refuses to write any status until all nine labels are present. Columns D through I
                belong to the app, and the full header is your explicit consent that it may write
                there. Without that check, connecting a sheet where you keep your own notes in
                column E would silently overwrite them.
              </Callout>
              <h3 className="text-cream font-medium pt-2">If your leads are in a CSV</h3>
              <p>
                A CRM export, a list someone sent you, anything already in CSV works. Give it the
                same nine-column header, then bring it into Google Sheets:
              </p>
              <Steps
                items={[
                  <>
                    In Google Drive, choose <strong className="text-cream">New → File upload</strong>,
                    or from an existing sheet <strong className="text-cream">File → Import</strong>.
                  </>,
                  <>
                    Import it as the <strong className="text-cream">first tab</strong>. If the import
                    lands your data on a second tab, Agent Hub will not see it.
                  </>,
                  <>
                    Check the Status column before connecting. See the warning below.
                  </>,
                  <>
                    In Settings, click <strong className="text-cream">Choose sheet</strong> and pick
                    it.
                  </>,
                ]}
              />
              <Callout tone="warn">
                <strong className="text-cream">A CSV export almost always has an empty Status
                column, and empty means queued.</strong> Import 500 contacts that way and all 500 are
                lined up to be emailed. Before you connect the sheet, fill the Status column with
                anything (<Code>Needs verification</Code> works), then clear it row by row as you
                approve. The daily cap limits the damage, but it does not prevent it.
              </Callout>
              <Callout>
                Agent Hub never reads the CSV file itself. Leads are read through the Google Sheets
                API, so the file has to become a Sheet first. Editing the original CSV afterwards
                changes nothing, and exporting the Sheet back to CSV does not feed anything back in.
              </Callout>
              <p>
                Only the <strong className="text-cream">first tab</strong> is read, so you can keep
                other tabs for your own working notes.
              </p>
            </Section>

            <Section id="step-4" title="4. Fill in your outreach settings">
              <Table
                head={['Field', 'What it does']}
                rows={[
                  ['Sender first name', 'Signs every outreach email'],
                  ['Calendar booking link', 'Dropped into emails so people can book you directly'],
                  ['Meeting purpose', 'One sentence, used in every email, describing what you want from the call'],
                  ['Notification email', 'Where batch summaries and reply alerts go'],
                ]}
              />
              <p>
                Write the meeting purpose as a natural continuation of "I'd like to set up…". For
                example: <em className="text-cream">a quick intro call to see if there's a fit to work together</em>.
              </p>
              <p>
                Click <strong className="text-cream">Save settings</strong>. The onboarding checklist
                at the top of the page ticks off as you complete each piece.
              </p>
            </Section>

            <Section id="step-5" title="5. Add leads">
              <p>Two ways in, and you can mix them freely in one sheet.</p>

              <h3 className="text-cream font-medium pt-2">Option A: bring a list you already have</h3>
              <p>
                This is the common case, and it needs no agent at all. Put your people in the sheet
                yourself: fill in <strong className="text-cream">Name</strong>,{' '}
                <strong className="text-cream">Email</strong> and{' '}
                <strong className="text-cream">Company</strong>, and leave{' '}
                <strong className="text-cream">Status</strong> blank on anyone you are happy to
                email. Blank Status means "approved, send this."
              </p>
              <p>
                Paste them in by hand, or import a CSV as described in step 3. The outreach agent
                does not care where the rows came from. Rows you added and rows the sourcing agent
                found are treated identically from here on.
              </p>
              <Callout tone="warn">
                If you are pasting in a large list, fill the Status column first and clear it as you
                approve. Every row you leave blank is queued to send.
              </Callout>

              <h3 className="text-cream font-medium pt-2">Option B: let the agent find them</h3>
              <p>
                Go to <strong className="text-cream">Leads</strong>. Describe who you are looking for
                in plain language, for example{' '}
                <em className="text-cream">founders of seed-stage fintech startups in the UK</em>, and
                set how many you want (1 to 25).
              </p>
              <p>The agent researches the web and appends what it finds with:</p>
              <ul className="space-y-1.5 pl-4 list-disc marker:text-mute">
                <li>
                  <strong className="text-cream">Status</strong> set to <Code>Needs verification</Code>,
                  so nothing can be emailed yet
                </li>
                <li>
                  <strong className="text-cream">LeadReason</strong> explaining why it thinks this
                  person fits
                </li>
                <li>
                  <strong className="text-cream">EmailConfidence</strong> marked{' '}
                  <Code>verified</Code> or <Code>unverified</Code>, telling you whether it found the
                  address stated outright or had to guess the pattern
                </li>
              </ul>
              <p>Searches are limited to {searchCap}.</p>
            </Section>

            <Section id="step-6" title="6. Review and approve">
              <p className="text-mute">
                Only applies to leads the sourcing agent found. If you brought your own list, you
                already approved them by leaving Status blank, so skip to step 7.
              </p>
              <p>
                On the <strong className="text-cream">Leads</strong> page you see everything waiting
                for review: the name, company, why the agent picked them, and the email confidence.
              </p>
              <ul className="space-y-1.5 pl-4 list-disc marker:text-mute">
                <li>
                  <strong className="text-cream">Approve</strong> clears the Status, which puts the
                  lead in the send queue.
                </li>
                <li>
                  <strong className="text-cream">Discard</strong> marks it <Code>Discarded</Code>, and
                  it is never emailed.
                </li>
              </ul>
              <Callout tone="warn">
                Pay attention to <Code>unverified</Code> confidence. That means the address is a
                pattern guess, and sending to it risks a bounce.
              </Callout>
            </Section>

            <Section id="step-7" title="7. Send your first batch">
              <p>
                Go to <strong className="text-cream">Outreach</strong>. Before anything sends you get
                a preview: how many contacts are eligible right now, how many already went out in the
                last 24 hours, and how many are held back by the daily cap. Sending requires an
                explicit confirmation, so nothing goes out on a stray click.
              </p>
              <p>
                For each contact the agent writes the email, sends it from your Gmail, and
                immediately marks the row <Code>Sent</Code> with the thread ID, timestamp, and the
                body it used. It sends four at a time, and emails you a summary when the batch
                finishes.
              </p>
              <Callout>
                <strong className="text-cream">The cap is {dailyCap} per 24 hours.</strong> This is a
                deliberate rate limit, not a licensing limit. Sending large volumes from a fresh
                address is how domains get flagged as spam.
              </Callout>
            </Section>

            <Section id="step-8" title="8. Handle replies">
              <p>
                Click <strong className="text-cream">Check replies</strong> on the Outreach page. The
                agent looks at every thread it has sent, finds new responses, strips out the quoted
                history, and drafts a reply for each.
              </p>
              <p>
                Drafts land in a review queue. You can edit the text and send, or dismiss without
                sending. Sent replies go out in the original thread, so the conversation stays intact
                for the person receiving it. Nothing reaches a prospect without you reading it first.
              </p>
            </Section>

            <Section id="status" title="Reference: what the Status column means">
              <p>
                Status is the control switch for the whole system, and the single most important
                column to understand.
              </p>
              <Table
                head={['Status', 'Meaning']}
                rows={[
                  [<em className="text-cream">(blank)</em>, <><strong className="text-cream">Queued.</strong> Will be emailed on the next run</>],
                  [<Code>Needs verification</Code>, 'Found by the lead agent, waiting for your approval'],
                  [<Code>Ready for review</Code>, 'Same, also accepted by the review queue'],
                  [<Code>Sent</Code>, 'Already emailed. SentAt and ThreadID record when and which thread'],
                  [<Code>Replied</Code>, 'They answered. The reply is waiting for you in Outreach'],
                  [<Code>Discarded</Code>, 'Skipped. Never emailed'],
                ]}
              />
              <Callout tone="warn">
                <strong className="text-cream">A blank Status is the only thing that makes a row
                eligible to send.</strong> If you paste in a list of 500 contacts with an empty Status
                column, all 500 are queued the moment you save. Add a status first, then clear it row
                by row as you approve.
              </Callout>
            </Section>

            <Section id="columns" title="Reference: who owns which column">
              <Table
                head={['Column', 'Filled in by']}
                rows={[
                  ['Name, Email, Company', 'You'],
                  ['Status', 'Both you and Agent Hub'],
                  ['ThreadID, SentAt, EmailBody', 'Agent Hub, when it sends'],
                  ['LeadReason, EmailConfidence', 'The lead sourcing agent'],
                ]}
              />
            </Section>

            <Section id="limits" title="Reference: limits">
              <Table
                head={['Limit', 'Value']}
                rows={[
                  ['Outreach emails', plan ? `${plan.daily_send_limit} per 24 hours` : 'per 24 hours'],
                  ['Send batches', '5 per hour'],
                  ['Lead searches', plan ? `${plan.lead_searches_per_hour} per hour` : 'per hour'],
                  ['Leads per search', '1 to 25'],
                  ['Reply checks', '45 per hour'],
                  ['Signup attempts', '5 per hour per IP'],
                  ['Login attempts', '10 per 5 minutes per IP'],
                ]}
              />
            </Section>

            <Section id="troubleshooting" title="Troubleshooting">
              <dl className="space-y-4">
                {[
                  [
                    '"Before Agent Hub can write statuses, row 1 must contain the full header."',
                    'Your sheet is missing one or more of the nine labels, or they are out of order. Paste the full header into row 1, or download the starter sheet from Settings.',
                  ],
                  [
                    '"The sheet\'s header row doesn\'t match what Agent Hub expects."',
                    'Same cause, caught on a read instead of a write. The message shows what it found so you can compare.',
                  ],
                  [
                    '"This account hasn\'t connected a Google Sheet yet."',
                    'Pick a sheet in Settings.',
                  ],
                  [
                    '"Could not load your sheets."',
                    'Usually the Drive API is not enabled on the instance, or your Google token expired. Try Reconnect on the Settings page.',
                  ],
                  [
                    'Google says "This app hasn\'t been verified."',
                    'Expected while Agent Hub is in Google\'s testing program. Click Advanced, then Go to Agent Hub.',
                  ],
                  [
                    '"Access blocked" from Google.',
                    'Your Google account has not been added as a test user yet. Ask whoever runs the instance.',
                  ],
                  [
                    'A row was emailed that you did not expect.',
                    'Check its Status was not blank. Blank means queued. This is the most common surprise.',
                  ],
                  [
                    'An email sent but the row still looks unsent.',
                    'Agent Hub marks the row straight after sending. If that write fails it raises an error saying the email was sent, so you can fix the row by hand instead of emailing the person twice. Mark it Sent yourself.',
                  ],
                ].map(([q, a]) => (
                  <div key={q}>
                    <dt className="text-cream font-medium mb-1">{q}</dt>
                    <dd className="text-sand">{a}</dd>
                  </div>
                ))}
              </dl>
            </Section>

            <section className="rounded-xl p-5 bg-elevated border border-line">
              <h2 className="font-display text-lg mb-3 text-cream" style={{ letterSpacing: '-0.01em' }}>
                What Agent Hub will never do
              </h2>
              <ul className="space-y-2 text-sm text-sand">
                {[
                  'Email anyone whose row you have not approved',
                  'Send a reply you have not read',
                  'Edit the Name, Email or Company you typed. Status updates only ever touch columns D, E, F, G and I',
                  'Write anything at all until row 1 carries the full header',
                  'Exceed the daily send cap',
                ].map((item) => (
                  <li key={item} className="flex gap-2.5">
                    <span className="shrink-0 mt-1.5 w-1 h-1 rounded-full" style={{ background: 'var(--color-ok)' }} />
                    <span>{item}</span>
                  </li>
                ))}
              </ul>
            </section>

            <p className="mt-10 text-xs text-mute">
              Still stuck? Everything on this page reflects how the app behaves today. If something
              here does not match what you see, that is a bug worth reporting.
            </p>
          </div>
        </div>
      </main>
    </>
  )
}
