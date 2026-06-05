import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Vitest config — deliberately separate from vite.config.ts so the
// PWA/build plugins (service worker, manifest, manualChunks) don't load
// during unit tests. jsdom gives hooks a DOM + timers; globals:true
// exposes describe/it/expect/vi without per-file imports.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
