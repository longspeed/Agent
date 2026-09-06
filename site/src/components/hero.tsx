import { useState } from 'react'
import { motion } from 'motion/react'
import { Menu, X, Search, ShieldCheck, PenLine, CalendarCheck } from 'lucide-react'
import {
  LogoMark,
  PrimaryButton,
  GhostButton,
  SectionEyebrow,
  usePrefersReducedMotion,
} from './primitives'

const NAV_LINKS = [
  { label: 'Product', href: '#product' },
  { label: 'How it works', href: '#how-it-works' },
  { label: 'Pricing', href: '#pricing' },
  { label: 'Security', href: '#security' },
  { label: 'Docs', href: '/getting-started' },
]

const navLinkClasses =
  'text-sm text-sand hover:text-cream transition-colors rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg active:text-cream'

/* The app's own background treatment (login.html): warm orange glow top-left,
   steel glow top-right, on #0F0E0C. Fixed behind everything. */
export function BackgroundGlow() {
  return (
    <div
      className="fixed inset-0 z-0 pointer-events-none"
      aria-hidden="true"
      style={{
        background:
          'radial-gradient(ellipse 800px 500px at 15% -10%, rgba(194,73,29,0.12), transparent), radial-gradient(ellipse 600px 400px at 90% 10%, rgba(82,113,132,0.08), transparent)',
      }}
    />
  )
}

export function GuideLines() {
  return (
    <>
      <div className="pointer-events-none fixed inset-y-0 left-1/2 -translate-x-[calc(50%+36rem)] w-px bg-cream/5 z-[5] hidden md:block" />
      <div className="pointer-events-none fixed inset-y-0 left-1/2 translate-x-[calc(-50%+36rem)] w-px bg-cream/5 z-[5] hidden md:block" />
    </>
  )
}

export function Navbar() {
  const [open, setOpen] = useState(false)
  return (
    <header className="relative z-30">
      <div className="max-w-6xl mx-auto px-6 py-5 flex items-center justify-between">
        <a
          href="/"
          className="flex items-center gap-2.5 rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg"
        >
          <LogoMark className="w-8 h-8" />
          <span className="font-display text-lg tracking-[-0.02em]">Sendkeep</span>
        </a>
        <nav className="hidden md:flex items-center gap-8" aria-label="Main">
          {NAV_LINKS.map((l) => (
            <a key={l.label} href={l.href} className={navLinkClasses}>
              {l.label}
            </a>
          ))}
        </nav>
        <div className="hidden md:flex items-center gap-3">
          <GhostButton href="/login">Sign in</GhostButton>
          <PrimaryButton href="/signup">Connect Gmail</PrimaryButton>
        </div>
        <button
          type="button"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          aria-label={open ? 'Close menu' : 'Open menu'}
          className="md:hidden w-10 h-10 rounded-full border border-line bg-floating flex items-center justify-center hover:bg-elevated hover:border-line-hover active:scale-95 transition-[transform,background-color,border-color] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg"
        >
          {open ? <X size={18} /> : <Menu size={18} />}
        </button>
      </div>
      {open && (
        <div className="md:hidden mx-6 mb-4 rounded-2xl border border-line bg-floating/95 backdrop-blur-xl p-4 flex flex-col gap-1">
          {NAV_LINKS.map((l) => (
            <a
              key={l.label}
              href={l.href}
              onClick={() => setOpen(false)}
              className="px-3 py-2.5 rounded-xl text-sm text-sand hover:bg-elevated hover:text-cream active:bg-elevated transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              {l.label}
            </a>
          ))}
          <div className="flex gap-3 pt-3 mt-2 border-t border-line">
            <GhostButton href="/login">Sign in</GhostButton>
            <PrimaryButton href="/signup">Connect Gmail</PrimaryButton>
          </div>
        </div>
      )}
    </header>
  )
}

/* Warm shine: accent -> gold -> accent, every stop stays legible on #0F0E0C */
const shinyStyle: React.CSSProperties = {
  backgroundImage:
    'linear-gradient(to right, #C2491D 0%, #A93E17 45%, #C2491D 100%)',
  backgroundSize: '200% auto',
  WebkitBackgroundClip: 'text',
  backgroundClip: 'text',
  color: 'transparent',
  WebkitTextFillColor: 'transparent',
  filter: 'url(#noise-hero)',
}

export function Hero() {
  const reduced = usePrefersReducedMotion()
  return (
    <section className="relative z-10 pt-16 md:pt-28 pb-20 text-center">
      <div className="max-w-6xl mx-auto px-6 flex flex-col items-center">
        {/* LCP text: opacity 1 on first paint, transform-only entrance */}
        <motion.h1
          initial={reduced ? false : { y: 20 }}
          animate={{ y: 0 }}
          transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
          className="font-display text-4xl md:text-7xl font-medium tracking-tight leading-[0.95]"
        >
          <span className="block">Know what you owe.</span>
          <span className="animate-shiny block mt-2 pb-2" style={shinyStyle}>
            Remember what they owe.
          </span>
        </motion.h1>
        <motion.p
          initial={reduced ? false : { y: 16 }}
          animate={{ y: 0 }}
          transition={{ duration: 0.6, delay: 0.1, ease: [0.22, 1, 0.36, 1] }}
          className="mt-8 text-sand max-w-lg text-base leading-[1.6]"
        >
          Sendkeep is a Gmail-native promise ledger for people sending 15-50 emails a day.
          It watches recent conversations and keeps the owner, action, evidence, due date,
          and next reminder together. Reply in Gmail or Gemini; confirm and track promises here.
        </motion.p>
        <motion.div
          initial={reduced ? false : { y: 12 }}
          animate={{ y: 0 }}
          transition={{ duration: 0.6, delay: 0.18, ease: [0.22, 1, 0.36, 1] }}
          className="mt-10 flex flex-wrap items-center justify-center gap-4"
        >
          <PrimaryButton href="/signup">Connect Gmail</PrimaryButton>
          <GhostButton href="#how-it-works">See how it works</GhostButton>
        </motion.div>
        <p className="mt-6 text-xs text-sand">
          Gmail Sent · One reminder per contact · No reply queue
        </p>
      </div>
    </section>
  )
}

const STEPS = [
  {
    icon: Search,
    title: 'Connect Gmail',
    body: 'Connect the Gmail mailbox where your real conversations already happen. Sendkeep reads a bounded Sent window; Gmail remains the place where you write and reply.',
  },
  {
    icon: ShieldCheck,
    title: 'Remember what you promised',
    body: 'Sendkeep watches a bounded window of Gmail Sent threads, even when another tool sent them, and preserves the evidence behind each promise.',
  },
  {
    icon: CalendarCheck,
    title: 'See one next action',
    body: 'Confirm a promise or get one disciplined follow-up reminder. No sequences, no warmup, no second reply inbox.',
  },
  {
    icon: PenLine,
    title: 'Reply where you work',
    body: 'Open the original Gmail thread, send when you are ready, and let Sendkeep record the outcome.',
  },
]

export function HowItWorks() {
  return (
    <section id="how-it-works" className="relative z-10 py-16 md:py-24">
      <div className="max-w-6xl mx-auto px-6">
        <SectionEyebrow label="How it works" tag="4 steps" />
        <div className="mt-8 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {STEPS.map((step, i) => (
            <div key={step.title} className="app-card rounded-2xl p-5">
              <div className="flex items-center gap-3">
                <span className="w-9 h-9 rounded-full bg-accent/12 text-accent flex items-center justify-center">
                  <step.icon size={16} aria-hidden="true" />
                </span>
                <span className="text-xs text-mute font-medium">0{i + 1}</span>
              </div>
              <h3 className="mt-4 font-display font-medium text-[17px]">{step.title}</h3>
              <p className="mt-2 text-sm text-sand leading-[1.55]">{step.body}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}
