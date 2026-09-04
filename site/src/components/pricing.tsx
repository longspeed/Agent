import { useState } from 'react'
import { Check } from 'lucide-react'
import { NoisePricingFilter } from './primitives'

const PUBLIC_BOOKING_URL = (import.meta.env.VITE_PUBLIC_BOOKING_URL || '').trim()

type Plan = {
  name: string
  monthly: string
  yearly: string
  blurb: string
  features: string[]
  cta: string
  href: string
  featured?: boolean
}

const PLANS: Plan[] = [
  {
    name: 'Trial',
    monthly: 'Free',
    yearly: 'Free',
    blurb: 'Connect one Gmail inbox and see promises stay visible.',
    features: [
      'Recent Sent-thread discovery',
      'Promise and follow-up memory',
      'Promised-action tracking',
      'One inbox, no Sheet required',
      'Capped first-touch trial',
    ],
    cta: 'Connect Gmail',
    href: '/signup',
  },
  {
    name: 'Inbox',
    monthly: '$29/mo',
    yearly: '$290/yr',
    blurb: 'Promise memory for one Gmail inbox.',
    features: [
      'Gmail Sent-thread discovery',
      'Promised dates, actions, and evidence',
      'One follow-up reminder per contact',
      'Opt-outs, duplicate, and bounce checks',
      'Audit trail and export',
      'Priority email support',
    ],
    cta: 'See your promises',
    href: '/outreach?tab=promises',
  },
  {
    name: 'Agency pilot',
    monthly: '$49/inbox/mo',
    yearly: '$490/inbox/yr',
    blurb: 'Concierge onboarding for agencies managing client Gmail inboxes.',
    features: [
      'Everything in Inbox',
      'Proof ledger: approvals, edits, and reconciliations',
      'Operator-confirmed meetings and deals',
      'Exportable account evidence',
      'Worker health and recovery alerts',
      'No warmup, rotation, or volume claims',
    ],
    cta: PUBLIC_BOOKING_URL ? 'Book agency pilot' : 'Connect Gmail',
    href: PUBLIC_BOOKING_URL || '/signup',
    featured: true,
  },
]

export function Pricing() {
  const [yearly, setYearly] = useState(false)
  return (
    <section id="pricing" className="ab-pricing-section relative z-10 py-16 md:py-24 px-6">
      <NoisePricingFilter />
      <div className="ab-watermark-main" aria-hidden="true">
        <span className="ab-watermark-line1">Pricing</span>
        <span className="ab-watermark-line2">Made simple.</span>
      </div>
      <h2 className="sr-only">Pricing</h2>

      <div className="ab-toggle-wrap">
        <span className={`text-sm ${yearly ? 'text-sand' : 'text-cream'}`}>Monthly</span>
        <button
          type="button"
          role="switch"
          aria-checked={yearly}
          aria-label="Bill yearly"
          onClick={() => setYearly(!yearly)}
          className={`ab-toggle ${yearly ? 'active' : ''}`}
        >
          <span className="ab-toggle-knob" />
        </button>
        <span className={`text-sm ${yearly ? 'text-cream' : 'text-sand'}`}>
          Yearly <span className="text-sand">(2 months free)</span>
        </span>
      </div>

      <div className="ab-grid">
        {PLANS.map((plan) => (
          <div key={plan.name} className={`ab-card ${plan.featured ? 'ab-card-featured' : ''}`}>
            <span className="ab-tier-small">{plan.name}</span>
            <span className="ab-tier-large">{yearly ? plan.yearly : plan.monthly}</span>
            <p className="ab-desc">{plan.blurb}</p>
            <ul className="ab-list">
              {plan.features.map((f) => (
                <li key={f}>
                  <span className="ab-check">
                    <Check size={13} aria-hidden="true" />
                  </span>
                  {f}
                </li>
              ))}
            </ul>
            <a href={plan.href} className="ab-btn inline-flex items-center justify-center">
              {plan.cta}
            </a>
          </div>
        ))}
      </div>
      <p className="mt-6 max-w-2xl text-center text-sm leading-relaxed text-sand">
        Inbox is $29/mo per Gmail inbox. Agency pilot is $49/mo per managed Gmail inbox with
        concierge onboarding. Both prices are validation offers, not volume-sending plans.
      </p>
    </section>
  )
}
