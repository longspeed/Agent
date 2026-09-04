import { useEffect, useState, type ReactNode } from 'react'
import { ChevronRight } from 'lucide-react'

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches)
  useEffect(() => {
    const mq = window.matchMedia(query)
    const onChange = (e: MediaQueryListEvent) => setMatches(e.matches)
    mq.addEventListener('change', onChange)
    setMatches(mq.matches)
    return () => mq.removeEventListener('change', onChange)
  }, [query])
  return matches
}

export const usePrefersReducedMotion = () => useMediaQuery('(prefers-reduced-motion: reduce)')

/* Orange app mark with the paper-plane glyph, same as the product's nav logo */
export function LogoMark({ className = 'w-8 h-8' }: { className?: string }) {
  return (
    <span
      className={`${className} rounded-[10px] bg-accent flex items-center justify-center shrink-0`}
      aria-hidden="true"
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
        <path
          d="M4 12L20 4L14 20L11 13L4 12Z"
          stroke="#FFFFFF"
          strokeWidth="2"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  )
}

const primaryClasses =
  'btn-primary group inline-flex items-center justify-center gap-2 rounded-full font-semibold text-sm px-5 py-3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg'

const ghostClasses =
  'group inline-flex items-center justify-center gap-2 rounded-full bg-floating border border-line text-cream font-medium text-sm px-5 py-3 transition-[transform,border-color,background-color] hover:border-line-hover hover:bg-elevated active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg'

export function PrimaryButton({
  children,
  href,
  chevron = true,
}: {
  children: ReactNode
  href: string
  chevron?: boolean
}) {
  return (
    <a href={href} className={primaryClasses}>
      {children}
      {chevron && (
        <ChevronRight
          size={16}
          className="transition-transform group-hover:translate-x-px"
          aria-hidden="true"
        />
      )}
    </a>
  )
}

export function GhostButton({ children, href }: { children: ReactNode; href: string }) {
  return (
    <a href={href} className={ghostClasses}>
      {children}
    </a>
  )
}

export function SectionEyebrow({ label, tag }: { label: string; tag?: string }) {
  return (
    <div className="flex items-center gap-3">
      <span className="w-1.5 h-1.5 rounded-full bg-accent" aria-hidden="true" />
      <span className="text-sm font-medium text-cream">{label}</span>
      {tag && (
        <span className="px-2 py-0.5 rounded-full border border-line text-xs text-sand">{tag}</span>
      )}
    </div>
  )
}

/* Root-level hero noise filter. Rendered ONCE in App. Unique id: noise-hero */
export function NoiseHeroFilter() {
  return (
    <svg width="0" height="0" className="absolute" aria-hidden="true">
      <filter id="noise-hero">
        <feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2" stitchTiles="stitch" />
        <feColorMatrix type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 0.35 0" />
        <feComposite in2="SourceGraphic" operator="in" />
        <feBlend in2="SourceGraphic" mode="multiply" />
      </filter>
    </svg>
  )
}

/* Pricing-scoped noise filter. Rendered ONCE in Pricing. Unique id: noise-pricing */
export function NoisePricingFilter() {
  return (
    <svg width="0" height="0" className="absolute" aria-hidden="true">
      <filter id="noise-pricing">
        <feTurbulence type="fractalNoise" baseFrequency="0.5" numOctaves="2" stitchTiles="stitch" />
        <feComponentTransfer>
          <feFuncA type="linear" slope="0.075" />
        </feComponentTransfer>
        <feComposite in2="SourceGraphic" operator="in" />
        <feBlend in2="SourceGraphic" mode="overlay" />
      </filter>
    </svg>
  )
}
