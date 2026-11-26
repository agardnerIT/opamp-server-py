import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Dev server proxies /api to the FastAPI server so the browser talks to one
// origin, mirroring the nginx-proxy layout planned for production.
// NOTE: the shorthand string form preserves the full path, which would forward
// `/api/auth/session` to a server that only serves `/auth/session` — the strip
// below is required, and the production nginx config (stage 10) needs the same.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // 8501 is the product port (Streamlit's old one); stage 10 handed it over,
    // so this is the console's port, not a fallback target. strictPort stays
    // false so `npm run dev` still starts (on 8502+) while the compose stack's
    // built `ui` container owns 8501 — the alternative is a dev server that
    // refuses to run until something else is stopped.
    port: 8501,
    strictPort: false,
    proxy: {
      '/api': {
        // OPAMP_API_TARGET lets the live smoke (playwright.live.config.ts) run
        // its own throwaway server on another port without touching the dev
        // stack's server on 4320.
        target: process.env.OPAMP_API_TARGET ?? 'http://127.0.0.1:4320',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
