// Shared header for every agent page. Call renderNav() on the home page,
// or renderNav('Agent Name') on an agent's own page for the breadcrumb variant.
// Expects a `<div id="app-nav"></div>` placeholder in the page body.
function renderNav(breadcrumb) {
  const header = document.createElement('header');
  header.className = 'sticky top-0 z-20 border-b';
  header.style.cssText = 'border-color:var(--border); background:rgba(15,14,12,0.75); backdrop-filter:blur(12px);';

  const logoutBtn = `<button id="nav-logout-btn" class="text-xs font-medium ease-spring transition-colors duration-150 hover:text-white focus-visible:outline-none focus-visible:ring-2 rounded" style="color:var(--text-muted); --tw-ring-color:var(--accent);">Log out</button>`;

  header.innerHTML = breadcrumb ? `
    <div class="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between">
      <div class="flex items-center gap-4">
        <a href="/" class="flex items-center gap-1.5 text-sm ease-spring transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 rounded" style="color:var(--text-secondary); --tw-ring-color:var(--accent);">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
          Agents
        </a>
        <span style="color:var(--border-hover);">/</span>
        <span class="font-display text-base" style="letter-spacing:-0.01em;">${breadcrumb}</span>
      </div>
      ${logoutBtn}
    </div>` : `
    <div class="max-w-6xl mx-auto px-6 py-4 flex items-center justify-between">
      <div class="flex items-center gap-2.5">
        <div class="w-7 h-7 rounded-md flex items-center justify-center" style="background:var(--accent);">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M4 12L20 4L14 20L11 13L4 12Z" stroke="#0F0E0C" stroke-width="2" stroke-linejoin="round"/></svg>
        </div>
        <span class="font-display text-lg tracking-tight" style="letter-spacing:-0.02em;">Agent Hub</span>
      </div>
      <nav class="flex items-center gap-5 text-sm" style="color:var(--text-secondary);">
        <span style="color:var(--text);">Home</span>
        ${logoutBtn}
      </nav>
    </div>`;

  const mount = document.getElementById('app-nav');
  mount.replaceWith(header);

  header.querySelector('#nav-logout-btn').addEventListener('click', async () => {
    await fetch('/logout', { method: 'POST' });
    window.location.href = '/login';
  });
}
