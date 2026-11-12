import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Vite runs this file in Node; the project carries no Node typings for one variable.
declare const process: { env: Record<string, string | undefined> }

// The backend's port: RBA_PORT, as `make` and `rba.cmd` pass it, or 8010.
const backend = `127.0.0.1:${process.env.RBA_PORT || 8010}`

// The path the app is served under, e.g. https://dev.samsungtv/streamlens/. The page, the API
// (`/streamlens/api`) and the WebSockets (`/streamlens/ws`) all live below it, so StreamLens
// shares a host with other sites without claiming `/`. VITE_BASE_PATH=/ serves it at the root.
const trimmed = (process.env.VITE_BASE_PATH || '/streamlens/').replace(/^\/+|\/+$/g, '')
const prefix = trimmed ? `/${trimmed}` : ''
const base = `${prefix}/`

// The backend serves /api and /ws with no prefix; nginx strips it in production and these
// proxies strip it here, so the app uses the same paths in development as behind nginx.
const strip = (path: string) => path.slice(prefix.length)
const proxy = {
  [`${prefix}/api`]: { target: `http://${backend}`, changeOrigin: true, rewrite: strip },
  [`${prefix}/ws`]: { target: `ws://${backend}`, ws: true, rewrite: strip },
}

export default defineConfig({
  base,
  plugins: [react()],
  server: {
    port: 5173,
    host: true,
    proxy,
  },
  preview: {
    // `vite preview` serves the production bundle where nginx is not available — the
    // Windows run, and any local check of the built output. It proxies the same two paths,
    // so the built app talks to the backend exactly as it does behind nginx.
    port: 8080,
    host: true,
    proxy,
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
