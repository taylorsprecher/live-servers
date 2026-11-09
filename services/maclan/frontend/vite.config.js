import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Public base the built app is served under. Default "/" for local/root
// hosting; build the production bundle for the gateway deployment with
//   VITE_BASE_PATH=/maclan/ npm run build
// Asset URLs in dist/index.html and import.meta.env.BASE_URL in the code
// both follow this value, so API calls resolve under the same prefix.
const base = process.env.VITE_BASE_PATH || '/'

// Dev server proxies /api to the FastAPI backend on :8000 (dev only; in
// production the FastAPI app serves this build and the API together).
export default defineConfig({
  base,
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
    },
  },
})
