import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` proxies API calls to the local FastAPI backend.
const api = process.env.ATLAS_API_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": api,
      "/health": api,
      "/ready": api,
      "/docs": api,
      "/openapi.json": api,
    },
  },
});
