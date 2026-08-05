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
      const sha = execSync(`git log -1 --format=%H -- ${srcDir}`, { cwd: __dirname })
        .toString()
        .trim()
      writeFileSync(resolve(__dirname, outDir, '.build-commit'), sha + '\n')
    },
  }
}

// Built into the FastAPI app's static dir and served at /static/landing/
// (server.py serves the index at "/" for logged-out visitors).
export default defineConfig({
  base: '/static/landing/',
  plugins: [react(), tailwindcss(), buildCommitStamp('src', '../static/landing')],
  build: {
    outDir: '../static/landing',
    emptyOutDir: true,
  },
})
