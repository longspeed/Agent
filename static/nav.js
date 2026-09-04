// Shared header for every reply-desk page. Call renderNav() on the home page,
// or renderNav('Page name') on a page for the breadcrumb variant.
// Expects a `<div id="app-nav"></div>` placeholder in the page body.
function renderNav(breadcrumb) {
  const header = document.createElement('header');
  const outreachDesk = document.body.classList.contains('variant-b');
  header.className = outreachDesk
    ? 'outreach-global-header sticky top-0 z-20 border-b'
    : 'sticky top-0 z-20 border-b';
  header.style.cssText = 'border-color:var(--border); background:rgba(244,241,233,0.88); backdrop-filter:blur(16px);';

  const guideLink = `<a href="/getting-started" class="text-xs font-medium whitespace-nowrap ease-spring transition-colors duration-150 hover:text-white focus-visible:outline-none focus-visible:ring-2 rounded" style="color:var(--text-muted); --tw-ring-color:var(--accent);">Guide</a>`;
  const settingsLink = `<a href="/settings" class="text-xs font-medium whitespace-nowrap ease-spring transition-colors duration-150 hover:text-white focus-visible:outline-none focus-visible:ring-2 rounded" style="color:var(--text-muted); --tw-ring-color:var(--accent);">Settings</a>`;
  const logoutBtn = `<button id="nav-logout-btn" class="text-xs font-medium whitespace-nowrap ease-spring transition-colors duration-150 hover:text-white focus-visible:outline-none focus-visible:ring-2 rounded" style="color:var(--text-muted); --tw-ring-color:var(--accent);">Log out</button>`;

  header.innerHTML = outreachDesk ? `
    <div class="outreach-global-header-inner">
      <a href="/outreach?tab=inbox" class="outreach-brand-group" data-outreach-tab-link="inbox" aria-label="Sendkeep Today">
        <span class="outreach-brand-mark" aria-hidden="true"></span>
        <span class="outreach-brand-name">Sendkeep</span>
      </a>
      <nav class="outreach-top-tabs" aria-label="Outreach workflow">
        <a class="outreach-top-tab" href="/outreach?tab=inbox" data-outreach-tab-link="inbox">Today</a>
        <a class="outreach-top-tab" href="/outreach?tab=promises" data-outreach-tab-link="promises">Promises</a>
        <a class="outreach-top-tab" href="/outreach?tab=follow-ups" data-outreach-tab-link="follow-ups">Follow-ups</a>
        <a class="outreach-top-tab" href="/outreach?tab=threads" data-outreach-tab-link="threads">Threads</a>
      </nav>
      <div class="outreach-top-actions">
        <span id="nav-monitoring-state" class="outreach-sync-pill" role="status">
          <span id="nav-monitoring-copy">Gmail checking</span>
        </span>
        <a href="/settings" class="outreach-top-link">Settings</a>
      </div>
    </div>` : breadcrumb ? `
    <div class="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between gap-3">
      <div class="flex items-center gap-3 sm:gap-4 min-w-0">
        <a href="/" class="flex items-center gap-1.5 text-sm ease-spring transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 rounded" style="color:var(--text-secondary); --tw-ring-color:var(--accent);">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
          Sendkeep
        </a>
        <span style="color:var(--border-hover);">/</span>
        <span class="font-display text-base truncate" style="letter-spacing:-0.01em;">${breadcrumb}</span>
      </div>
      <div class="flex items-center gap-3 sm:gap-5 shrink-0">${guideLink}${settingsLink}${logoutBtn}</div>
    </div>` : `
    <div class="max-w-6xl mx-auto px-6 py-4 flex items-center justify-between">
      <div class="flex items-center gap-2.5">
        <div class="w-7 h-7 rounded-md flex items-center justify-center" style="background:var(--accent);">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M4 12L20 4L14 20L11 13L4 12Z" stroke="#FFFFFF" stroke-width="2" stroke-linejoin="round"/></svg>
        </div>
        <span class="font-display text-lg tracking-tight" style="letter-spacing:-0.02em;">Sendkeep</span>
      </div>
      <nav class="flex items-center gap-5 text-sm" style="color:var(--text-secondary);">
        <span style="color:var(--text);">Home</span>
        ${guideLink}
        ${settingsLink}
        ${logoutBtn}
      </nav>
    </div>`;

  const mount = document.getElementById('app-nav');
  mount.replaceWith(header);

  header.querySelector('#nav-logout-btn')?.addEventListener('click', async () => {
    await fetch('/logout', { method: 'POST' });
    window.location.href = '/login';
  });
}

// Animates a stat number counting up from 0 to `target` (ease-out cubic).
// Respects prefers-reduced-motion by jumping straight to the final value.
function animateCount(el, target, { duration = 700, prefix = '', suffix = '', locale = false } = {}) {
  if (!el) return;
  const format = (n) => prefix + (locale ? n.toLocaleString('en-US') : n) + suffix;
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    el.textContent = format(target);
    return;
  }
  const start = performance.now();
  function tick(now) {
    const t = Math.min((now - start) / duration, 1);
    const eased = 1 - Math.pow(1 - t, 3);
    el.textContent = format(Math.round(target * eased));
    if (t < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}
