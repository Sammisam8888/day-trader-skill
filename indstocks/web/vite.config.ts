import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Built once into dist/, committed, and served by dashboard.py: the server
// never needs Node. `npm run dev` proxies the API to a running dashboard.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 900 },
  server: { proxy: { "/api": "http://127.0.0.1:8080", "/shoonya": "http://127.0.0.1:8080" } },
});
