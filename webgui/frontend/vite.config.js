import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Build to webgui/frontend/dist, which the BFF serves. In dev, proxy /api to the
// BFF (:8810) so the console works against a running backend.
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: { outDir: 'dist', emptyOutDir: true, chunkSizeWarningLimit: 4000 },
  server: {
    port: 5174,
    proxy: { '/api': process.env.SLEP_CONSOLE_URL || 'http://127.0.0.1:8810' },
  },
})
