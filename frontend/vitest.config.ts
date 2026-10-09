import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Vitest config — deliberately separate from vite.config.ts so the
// PWA/build plugins (service worker, manifest, manualChunks) don't load
// during unit tests. jsdom gives hooks and pages a DOM + timers;
// globals:true lets Testing Library register its automatic cleanup.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["src/test/setup.ts"],
  },
});
