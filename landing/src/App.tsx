import { motion } from 'motion/react'
import { ChevronRight } from 'lucide-react'
import { cn } from './lib/utils'

type Logo = {
  src: string
  alt: string
  gradient: { from: string; to: string }
}

const logos: Logo[] = [
  { src: 'https://svgl.app/library/procure.svg', alt: 'Procure', gradient: { from: '#3b82f6', to: '#1d4ed8' } },
  { src: 'https://svgl.app/library/shopify.svg', alt: 'Shopify', gradient: { from: '#fde047', to: '#f59e0b' } },
  { src: 'https://svgl.app/library/blender.svg', alt: 'Blender', gradient: { from: '#60a5fa', to: '#2563eb' } },
  { src: 'https://svgl.app/library/figma.svg', alt: 'Figma', gradient: { from: '#a78bfa', to: '#7c3aed' } },
  { src: 'https://svgl.app/library/spotify.svg', alt: 'Spotify', gradient: { from: '#fb7185', to: '#ef4444' } },
  { src: 'https://svgl.app/library/lottielab.svg', alt: 'Lottielab', gradient: { from: '#facc15', to: '#22c55e' } },
  { src: 'https://svgl.app/library/google-cloud.svg', alt: 'Google Cloud', gradient: { from: '#bae6fd', to: '#38bdf8' } },
  { src: 'https://svgl.app/library/bing.svg', alt: 'Bing', gradient: { from: '#22d3ee', to: '#14b8a6' } },
]

function LogoCard({ logo }: { logo: Logo }) {
  return (
    <div
      className={cn(
        'group relative h-24 w-40 shrink-0 flex items-center justify-center rounded-full',
        'bg-white border border-slate-200/60 shadow-sm hover:border-slate-300 transition-all overflow-hidden',
      )}
    >
      <div
        aria-hidden
        className="absolute inset-0 scale-150 opacity-0 transition-all duration-500 ease-out group-hover:scale-100 group-hover:opacity-100"
        style={{ background: `linear-gradient(135deg, ${logo.gradient.from}, ${logo.gradient.to})` }}
      />
      <img
        src={logo.src}
        alt={logo.alt}
        loading="lazy"
        className="relative z-10 h-8 w-auto max-w-[96px] object-contain transition-all duration-300 group-hover:brightness-0 group-hover:invert"
      />
    </div>
  )
}

function MarqueeScroller() {
  return (
    <div
      className="marquee-container mt-10 w-full max-w-[1400px] mx-auto overflow-hidden"
      style={{
        maskImage: 'linear-gradient(to right, transparent, black 12%, black 88%, transparent)',
        WebkitMaskImage: 'linear-gradient(to right, transparent, black 12%, black 88%, transparent)',
      }}
    >
      <div className="animate-marquee flex w-max items-center gap-5 pr-5">
        {[...logos, ...logos].map((logo, i) => (
          <LogoCard key={`${logo.alt}-${i}`} logo={logo} />
        ))}
      </div>
    </div>
  )
}

function Hero() {
  return (
    <section
      className={cn(
        'relative w-full max-w-[1400px] mx-auto rounded-[48px] bg-white border border-slate-200/50',
        'shadow-[0_40px_100px_-20px_rgba(0,0,0,0.03)] overflow-hidden h-[600px] flex flex-col',
      )}
    >
      {/* Background video layer */}
      <div className="absolute inset-0 pointer-events-none z-0 overflow-hidden select-none">
        <video
          src="https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/hf_20260505_101331_74f9b798-3f00-4e86-8a01-377aa16ffeaa.mp4"
          autoPlay
          loop
          muted
          playsInline
          className="w-full h-full object-cover scale-105 transition-transform duration-1000"
        />
      </div>

      {/* Text content */}
      <div className="relative z-20 flex-1 px-8 md:px-16 pt-12 md:pt-16 flex flex-col items-start">
        <motion.div
          initial={{ opacity: 0, y: 24 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1] }}
          className="flex flex-col items-start"
        >
          <h1 className="font-display text-[42px] md:text-[56px] font-medium tracking-tight leading-[1.08] text-[#0a1b33]">
            Foundation of the
            <br />
            new digital epoch
          </h1>
          <p className="mt-5 max-w-md font-sans text-[14px] md:text-[15px] leading-relaxed text-[#64748b]">
            Designing products, powering ecosystems and laying the foundation of a decentralized web
            for enterprises, builders and communities alike.
          </p>
          <motion.button
            whileHover={{ scale: 1.04 }}
            whileTap={{ scale: 0.97 }}
            transition={{ type: 'spring', stiffness: 400, damping: 22 }}
            className="mt-8 bg-[#0a152d] text-white text-[13px] font-semibold px-6 py-3 rounded-full shadow-sm cursor-pointer focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0a152d]"
          >
            Contact Us
          </motion.button>
        </motion.div>
      </div>

      {/* Floating bottom navbar */}
      <div className="absolute bottom-10 left-1/2 -translate-x-1/2 z-30">
        <motion.nav
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, delay: 0.35, ease: [0.22, 1, 0.36, 1] }}
          className={cn(
            'flex items-center bg-white/90 backdrop-blur-2xl px-1.5 py-1.5 rounded-full',
            'shadow-[0_12px_40px_rgba(0,0,0,0.08)] border border-slate-200/40',
          )}
        >
          <div className="w-9 h-9 rounded-full bg-white border border-slate-100 shadow-sm flex items-center justify-center text-[15px] text-[#0a1b33] select-none">
            ✦
          </div>
          <button className="px-4 py-2 text-[12px] font-semibold text-slate-500 hover:text-[#0a1b33] transition-colors cursor-pointer">
            Products
          </button>
          <button className="px-4 py-2 text-[12px] font-semibold text-slate-500 hover:text-[#0a1b33] transition-colors cursor-pointer">
            Docs
          </button>
          <button
            className={cn(
              'ml-1 flex items-center gap-1 whitespace-nowrap bg-white px-5 py-2 rounded-full text-[12px] font-semibold text-[#0a1b33]',
              'border border-slate-200/60 shadow-sm hover:border-slate-300 transition-all cursor-pointer',
            )}
          >
            Get in touch
            <ChevronRight size={14} strokeWidth={2.5} />
          </button>
        </motion.nav>
      </div>
    </section>
  )
}

export default function App() {
  return (
    <main className="min-h-screen px-4 py-10 md:py-14">
      <Hero />
      <MarqueeScroller />
    </main>
  )
}
