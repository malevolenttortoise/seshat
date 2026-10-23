// Discovery → MAM page fixtures: section counts, a page of books MAM
// has, both libraries as tabs; the variant has a MAM scan running.
import { idleScans, orchardBooks, standaloneBooks } from "./common";

const books = [orchardBooks[1], { ...standaloneBooks[1], mam_status: "found" as const, mam_url: "https://example.invalid/t/900005" }];

export const mamPageRoutes = {
  "GET /discovery/mam/status": {
    enabled: true,
    validation_ok: true,
    stats: { upload_candidates: 7, available_to_download: 160, missing_everywhere: 210, total_unscanned: 42 },
  },
  "GET /discovery/mam/scan/status": { running: false, status: "idle" },
  "GET /discovery/scan-status": idleScans,
  "GET /discovery/pipeline/status": { configured: true, reachable: true },
  "GET /discovery/mam/books": { books, total: 2 },
};

export const mamPageScanningRoutes = {
  ...mamPageRoutes,
  "GET /discovery/mam/scan/status": {
    running: true, status: "running", type: "scan", scanned: 18, total: 100,
    found: 6, possible: 2, not_found: 10, errors: 0, progress_pct: 18,
  },
};
