import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Forward API calls (HTTP + WebSocket) to the FastAPI backend during development.
    proxy: {
      '/api': { target: 'http://localhost:8000', ws: true },
    },
  },
})
