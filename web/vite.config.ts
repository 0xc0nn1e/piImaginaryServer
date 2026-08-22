import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    host: process.env.VITE_DEV_HOST ?? "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": process.env.VITE_DEV_API_TARGET ?? "http://127.0.0.1:8000",
      "/health": process.env.VITE_DEV_API_TARGET ?? "http://127.0.0.1:8000",
    },
  },
});
