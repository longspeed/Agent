// React port of static/nav.js's renderNav(). Same two variants: a plain
// home-page header, or a breadcrumb variant for agent/settings pages.
export function NavBar({ breadcrumb }: { breadcrumb?: string }) {
  async function logout() {
    await fetch('/logout', { method: 'POST' })
    window.location.href = '/login'
  }

  const settingsLink = (
    <a
      href="/settings"
      className="text-xs font-medium transition-colors duration-150 ease-[cubic-bezier(.22,1,.36,1)] hover:text-white focus-visible:outline-none focus-visible:ring-2 rounded"
      style={{ color: 'var(--color-sand)' }}
    >
      Settings
    </a>
  )

  const logoutBtn = (
    <button
      onClick={logout}
      className="text-xs font-medium transition-colors duration-150 ease-[cubic-bezier(.22,1,.36,1)] hover:text-white focus-visible:outline-none focus-visible:ring-2 rounded"
      style={{ color: 'var(--color-sand)' }}
    >
      Log out
    </button>
  )

  return (
    <header
      className="sticky top-0 z-20 border-b"
      style={{ borderColor: 'var(--color-line)', background: 'rgba(15,14,12,0.75)', backdropFilter: 'blur(12px)' }}
    >
      {breadcrumb ? (
        <div className="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-4">
            <a
              href="/"
              className="flex items-center gap-1.5 text-sm transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 rounded"
              style={{ color: 'var(--color-sand)' }}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M19 12H5M12 19l-7-7 7-7" />
              </svg>
              Agents
            </a>
            <span style={{ color: 'var(--color-line-hover)' }}>/</span>
            <span className="font-display text-base" style={{ letterSpacing: '-0.01em' }}>
              {breadcrumb}
            </span>
          </div>
          <div className="flex items-center gap-5">
            {settingsLink}
            {logoutBtn}
          </div>
        </div>
      ) : (
        <div className="max-w-6xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <div className="w-7 h-7 rounded-md flex items-center justify-center" style={{ background: 'var(--color-accent)' }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
                <path d="M4 12L20 4L14 20L11 13L4 12Z" stroke="#0F0E0C" strokeWidth="2" strokeLinejoin="round" />
              </svg>
            </div>
            <span className="font-display text-lg" style={{ letterSpacing: '-0.02em' }}>
              Agent Hub
            </span>
          </div>
          <nav className="flex items-center gap-5 text-sm" style={{ color: 'var(--color-sand)' }}>
            <span style={{ color: 'var(--color-cream)' }}>Home</span>
            {settingsLink}
            {logoutBtn}
          </nav>
        </div>
      )}
    </header>
  )
}
