// Settings → Metadata Sources panel fixtures. `metadataSources.json`
// was generated from the backend's own new-install defaults
// (`_DEFAULT_NEW_INSTALL_STATE`, the default priority lists, the
// derive_* helpers), so its shape is what GET /v1/metadata-sources
// returns; the traffic block follows `source_gate.traffic_summary`.
import data from "./metadataSources.json";
import { cacheStatusCooldownRoutes, cacheStatusRoutes } from "./cacheStatus";

export const metadataSourcesRoutes: Record<string, unknown> = {
  "GET /v1/metadata-sources": data.metadataSources,
  "GET /v1/metadata-sources/traffic": data.traffic,
  "GET /v1/metadata/goodreads/state": {
    state: "active",
    since: Date.parse("2026-10-09T09:00:00Z") / 1000,
    last_status: 200,
    backoff: {},
  },
  ...cacheStatusRoutes,
};

export const metadataSourcesCooldownRoutes: Record<string, unknown> = {
  ...metadataSourcesRoutes,
  ...cacheStatusCooldownRoutes,
};
