// Settings page fixtures. `settingsBlob.json` = the backend's DEFAULT_SETTINGS
// as GET /v1/settings returns it (secrets → `<key>_configured`), plus a
// few URLs so the page renders a configured install. (Not named
// `settings.json`: .gitignore drops every file of that name.)
import settings from "./settingsBlob.json";
import { cacheStatusCooldownRoutes, cacheStatusRoutes } from "./cacheStatus";

export const settingsRoutes: Record<string, unknown> = {
  "GET /v1/settings": settings,
  "GET /v1/credentials": {
    items: [
      { key: "mam_session_id", label: "MAM session", configured: true },
      { key: "qbit_password", label: "qBittorrent password", configured: true },
      { key: "hardcover_api_key", label: "Hardcover API key", configured: true },
      { key: "cwa_password", label: "CWA password", configured: false },
    ],
  },
  "GET /version": { short_sha: "98d6075" },
};

export { cacheStatusCooldownRoutes, cacheStatusRoutes };
