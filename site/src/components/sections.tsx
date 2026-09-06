import { useState } from 'react'
import {
  Mail,
  Table,
  KeyRound,
  ChevronDown,
  ShieldCheck,
  EyeOff,
  Unplug,
  Gauge,
  MessageSquareReply,
  CalendarCheck,
  PenLine,
  Download,
} from 'lucide-react'
import { SectionEyebrow, PrimaryButton, GhostButton } from './primitives'

const PUBLIC_BOOKING_URL = (import.meta.env.VITE_PUBLIC_BOOKING_URL || '').trim()

const CHIPS = [
  'Gmail Sent only',
  'One follow-up per contact',
  'No second reply inbox',
  'Opt-outs enforced',
  'Evidence stays attached',
]

const QUEUE_GUARANTEES = [
  { label: 'Recent Gmail threads stay on a durable watch list', color: 'text-grass' },
  { label: 'Promised actions keep their original evidence', color: 'text-steel' },
  { label: 'One follow-up reminder prevents the quiet thread from disappearing', color: 'text-sand' },
  { label: 'Gmail or Gemini remains your reply surface', color: 'text-gold' },
]

export function Trust() {
  return (
    <section className="relative z-10 py-16 md:py-24">
      <div className="max-w-6xl mx-auto px-6 grid grid-cols-1 md:grid-cols-2 gap-10 items-center">
        <div>
          <SectionEyebrow label="The memory layer" />
          <h2 className="mt-6 font-display text-3xl md:text-5xl font-medium tracking-tight leading-[1.05]">
            The promise is the event.
            <br />
            The memory is the work.
          </h2>
          <p className="mt-6 text-sand text-base leading-[1.6] max-w-md">
            Sendkeep starts after you send. It reads the Gmail threads you already started, spots
            what you promised, and keeps one next follow-up visible. You write and reply in Gmail;
            Sendkeep remembers the revenue-bearing work Gmail alone lets disappear.
          </p>
          <div className="mt-8 flex flex-wrap gap-2">
            {CHIPS.map((chip) => (
              <span
                key={chip}
                className="px-3 py-1.5 rounded-full border border-line bg-elevated text-[13px] text-sand"
              >
                {chip}
              </span>
            ))}
          </div>
        </div>
        <div className="app-card rounded-2xl p-5">
          <p className="text-[11px] uppercase tracking-widest text-mute">
            What Sendkeep remembers
          </p>
          <div className="mt-4 flex flex-col gap-3">
            {QUEUE_GUARANTEES.map((row) => (
              <div
                key={row.label}
                className="flex items-center gap-3 rounded-xl border border-line bg-floating px-4 py-3"
              >
                <ShieldCheck size={16} className={row.color} aria-hidden="true" />
                <span className="text-sm text-sand">{row.label}</span>
              </div>
            ))}
          </div>
          <p className="mt-4 text-[12px] text-mute">
            No Sheet required. Connect Gmail and the memory layer starts with recent Sent threads.
          </p>
        </div>
      </div>
    </section>
  )
}

export function ProofLoop() {
  const signals = [
    {
      icon: MessageSquareReply,
      label: 'Promises detected',
      detail: 'Evidence-backed actions extracted from the Gmail Sent and reply threads you monitor.',
    },
    {
      icon: CalendarCheck,
      label: 'Follow-ups scheduled',
      detail: 'At most one reminder per contact, with cancellations recorded when a reply arrives.',
    },
    {
      icon: ShieldCheck,
      label: 'Replies after follow-up',
      detail: 'Observed outcomes only; Sendkeep never turns workflow evidence into a guaranteed lift.',
    },
    {
      icon: PenLine,
      label: 'Minutes saved',
      detail: 'Operator-estimated time saved by remembering promises instead of searching Gmail.',
    },
  ]

  return (
    <section className="relative z-10 py-16 md:py-24">
      <div className="max-w-6xl mx-auto px-6">
        <div className="grid grid-cols-1 lg:grid-cols-[1fr_1.1fr] gap-10 lg:gap-16 items-start">
          <div>
            <h2 className="font-display text-3xl md:text-5xl font-medium tracking-tight leading-[1.05]">
              Show the work after the send.
            </h2>
            <p className="mt-6 text-sand text-base leading-[1.6] max-w-md">
              A pilot can prove value without inventing reply-rate claims: promises kept visible,
              one follow-up completed, replies after follow-ups, and minutes no longer spent on
              inbox archaeology.
            </p>
          </div>
          <div className="border-t border-line">
            {signals.map((signal) => (
              <div
                key={signal.label}
                className="grid grid-cols-[auto_1fr] sm:grid-cols-[auto_10rem_1fr] items-center gap-4 border-b border-line py-5"
              >
                <signal.icon size={17} className="text-accent" aria-hidden="true" />
                <span className="text-sm font-medium text-cream">{signal.label}</span>
                <span className="text-sm text-sand sm:text-right">{signal.detail}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  )
}

const INTEGRATIONS = [
  { icon: Mail, label: 'Gmail' },
  { icon: KeyRound, label: 'Google OAuth' },
  { icon: Table, label: 'Google Sheets (optional)' },
]

export function Integrations() {
  return (
    <section className="relative z-10 py-12 md:py-16">
      <div className="max-w-6xl mx-auto px-6 text-center">
        <p className="text-xs uppercase tracking-widest text-sand">
          Works inside the tools you already use
        </p>
        <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
          {INTEGRATIONS.map((it) => (
            <span
              key={it.label}
              className="flex items-center gap-2 px-4 py-2.5 rounded-full border border-line bg-elevated text-sm text-sand"
            >
              <it.icon size={15} className="text-accent" aria-hidden="true" />
              {it.label}
            </span>
          ))}
        </div>
      </div>
    </section>
  )
}

export function AgencyWedge() {
  return (
    <section className="relative z-10 py-16 md:py-24">
      <div className="max-w-6xl mx-auto px-6">
        <div className="app-card rounded-3xl p-7 md:p-10 grid grid-cols-1 lg:grid-cols-[1.1fr_.9fr] gap-10 items-center">
          <div>
            <SectionEyebrow label="For agencies" />
            <h2 className="mt-6 font-display text-3xl md:text-5xl font-medium tracking-tight leading-[1.05]">
              One follow-through habit,
              <br />
              repeated across every client inbox.
            </h2>
            <p className="mt-6 text-sand text-base leading-[1.6] max-w-lg">
              Keep the sending workflow you already trust. Start one client inbox at a time: each
              managed Gmail inbox gets its own isolated promise graph, worker watch, and proof ledger.
              We provision agency pilots with you while shared multi-inbox controls are validated.
            </p>
            {PUBLIC_BOOKING_URL ? (
              <a
                href={PUBLIC_BOOKING_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-8 inline-flex items-center justify-center rounded-lg bg-cream px-4 py-2.5 text-sm font-medium text-bg transition-colors hover:bg-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              >
                Book an agency interview
              </a>
            ) : (
              <div className="mt-8">
                <PrimaryButton href="/signup">Connect Gmail</PrimaryButton>
              </div>
            )}
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-1 gap-3">
            <div className="rounded-2xl border border-line bg-floating p-5">
              <div className="text-xs uppercase tracking-widest text-mute">Agency pricing</div>
                <div className="mt-3 font-display text-3xl text-cream">$49 / inbox / mo</div>
              <p className="mt-2 text-sm leading-[1.55] text-sand">
                  Concierge onboarding for agencies managing client Gmail inboxes. Each inbox stays
                  isolated with its own promise graph, worker watch, and proof ledger.
              </p>
            </div>
            <div className="rounded-2xl border border-line bg-floating p-5">
              <div className="text-xs uppercase tracking-widest text-mute">Interview topic</div>
                <div className="mt-3 font-display text-3xl text-accent">Proof workload</div>
              <p className="mt-2 text-sm leading-[1.55] text-sand">
                  Price follows operational burden and evidence delivered, never an invented recovered-revenue claim.
              </p>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

const SECURITY_CARDS = [
  {
    icon: KeyRound,
    title: 'Your Gmail, your tokens',
    body: 'Sendkeep connects through Google OAuth, stores tokens encrypted, and watches the mailbox you choose. Revoke access whenever you want.',
  },
  {
    icon: EyeOff,
    title: 'We do not sell your data',
    body: 'Your Gmail thread metadata, reply history, and optional Sheet belong to your account. No resale or enrichment-provider handoff.',
  },
  {
    icon: Unplug,
    title: 'Revoke access in one click',
    body: 'Disconnect your Google account at any time from Sendkeep or from your Google security settings.',
  },
  {
    icon: Gauge,
    title: 'Google-safe by design',
    body: 'The core is read-only against Gmail Sent. Optional first-touch sending is capped on purpose at 50 sends per rolling 24 hours for paid inboxes and pauses on opt-outs or bounces. This is not warmup, rotation, or an inbox-placement guarantee.',
  },
  {
    icon: Download,
    title: 'Leave with a copy',
    body: 'Export tracked conversations, promises, follow-up outcomes, and audit records. Your Gmail threads stay in Gmail; you are never dependent on a proprietary inbox.',
  },
]

export function Security() {
  return (
    <section id="security" className="relative z-10 py-16 md:py-24">
      <div className="max-w-6xl mx-auto px-6">
        <SectionEyebrow label="Security & data" />
        <div className="mt-8 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {SECURITY_CARDS.map((card) => (
            <div key={card.title} className="app-card rounded-2xl p-5">
              <span className="w-9 h-9 rounded-full bg-accent/12 text-accent flex items-center justify-center">
                <card.icon size={16} aria-hidden="true" />
              </span>
              <h3 className="mt-4 font-display font-medium text-[17px]">{card.title}</h3>
              <p className="mt-2 text-sm text-sand leading-[1.55]">{card.body}</p>
            </div>
          ))}
        </div>
        <p className="mt-6 text-sm text-sand">
          Read the details in our{' '}
          <a
            href="/privacy"
            className="underline underline-offset-2 text-cream/90 hover:text-cream focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
          >
            privacy policy
          </a>{' '}
          and{' '}
          <a
            href="/security"
            className="underline underline-offset-2 text-cream/90 hover:text-cream focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
          >
            security overview
          </a>
          .
        </p>
      </div>
    </section>
  )
}

const FAQ_ITEMS = [
  {
    q: 'Does Sendkeep replace warmup or rotation?',
    a: 'No. Sendkeep is a memory and follow-through utility for the Gmail inboxes you already use. It does not promise inbox placement, warmup, domain protection, or multi-inbox rotation.',
  },
  {
    q: 'Will it watch emails sent from another tool?',
    a: 'Yes. After Gmail is connected, the worker discovers a bounded window of recent Sent threads and keeps them in durable tracking. A Google Sheet is optional and is not the source of truth for the promise layer.',
  },
  {
    q: 'Does it send anything without me?',
    a: 'The core reads Gmail Sent and never sends. Optional first-touch drafts remain capped and require manual review in the standard deployment; they are a secondary lane, not the product promise.',
  },
  {
    q: 'What happens when someone replies?',
    a: 'Sendkeep watches the thread and extracts promised dates or actions when present. Reply in the original Gmail or Gemini thread; confirm or dismiss the promise in Sendkeep, and see one follow-up reminder when the thread goes quiet.',
  },
  {
    q: 'Do I need a Google Sheet?',
    a: 'No. Sheets are optional and only support the legacy first-touch workflow; Gmail Sent discovery is the core product.',
  },
  {
    q: 'How do agencies manage multiple inboxes?',
    a: 'Each client inbox is isolated in its own Sendkeep account and promise log. Agency pilots are priced per managed inbox while we validate shared multi-inbox controls, so Sendkeep is not pretending to be a sending console today.',
  },
  {
    q: 'How do I cancel?',
    a: 'Disconnect Gmail from Settings at any time to immediately stop Sendkeep monitoring and sends. Download your Sendkeep data there first, then email support to request account-data deletion. Mail already in Gmail stays in Gmail.',
  },
]

export function Faq() {
  const [openIndex, setOpenIndex] = useState<number | null>(0)
  return (
    <section className="relative z-10 py-16 md:py-24">
      <div className="max-w-6xl mx-auto px-6">
        <SectionEyebrow label="FAQ" />
        <div className="mt-8 max-w-3xl flex flex-col gap-3">
          {FAQ_ITEMS.map((item, i) => {
            const open = openIndex === i
            return (
              <div key={item.q} className="app-card rounded-2xl overflow-hidden">
                <button
                  type="button"
                  aria-expanded={open}
                  onClick={() => setOpenIndex(open ? null : i)}
                  className="w-full flex items-center justify-between gap-4 px-5 py-4 text-left text-[15px] font-medium hover:bg-floating active:bg-floating transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-inset"
                >
                  {item.q}
                  <ChevronDown
                    size={16}
                    className={`shrink-0 text-sand transition-transform ${open ? 'rotate-180' : ''}`}
                    aria-hidden="true"
                  />
                </button>
                {open && <p className="px-5 pb-5 text-sm text-sand leading-[1.6]">{item.a}</p>}
              </div>
            )
          })}
        </div>
      </div>
    </section>
  )
}

export function FinalCta() {
  return (
    <section className="relative z-10 max-w-6xl mx-auto px-6 py-20 md:py-32">
      <div className="app-card relative overflow-hidden rounded-3xl px-8 py-16 md:py-24 text-center">
        <div
          aria-hidden="true"
          className="absolute inset-0 pointer-events-none"
          style={{
            background:
              'radial-gradient(600px circle at 50% 0%, rgba(194,73,29,.12), transparent 70%)',
          }}
        />
        <h2 className="relative font-display text-4xl md:text-6xl font-medium tracking-tight leading-[1.05]">
          Know what you owe.
          <br />
          Remember what they owe.
        </h2>
        <p className="relative mt-6 text-sand max-w-md mx-auto text-sm leading-[1.6]">
          Connect Gmail, see your promises with their evidence and due dates, and keep the next action
          visible without changing the way you reply.
        </p>
        <div className="relative mt-10 flex flex-wrap items-center justify-center gap-4">
          <PrimaryButton href="/signup">Connect Gmail</PrimaryButton>
          <GhostButton href={PUBLIC_BOOKING_URL || '/outreach?tab=promises'}>
            {PUBLIC_BOOKING_URL ? 'Book an agency pilot' : 'See your promises'}
          </GhostButton>
        </div>
      </div>
    </section>
  )
}

const FOOTER_COLS: { title: string; links: { label: string; href: string }[] }[] = [
  {
    title: 'Product',
    links: [
      { label: 'How it works', href: '#how-it-works' },
      { label: 'Pricing', href: '#pricing' },
      { label: 'Security', href: '#security' },
      { label: 'Docs', href: '/getting-started' },
    ],
  },
  {
    title: 'Company',
    links: [
      { label: 'Contact', href: 'mailto:support@sendkeep.app' },
      {
        label: PUBLIC_BOOKING_URL ? 'Book an agency pilot' : 'See your promises',
        href: PUBLIC_BOOKING_URL || '/outreach?tab=promises',
      },
      { label: 'Sign in', href: '/login' },
    ],
  },
  {
    title: 'Legal',
    links: [
      { label: 'Privacy Policy', href: '/privacy' },
      { label: 'Terms of Service', href: '/terms' },
      { label: 'Acceptable Use (anti-spam)', href: '/acceptable-use' },
      { label: 'Data Processing Agreement', href: '/dpa' },
    ],
  },
]

export function Footer() {
  return (
    <footer className="relative z-10 border-t border-line">
      <div className="max-w-6xl mx-auto px-6 py-14 grid grid-cols-2 md:grid-cols-4 gap-10">
        {FOOTER_COLS.map((col) => (
          <div key={col.title}>
            <p className="text-[13px] font-semibold text-cream/90">{col.title}</p>
            <ul className="mt-4 flex flex-col gap-2.5">
              {col.links.map((l) => (
                <li key={l.label}>
                  <a
                    href={l.href}
                    className="text-[13px] text-sand hover:text-cream transition-colors rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                  >
                    {l.label}
                  </a>
                </li>
              ))}
            </ul>
          </div>
        ))}
        <div>
          <p className="text-[13px] font-semibold text-cream/90">Contact</p>
          <ul className="mt-4 flex flex-col gap-2.5 text-[13px] text-sand">
            <li>
              <a
                href="mailto:support@sendkeep.app"
                className="hover:text-cream transition-colors rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              >
                support@sendkeep.app
              </a>
            </li>
          </ul>
        </div>
      </div>
      <div className="border-t border-line">
        <div className="max-w-6xl mx-auto px-6 py-6 flex flex-col sm:flex-row items-center justify-between gap-3 text-[12px] text-sand">
          <span>© {new Date().getFullYear()} Sendkeep. All rights reserved.</span>
          <span className="flex items-center gap-1.5">
            <ShieldCheck size={13} className="text-grass" aria-hidden="true" />
        Gmail Sent monitoring + one disciplined follow-up. Capped on purpose. Opt-outs stay suppressed.
          </span>
        </div>
      </div>
    </footer>
  )
}
