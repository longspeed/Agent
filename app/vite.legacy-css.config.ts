import { resolve } from 'node:path'
import { defineConfig } from 'vite'
import tailwindcss from '@tailwindcss/vite'


export default defineConfig({
  plugins: [tailwindcss()],
  build: {
    outDir: '../static',
    emptyOutDir: false,
    cssCodeSplit: true,
    rollupOptions: {
      input: resolve(__dirname, 'legacy-static.css'),
      output: {
        assetFileNames: 'legacy-tailwind.css',
        entryFileNames: 'legacy-tailwind-loader.js',
      },
    },
  },
})
