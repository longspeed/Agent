import { motion } from 'motion/react'
import {
  Sparkles,
  CalendarCheck,
  Bell,
  ListChecks,
  Settings,
  CheckCircle2,
} from 'lucide-react'
import { usePrefersReducedMotion } from './primitives'

const RAIL = [
  { icon: ListChecks, label: 'Promises', badge: 2, active: true },
  { icon: Bell, label: 'Follow-ups', badge: 1 },
  { icon: CalendarCheck, label: 'Threads' },
  { icon: Settings, label: 'Settings' },
]

const PENDING = [
  {
    name: 'Alex Morgan',
    meta: 'alex@example.com',
    snippet: '"I will send the deck Friday"',
    time: '2m',
    active: true,
    unread: true,
  },
  { name: 'Taylor R.', meta: 'taylor@example.com', snippet: '"I will send the finance approval Monday."', time: '1h' },
  { name: 'M. Reyes', meta: 'm.reyes@example.com', snippet: '"Can you follow up next week?"', time: '3h' },
]

const railItem = (active?: boolean) =>
  `flex items-center gap-2.5 px-3 py-2 rounded-xl text-[13px] transition-colors cursor-default ${
    active ? 'bg-accent/12 text-cream' : 'text-sand hover:bg-elevated hover:text-cream'
  }`

export function ProductMockup() {
  const reduced = usePrefersReducedMotion()
  return (
    <section id="product" className="relative z-10 max-w-6xl mx-auto px-6 py-16 md:py-24">
      <motion.div
        initial={reduced ? false : { y: 24 }}
        whileInView={{ y: 0 }}
        viewport={{ once: true, margin: '-80px' }}
        transition={{ duration: 0.7, delay: 0.2, ease: [0.22, 1, 0.36, 1] }}
        className="relative rounded-2xl overflow-hidden border border-line bg-elevated/95 backdrop-blur-xl"
      >
        {/* Title bar, web app, no desktop chrome */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-line">
          <span className="text-[13px] font-medium text-cream/90">Sendkeep - Follow-through</span>
          <div className="flex items-center gap-1 text-[12px]">
            {['Sent threads', 'Promises', 'Follow-ups'].map((tab) => (
              <span
                key={tab}
                className={`px-3 py-1 rounded-full ${
                  tab === 'Promises' ? 'bg-accent/15 text-accent' : 'text-sand'
                }`}
              >
                {tab}
              </span>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-12 min-h-[520px] md:h-[520px]">
          {/* Left rail, horizontal chip row below md */}
          <div className="md:col-span-3 border-b md:border-b-0 md:border-r border-line p-3 flex md:flex-col gap-1 overflow-x-auto md:overflow-visible">
            {RAIL.map((item) => (
              <span key={item.label} className={`${railItem(item.active)} shrink-0`}>
                <item.icon size={15} aria-hidden="true" />
                {item.label}
                {item.badge && (
                  <span className="ml-auto bg-accent text-bg text-[10px] font-bold px-1.5 py-0.5 rounded-full">
                    {item.badge}
                  </span>
                )}
              </span>
            ))}
          </div>

          {/* Pending reviews list */}
          <div className="md:col-span-4 border-b md:border-b-0 md:border-r border-line p-3">
            <p className="px-2 pb-2 text-[11px] uppercase tracking-widest text-mute">
              Promises to confirm
            </p>
            <div className="flex flex-col gap-1">
              {PENDING.map((p) => (
                <div
                  key={p.name}
                  className={`px-3 py-3 rounded-xl ${
                    p.active ? 'bg-floating border border-line' : 'hover:bg-floating/60'
                  } transition-colors cursor-default`}
                >
                  <div className="flex items-center gap-2">
                    {p.unread && <span className="w-1.5 h-1.5 rounded-full bg-accent" />}
                    <span className="text-[13px] font-semibold">{p.name}</span>
                    <span className="ml-auto text-[11px] text-mute">{p.time}</span>
                  </div>
                  <p className="mt-0.5 text-[12px] text-mute truncate">{p.meta}</p>
                  <p className="mt-1 text-[12px] text-sand truncate">{p.snippet}</p>
                </div>
              ))}
            </div>
          </div>

          {/* Promise card, the differentiator */}
          <div className="md:col-span-5 p-4 flex flex-col">
            <div className="flex items-center gap-2">
              <span className="text-[13px] font-semibold">Alex Morgan</span>
              <span className="text-[11px] text-mute">Gmail thread · 2m ago</span>
              <span className="ml-auto flex items-center gap-1 px-2 py-0.5 rounded-full bg-grass/15 text-grass text-[11px]">
                <CheckCircle2 size={11} aria-hidden="true" /> Evidence found
              </span>
            </div>

            <div className="mt-3 rounded-xl border border-line bg-bg/60 p-3">
              <p className="text-[11px] uppercase tracking-widest text-mute">Promise detected</p>
              <p className="mt-1.5 text-[13px] text-sand leading-[1.55]">
                "I will send the deck Friday after I check the numbers."
              </p>
            </div>

            <div className="mt-3 rounded-xl border border-line bg-bg/60 p-3 flex-1 flex flex-col">
              <p className="flex items-center gap-1.5 text-[11px] uppercase tracking-widest text-mute">
                <Sparkles size={12} className="text-gold" aria-hidden="true" /> Keep the commitment visible
              </p>
              <div className="mt-2 grid gap-2 text-[13px] text-sand">
                <div className="flex items-center justify-between gap-3"><span>Action</span><strong className="text-cream">Send the deck</strong></div>
                <div className="flex items-center justify-between gap-3"><span>Detected date</span><strong className="text-cream">Friday</strong></div>
                <div className="flex items-center justify-between gap-3"><span>Confidence</span><strong className="text-grass">High</strong></div>
              </div>
              <div className="mt-3" aria-label="Example workflow actions">
                <p className="text-[11px] uppercase tracking-widest text-mute">Example only</p>
                <div className="mt-2 flex flex-wrap items-center gap-2 text-[12px]" aria-label="Illustrative workflow steps">
                  <span className="btn-primary px-4 py-2 rounded-full font-semibold">Confirm promise</span>
                  <span className="px-4 py-2 rounded-full bg-floating border border-line text-sand">Dismiss</span>
                  <span className="px-4 py-2 rounded-full bg-floating border border-line text-sand">Open in Gmail</span>
                </div>
                <p className="mt-2 text-[11px] text-mute">These labels illustrate the workflow; they do not change Gmail.</p>
              </div>
            </div>
          </div>
        </div>
      </motion.div>
      <p className="mt-4 text-center text-sm text-sand">Example workflow. Gmail stays the reply surface; Sendkeep remembers the promise.</p>
    </section>
  )
}
