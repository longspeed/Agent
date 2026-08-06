import { resolve } from 'node:path'
import { execSync } from 'node:child_process'
import { writeFileSync } from 'node:fs'
import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Stamps the built output with the commit that last touched this project's
// src/, so a freshness check can tell "built from current source" apart from
// "built days ago, source has moved on since" -- without trusting file
// mtimes/commit *dates*, which change under rebase/amend/cherry-pick without
// the content changing. Read back by check_build_freshness.py.
function buildCommitStamp(srcDir: string, outDir: string): Plugin {
  return {
    name: 'build-commit-stamp',
    closeBundle() {
      // Advisory, same as check_build_freshness.py that reads this file: a
      // git failure here (unusual environment, git missing, detached HEAD)
      // must not abort the production build -- it degrades to the same
      // "no build found" state the freshness check already handles for a
      // missing stamp, not a build-breaking error.
      try {
        const sha = execSync(`git log -1 --format=%H -- ${srcDir}`, { cwd: __dirname })
          .toString()
          .trim()
        writeFileSync(resolve(__dirname, outDir, '.build-commit'), sha + '\n')
      } catch (err) {
        console.warn(`[build-commit-stamp] could not stamp ${outDir} -- ${(err as Error).message}`)
      }
    },
  }
}

// Multi-page build: one HTML entry per converted app page, added incrementally
// as pages migrate from static/*.html to here. Every entry shares this one
// base/outDir, landing in static/app/<page>.html + static/app/assets/*.
// server.py's route handlers point a FileResponse at the built file per page
// -- there is no client-side router here, each page is still its own
// server-routed URL exactly like the static HTML it replaces.
export default defineConfig({
  base: '/static/app/',
  plugins: [react(), tailwindcss(), buildCommitStamp('src', '../static/app')],
  build: {
    outDir: '../static/app',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        settings: resolve(__dirname, 'settings.html'),
        'getting-started': resolve(__dirname, 'getting-started.html'),
      },
    },
  },
  server: {
    // Dev-only proxy so `npm run dev` (Vite on :5173) can hit the real
    // FastAPI backend on :8000 without needing CORS -- server.py has no
    // CORSMiddleware today, and this avoids requiring one.
    proxy: {
      '/api': 'http://localhost:8000',
      '/login': 'http://localhost:8000',
      '/signup': 'http://localhost:8000',
      '/logout': 'http://localhost:8000',
      '/auth': 'http://localhost:8000',
    },
  },
})
