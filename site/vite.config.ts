import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Built into the FastAPI app's static dir and served at /static/landing/
// (server.py serves the index at "/" for logged-out visitors).
export default defineConfig({
  base: '/static/landing/',
  plugins: [react(), tailwindcss()],
  build: {
    outDir: '../static/landing',
    emptyOutDir: true,
  },
})
