import { useState } from 'react'
import { Check } from 'lucide-react'
import { NoisePricingFilter } from './primitives'

type Plan = {
  name: string
  monthly: string
  yearly: string
  blurb: string
  features: string[]
  cta: string
  featured?: boolean
}

const PLANS: Plan[] = [
  {
    name: 'Trial',
    monthly: 'Free',
    yearly: 'Free',
    blurb: 'Send a small batch and see the reply queue work.',
    features: [
      '50 sourced leads',
      '25 approved sends per day',
      'Manual reply queue',
      'One manual follow-up per contact',
      'Google Sheets sync',
    ],
    cta: 'Start free',
  },
  {
    name: 'Pilot',
    monthly: '$19/mo',
    yearly: '$190/yr',
    blurb: 'A reply and follow-up desk for one Gmail inbox.',
    features: [
      'Drafts first-touch outreach',
      'Unlimited reply drafts',
      'One manual follow-up per contact',
      '500 sourced leads/mo',
      '10 lead searches per hour',
      'Caps, opt-outs, and bounce pauses',
      'Priority email support',
    ],
    cta: 'Start Pilot',
    featured: true,
  },
  {
    name: 'Team',
    monthly: '$149/mo',
    yearly: '$1,490/yr',
    blurb: 'For teams after the single-inbox loop proves useful.',
    features: [
      'Everything in Pilot',
      '100 sends per day',
      '3,000 drafts per month',
      '2,000 sourced leads/mo',
      '30 lead searches per hour',
    ],
    cta: 'Start Team',
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
            <a href="/signup" className="ab-btn inline-flex items-center justify-center">
              {plan.cta}
            </a>
          </div>
        ))}
      </div>
    </section>
  )
}
