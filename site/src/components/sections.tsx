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
} from 'lucide-react'
import { SectionEyebrow, PrimaryButton, GhostButton } from './primitives'

const CHIPS = [
  'Manual review by default',
  'Auto mode is optional',
  'Opt-outs enforced',
  'Daily send caps',
  'Auto-pause on bounces',
]

const QUEUE_GUARANTEES = [
  { label: 'Replies are queued instead of buried in Gmail', color: 'text-grass' },
  { label: 'Drafted follow-ups stay grounded in the thread', color: 'text-steel' },
  { label: 'Opt-outs are suppressed permanently', color: 'text-sand' },
  { label: 'Every send goes out from your own Gmail', color: 'text-gold' },
]

export function Trust() {
  return (
    <section className="relative z-10 py-16 md:py-24">
      <div className="max-w-6xl mx-auto px-6 grid grid-cols-1 md:grid-cols-2 gap-10 items-center">
        <div>
          <SectionEyebrow label="Control" />
          <h2 className="mt-6 font-display text-3xl md:text-5xl font-medium tracking-tight leading-[1.05]">
            The agent drafts.
            <br />
            You decide.
          </h2>
          <p className="mt-6 text-sand text-base leading-[1.6] max-w-md">
            The default workflow keeps you in control: review first touches, handle replies from
            one queue, and decide when auto-send is safe enough for a batch. The product is the
            follow-up loop, not a promise that one inbox can behave like a cold-email platform.
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
            What the reply queue gives you
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
            No activity to show yet — this is what happens the first time you use it, too.
          </p>
        </div>
      </div>
    </section>
  )
}

const INTEGRATIONS = [
  { icon: Mail, label: 'Gmail' },
  { icon: Table, label: 'Google Sheets' },
  { icon: KeyRound, label: 'Google OAuth' },
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

const SECURITY_CARDS = [
  {
    icon: KeyRound,
    title: 'Your Gmail, your tokens',
    body: 'Outreach sends from your own Gmail via Google OAuth. Tokens are stored encrypted and never leave our infrastructure.',
  },
  {
    icon: EyeOff,
    title: 'We never sell or share lead data',
    body: 'Your lead sheet and reply history belong to you. No resale, no enrichment providers, no third-party sharing.',
  },
  {
    icon: Unplug,
    title: 'Revoke access in one click',
    body: 'Disconnect your Google account at any time — from Sendkeep or from your Google security settings.',
  },
  {
    icon: Gauge,
    title: 'Risk reduction, not magic',
    body: 'Caps, opt-outs, and bounce pauses reduce obvious damage. They do not replace dedicated sending domains or warmup for high-volume cold email.',
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
    q: 'Does Sendkeep protect my domain?',
    a: 'It reduces obvious risk with human-volume caps, opt-out suppression, and bounce pauses. It is not multi-inbox infrastructure, warmup, or inbox placement testing. If you need high-volume cold email, use dedicated sending domains and an infrastructure tool.',
  },
  {
    q: 'Does it send anything without me?',
    a: 'Manual review is the default. You can switch first-touch outreach to auto-send mode once you trust your list and settings. Replies still land in the review queue before you send a response.',
  },
  {
    q: 'Where do the leads come from?',
    a: 'Most users should start with a list they already trust. The lead agent can search the public web and write small batches to your Google Sheet with a reason and confidence note for review.',
  },
  {
    q: 'What happens when someone replies?',
    a: 'Sendkeep watches the thread, detects the reply, and drafts a response grounded in the actual conversation. The draft appears in your review queue where you can edit it, send it, or dismiss it.',
  },
  {
    q: 'Can I use my own Gmail?',
    a: "Yes. You connect Gmail with Google OAuth; emails send from your address and replies land in your inbox. Revoke access anytime from Google's security settings.",
  },
  {
    q: 'How do I cancel?',
    a: 'Cancel from settings in one click. No calls, no forms. Disconnecting your Google account immediately stops all sending and reply-watching, and you can ask us to delete your data entirely.',
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
              'radial-gradient(600px circle at 50% 0%, rgba(232,98,44,.12), transparent 70%)',
          }}
        />
        <h2 className="relative font-display text-4xl md:text-6xl font-medium tracking-tight leading-[1.05]">
          Stop losing cold replies.
          <br />
          Start working the queue.
        </h2>
        <p className="relative mt-6 text-sand max-w-md mx-auto text-sm leading-[1.6]">
          Connect Gmail, send a small batch, and handle every reply from one review queue.
        </p>
        <div className="relative mt-10 flex flex-wrap items-center justify-center gap-4">
          <PrimaryButton href="/signup">Start free</PrimaryButton>
          <GhostButton href="mailto:support@sendkeep.app?subject=Demo%20request">
            Book a demo
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
      { label: 'Book a demo', href: 'mailto:support@sendkeep.app?subject=Demo%20request' },
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
            Manual review or confirmed auto batches. Opt-outs stay suppressed.
          </span>
        </div>
      </div>
    </footer>
  )
}
