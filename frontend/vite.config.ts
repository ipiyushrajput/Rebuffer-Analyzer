import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: true,
    proxy: {
      // In development the Vite server forwards to the backend on 8010, so the app uses the
      // same relative paths it uses behind nginx in production.
      '/api': { target: 'http://127.0.0.1:8010', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8010', ws: true },
    },
  },
  preview: {
    // `vite preview` serves the production bundle where nginx is not available — the
    // Windows run, and any local check of the built output. It proxies the same two paths,
    // so the built app talks to the backend exactly as it does behind nginx.
    port: 8080,
    host: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8010', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8010', ws: true },
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
