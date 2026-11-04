import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Vite runs this file in Node; the project carries no Node typings for one variable.
declare const process: { env: Record<string, string | undefined> }

// The backend's port: RBA_PORT, as `make` and `rba.cmd` pass it, or 8010.
const backend = `127.0.0.1:${process.env.RBA_PORT || 8010}`

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: true,
    proxy: {
      // In development the Vite server forwards to the backend (8010 by default), so the app
      // uses the same relative paths it uses behind nginx in production.
      '/api': { target: `http://${backend}`, changeOrigin: true },
      '/ws': { target: `ws://${backend}`, ws: true },
    },
  },
  preview: {
    // `vite preview` serves the production bundle where nginx is not available — the
    // Windows run, and any local check of the built output. It proxies the same two paths,
    // so the built app talks to the backend exactly as it does behind nginx.
    port: 8080,
    host: true,
    proxy: {
      '/api': { target: `http://${backend}`, changeOrigin: true },
      '/ws': { target: `ws://${backend}`, ws: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      output: {
        manualChunks: {
          echarts: ['echarts', 'echarts-for-react'],
          hls: ['hls.js'],
          react: ['react', 'react-dom'],
        },
      },
    },
  },
})
