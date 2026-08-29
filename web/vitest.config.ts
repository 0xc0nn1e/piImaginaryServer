import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    // The viewer's zone is Hong Kong while days are grouped in Japan time, so
    // pinning it keeps a Japan-time assertion meaningful wherever the suite
    // runs instead of passing by accident on a machine already at UTC+9.
    env: { TZ: "Asia/Hong_Kong" },
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test/setup.ts",
    restoreMocks: true,
  },
});
