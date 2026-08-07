import { motion } from 'motion/react'
import {
  Sparkles,
  Users,
  Send,
  MessageSquareReply,
  Settings,
  CheckCircle2,
} from 'lucide-react'
import { usePrefersReducedMotion } from './primitives'

const RAIL = [
  { icon: Users, label: 'Lead Agent' },
  { icon: Send, label: 'Outreach' },
  { icon: MessageSquareReply, label: 'Replies', badge: 3, active: true },
  { icon: Settings, label: 'Settings' },
]

const PENDING = [
  {
    name: 'Nam Long',
    meta: 'pessiskibi@…',
    snippet: '"Me too!"',
    time: '2m',
    active: true,
    unread: true,
  },
  { name: 'Ethan R.', meta: 'longspeed2828@…', snippet: '"Ok, sounds good."', time: '1h' },
  { name: 'M. Reyes', meta: 'm.reyes@northwind.co', snippet: '"What\'s pricing look like?"', time: '3h' },
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
        {/* Title bar — web app, no desktop chrome */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-line">
          <span className="text-[13px] font-medium text-cream/90">Sendkeep — Review queue</span>
          <div className="flex items-center gap-1 text-[12px]">
            {['Leads', 'Outreach', 'Replies'].map((tab) => (
              <span
                key={tab}
                className={`px-3 py-1 rounded-full ${
                  tab === 'Replies' ? 'bg-accent/15 text-accent' : 'text-sand'
                }`}
              >
                {tab}
              </span>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-12 min-h-[520px] md:h-[520px]">
          {/* Left rail — horizontal chip row below md */}
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
              Pending review
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

          {/* Review card — the differentiator */}
          <div className="md:col-span-5 p-4 flex flex-col">
            <div className="flex items-center gap-2">
              <span className="text-[13px] font-semibold">Nam Long</span>
              <span className="text-[11px] text-mute">Replied · 2m ago</span>
              <span className="ml-auto flex items-center gap-1 px-2 py-0.5 rounded-full bg-grass/15 text-grass text-[11px]">
                <CheckCircle2 size={11} aria-hidden="true" /> Draft ready
              </span>
            </div>

            <div className="mt-3 rounded-xl border border-line bg-bg/60 p-3">
              <p className="text-[11px] uppercase tracking-widest text-mute">Their reply</p>
              <p className="mt-1.5 text-[13px] text-sand leading-[1.55]">
                "Me too! Been meaning to fix our outbound for months. What does next week look
                like on your end?"
              </p>
            </div>

            <div className="mt-3 rounded-xl border border-line bg-bg/60 p-3 flex-1 flex flex-col">
              <p className="flex items-center gap-1.5 text-[11px] uppercase tracking-widest text-mute">
                <Sparkles size={12} className="text-gold" aria-hidden="true" /> Drafted by Sendkeep
              </p>
              <textarea
                aria-label="Drafted reply"
                className="mt-1.5 w-full flex-1 min-h-[96px] resize-none bg-transparent text-[13px] text-cream/90 leading-[1.55] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
                defaultValue={
                  "Glad it resonates, Nam! Happy to walk you through it — I have Tuesday or Thursday afternoon open next week. Here's my calendar if that's easier: cal.com/sendkeep/15min. Either way, looking forward to it."
                }
              />
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  className="btn-primary px-4 py-2 rounded-full text-[12px] font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-elevated"
                >
                  Send reply
                </button>
                <button
                  type="button"
                  className="px-4 py-2 rounded-full bg-floating border border-line text-[12px] text-sand hover:border-line-hover hover:text-cream active:scale-[0.98] transition-[transform,border-color,color] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-elevated"
                >
                  Dismiss
                </button>
                <button
                  type="button"
                  className="px-4 py-2 rounded-full bg-floating border border-line text-[12px] text-sand hover:border-line-hover hover:text-cream active:scale-[0.98] transition-[transform,border-color,color] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-elevated"
                >
                  Edit
                </button>
              </div>
            </div>
          </div>
        </div>
      </motion.div>
      <p className="mt-4 text-center text-sm text-sand">Nothing is sent until you press Send.</p>
    </section>
  )
}
