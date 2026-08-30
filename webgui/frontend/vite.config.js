import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Build to webgui/frontend/dist, which the BFF serves. In dev, proxy /api to the
// BFF (:8810) so the console works against a running backend.
//
// SYSIBLE_BASE_PATH is the URL prefix the console is served under: "/" standalone,
// "/slep/" behind the SLOP gateway (which path-routes /slep/* to this app on one
// shared origin). Vite prefixes every asset URL; the SPA reads it back via
// import.meta.env.BASE_URL to prefix its API calls (see src/api.js).
export default defineConfig({
  plugins: [react()],
  base: process.env.SYSIBLE_BASE_PATH || '/',
  build: { outDir: 'dist', emptyOutDir: true, chunkSizeWarningLimit: 4000 },
  server: {
    port: 5174,
    proxy: { '/api': process.env.SLEP_CONSOLE_URL || 'http://127.0.0.1:8810' },
  },
})
