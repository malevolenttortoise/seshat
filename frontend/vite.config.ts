import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

// Vite config for the Seshat frontend.
//
// - Dev server proxies /api → the FastAPI backend on :8789 so cookies
//   work without CORS shenanigans.
// - Build emits to ./dist; FastAPI mounts that at runtime.
// - A separate chunk for the React vendor bundle keeps page chunks small
//   and lets the browser cache react/react-dom across deploys (Rolldown
//   `codeSplitting` group since Vite 8; Rollup's object-form
//   `manualChunks` before).
// - VitePWA generates the web-app manifest + service worker. The
//   service worker only activates under HTTPS (or localhost) —
//   browsers refuse to register SWs on plain-HTTP LAN origins, so on
//   http://<lan-ip> Seshat degrades gracefully to a regular SPA. When
//   Mark fronts Seshat with a reverse proxy + cert later, the PWA
//   layer lights up automatically on the next page load.
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      // Silent auto-update: a new build auto-installs and activates on
      // the next full page load. `registerType: 'autoUpdate'`
      // combined with `skipWaiting` + `clientsClaim` below skips the
      // "waiting" limbo that the default `prompt` flow uses. No
      // user-facing "new version available" dialog — Mark ships by
      // pushing to main, the next visit just picks up the new assets.
      registerType: "autoUpdate",
      includeAssets: [
        "favicon.png",
        "icon.svg",
        "apple-touch-icon.png",
        "icon-16.png",
        "icon-32.png",
        "icon-180.png",
        "icon-512.png",
      ],
      manifest: {
        name: "Seshat",
        short_name: "Seshat",
        description:
          "Book discovery + MAM automation — unified library management",
        // Match the Egyptian-goddess dark theme. These drive the
        // standalone-window chrome color on Android + desktop.
        theme_color: "#e4b868",
        background_color: "#1a1c30",
        display: "standalone",
        orientation: "any",
        scope: "/",
        start_url: "/",
        icons: [
          {
            src: "/icon-180.png",
            sizes: "180x180",
            type: "image/png",
          },
          {
            src: "/icon-512.png",
            sizes: "512x512",
            type: "image/png",
          },
          // Same 512 reused as a maskable variant — Android adaptive
          // icons crop to a safe zone, and a fully-populated 512
          // reads correctly enough without a dedicated maskable. Can
          // swap in a real maskable source if icons look cropped.
          {
            src: "/icon-512.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "maskable",
          },
        ],
      },
      workbox: {
        // Activate the new SW immediately on install instead of the
        // default "wait until all tabs close" behavior.
        skipWaiting: true,
        clientsClaim: true,
        // Precache the Vite build output (app shell).
        globPatterns: ["**/*.{js,css,html,ico,png,svg,woff2}"],
        // SPA fallback: navigations resolve to index.html so
        // deep-linked routes work offline after the shell is cached.
        navigateFallback: "/index.html",
        navigateFallbackDenylist: [/^\/api\//],
        // SSE connections (EventSource) MUST NEVER be intercepted by
        // the service worker — workbox would buffer them into a
        // cache entry and the live-update stream would die. The
        // broadcast channel under /api/v1/events handles torrent
        // progress, client status, mam stats, and toasts.
        // Same for any other streaming endpoint we add later.
        runtimeCaching: [
          // 1. Covers: cache-first, 7-day TTL. Book covers change
          //    rarely (re-enrich events) and account for the bulk of
          //    image bytes. Cache-first means instant display on
          //    repeat views; the TTL + LRU cap bounds disk usage.
          //    No URL-versioning on the backend today, so if a cover
          //    IS replaced the browser won't pick it up until the
          //    cache entry ages out. Acceptable tradeoff.
          {
            urlPattern: /\/api\/discovery\/covers\//,
            handler: "CacheFirst",
            options: {
              cacheName: "seshat-covers",
              expiration: {
                maxEntries: 500,
                maxAgeSeconds: 60 * 60 * 24 * 7,
              },
              cacheableResponse: { statuses: [0, 200] },
            },
          },

          // 2. MAM status + user-status: stale-while-revalidate.
          //    These endpoints drive the dashboard pills and economy
          //    widgets; the user wants the number NOW plus a refresh.
          //    SSE pushes the truth in real time regardless, so a
          //    slightly stale HTTP response is harmless.
          {
            urlPattern: /\/api\/v1\/mam\/status/,
            handler: "StaleWhileRevalidate",
            options: {
              cacheName: "seshat-mam-status",
              expiration: { maxEntries: 16, maxAgeSeconds: 60 * 60 },
              cacheableResponse: { statuses: [200] },
            },
          },

          // 3. Discovery list endpoints (books, authors, series,
          //    config). Stale-while-revalidate so offline browsing
          //    shows the last snapshot while the network fetch
          //    refreshes in the background when online.
          {
            urlPattern: /\/api\/discovery\/(books|authors|series|config|libraries|works)\b/,
            handler: "StaleWhileRevalidate",
            options: {
              cacheName: "seshat-lists",
              expiration: { maxEntries: 100, maxAgeSeconds: 60 * 60 * 24 },
              cacheableResponse: { statuses: [200] },
            },
          },

          // 4. SSE stream — explicit NetworkOnly so workbox doesn't
          //    buffer the EventSource response. If it ever ended up
          //    in a caching handler the live-update layer would die
          //    silently. Belt-and-braces against future rule reordering.
          {
            urlPattern: /\/api\/v1\/events/,
            handler: "NetworkOnly",
          },

          // 5. Default for everything else under /api/: network-first
          //    with a 5-second timeout, falling back to cache when
          //    offline. Covers the write endpoints too — those just
          //    fail gracefully offline instead of matching some stale
          //    cache entry and pretending to succeed.
          {
            urlPattern: /\/api\//,
            handler: "NetworkFirst",
            options: {
              cacheName: "seshat-api",
              networkTimeoutSeconds: 5,
              expiration: { maxEntries: 64, maxAgeSeconds: 60 * 60 },
              cacheableResponse: { statuses: [200] },
            },
          },
        ],
      },
      // Dev build: SW runs in dev too so we can test registration
      // and caching locally without doing a full production build
      // each iteration. Note: service workers only register in
      // secure contexts (localhost is always secure; LAN IPs are
      // not) — this is a real browser constraint, not a config.
      devOptions: {
        enabled: true,
        type: "module",
      },
    }),
  ],
  server: {
    proxy: {
      "/api": "http://localhost:8789",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Minifier: Vite 8's default (oxc). Until Vite 6 this config named
    // esbuild, which was that version's default.
    target: "es2020",
    cssCodeSplit: true,
    reportCompressedSize: false,
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            {
              name: "react-vendor",
              test: /[\\/]node_modules[\\/](react|react-dom|scheduler)[\\/]/,
            },
          ],
        },
      },
    },
  },
});
