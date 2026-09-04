import { useEffect, useState } from 'react'

// React port of static/nav.js's renderNav(). Same two variants: a plain
// home-page header, or a breadcrumb variant for reply-desk/settings pages.
// Public pages must not imply that an anonymous visitor is already signed in.
export function NavBar({ breadcrumb, publicPage = false }: { breadcrumb?: string; publicPage?: boolean }) {
  const [publicSession, setPublicSession] = useState<'checking' | 'signed-in' | 'signed-out'>(
    publicPage ? 'checking' : 'signed-in',
  )

  useEffect(() => {
    if (!publicPage) {
      setPublicSession('signed-in')
      return
    }

    let cancelled = false
    fetch('/api/me', { credentials: 'same-origin' })
      .then((response) => {
        if (!cancelled) setPublicSession(response.ok ? 'signed-in' : 'signed-out')
      })
      .catch(() => {
        if (!cancelled) setPublicSession('signed-out')
      })
    return () => {
      cancelled = true
    }
  }, [publicPage])

  async function logout() {
    await fetch('/logout', { method: 'POST' })
    window.location.href = '/login'
  }

  const guideLink = (
    <a
      href="/getting-started"
      className="text-xs font-medium whitespace-nowrap transition-colors duration-150 ease-[cubic-bezier(.22,1,.36,1)] hover:text-white focus-visible:outline-none focus-visible:ring-2 rounded"
      style={{ color: 'var(--color-sand)' }}
    >
      Guide
    </a>
  )

  const settingsLink = (
    <a
      href="/settings"
      className="text-xs font-medium whitespace-nowrap transition-colors duration-150 ease-[cubic-bezier(.22,1,.36,1)] hover:text-white focus-visible:outline-none focus-visible:ring-2 rounded"
      style={{ color: 'var(--color-sand)' }}
    >
      Settings
    </a>
  )

  const logoutBtn = (
    <button
      onClick={logout}
      className="text-xs font-medium whitespace-nowrap transition-colors duration-150 ease-[cubic-bezier(.22,1,.36,1)] hover:text-white focus-visible:outline-none focus-visible:ring-2 rounded"
      style={{ color: 'var(--color-sand)' }}
    >
      Log out
    </button>
  )

  const publicSignInLink = (
    <a
      href="/login"
      className="text-xs font-medium whitespace-nowrap transition-colors duration-150 ease-[cubic-bezier(.22,1,.36,1)] hover:text-white focus-visible:outline-none focus-visible:ring-2 rounded"
      style={{ color: 'var(--color-sand)' }}
    >
      Sign in
    </a>
  )

  const accountLinks = !publicPage || publicSession === 'signed-in'
    ? <>{settingsLink}{logoutBtn}</>
    : publicSession === 'signed-out'
      ? publicSignInLink
      : null

  return (
    <header
      className="sticky top-0 z-20 border-b"
      style={{ borderColor: 'var(--color-line)', background: 'rgba(244,241,233,0.88)', backdropFilter: 'blur(16px)' }}
    >
      {breadcrumb ? (
        <div className="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between gap-3">
          <div className="flex items-center gap-3 sm:gap-4 min-w-0">
            <a
              href="/"
              className="flex items-center gap-1.5 text-sm whitespace-nowrap transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 rounded"
              style={{ color: 'var(--color-sand)' }}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M19 12H5M12 19l-7-7 7-7" />
              </svg>
              Sendkeep
            </a>
            <span style={{ color: 'var(--color-line-hover)' }}>/</span>
            <span className="font-display text-base truncate" style={{ letterSpacing: '-0.01em' }}>
              {breadcrumb}
            </span>
          </div>
          <div className="flex items-center gap-3 sm:gap-5 shrink-0">
            {guideLink}
            {accountLinks}
          </div>
        </div>
      ) : (
        <div className="max-w-6xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <div className="w-7 h-7 rounded-md flex items-center justify-center" style={{ background: 'var(--color-accent)' }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
                <path d="M4 12L20 4L14 20L11 13L4 12Z" stroke="#FFFFFF" strokeWidth="2" strokeLinejoin="round" />
              </svg>
            </div>
            <span className="font-display text-lg" style={{ letterSpacing: '-0.02em' }}>
              Sendkeep
            </span>
          </div>
          <nav className="flex items-center gap-5 text-sm" style={{ color: 'var(--color-sand)' }}>
            <span style={{ color: 'var(--color-cream)' }}>Home</span>
            {guideLink}
            {accountLinks}
          </nav>
        </div>
      )}
    </header>
  )
}
