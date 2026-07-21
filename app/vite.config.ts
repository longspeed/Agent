import { resolve } from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Multi-page build: one HTML entry per converted app page, added incrementally
// as pages migrate from static/*.html to here. Every entry shares this one
// base/outDir, landing in static/app/<page>.html + static/app/assets/*.
// server.py's route handlers point a FileResponse at the built file per page
// -- there is no client-side router here, each page is still its own
// server-routed URL exactly like the static HTML it replaces.
export default defineConfig({
  base: '/static/app/',
  plugins: [react(), tailwindcss()],
  build: {
    outDir: '../static/app',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        settings: resolve(__dirname, 'settings.html'),
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
