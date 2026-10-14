import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiTarget = process.env.PANAH_DEV_API ?? "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    // The panel serves these files itself; keep the bundle lean.
    chunkSizeWarningLimit: 700,
  },
  server: {
    port: 5173,
    host: "127.0.0.1",
    proxy: {
      "/api": { target: apiTarget, changeOrigin: false },
      "/sub": { target: apiTarget, changeOrigin: false },
      "/health": { target: apiTarget, changeOrigin: false },
    },
  },
});
