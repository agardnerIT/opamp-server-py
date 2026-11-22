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
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:4320',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
