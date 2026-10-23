// /v1/metadata-cache/{amazon,goodreads}/status fixtures — the shape the
// navbar icon, the Dashboard rails and the Settings cards all read.
const base = (source: "amazon" | "goodreads") => ({
  source,
  enabled: true,
  mode: "continuous" as const,
  schedule: { active_hours: "08:00-23:00", timezone: "America/New_York" },
  inside_schedule_window: true,
  seconds_until_window_open: 0,
  cooldown: { blocked: false, remaining_s: 0, reason: null },
  worker: {
    last_block_at: 0,
    block_cooldown_s: 0,
    consecutive_blocks: 0,
    last_heartbeat_at: Date.parse("2026-10-09T15:59:30Z") / 1000,
    last_scan_completed_at: Date.parse("2026-10-09T15:55:00Z") / 1000,
    today_scan_count: source === "amazon" ? 42 : 17,
    today_block_count: source === "amazon" ? 0 : 3,
    seconds_since_heartbeat: 30,
    seconds_since_scan_completed: 300,
  },
  queue: {
    total: 640,
    pending: 610,
    in_progress: 1,
    failed_permanent: 0,
    other: 29,
    due_now: 12,
    scheduled_later: 598,
    refreshed_today: source === "amazon" ? 40 : 15,
    daily_cap: 101,
  },
  cache: {
    state_rows: 1280,
    books_rows: 9100,
    ok_authors: 1190,
    error_authors: 4,
    unique_ok_authors: 595,
    unique_total_authors: 640,
    list_pages_rows: source === "goodreads" ? 2210 : undefined,
    today_budget_exhaust_count: 0,
  },
  candidates: null as unknown,
});

export const amazonStatus = base("amazon");

export const goodreadsStatus = {
  ...base("goodreads"),
  candidates: {
    first_fill: { candidates: 300, decided: 120, authors: 64, authors_left: 31 },
    weekly: { candidates: 22, decided: 22 },
    pending: 180,
    awaiting_page: 9,
    accepted_waiting_merge: 2,
    created: 41,
    rejected: 77,
    rejected_by_reason: { no_confirm: 50, owned: 27 },
    tracked_authors: 64,
    last: { title: "Rootbound", state: "accepted", author_id: "g-1001" },
    book_page_next_at: Date.parse("2026-10-09T16:02:00Z") / 1000,
    phase2: { enabled: false, fetched: 0, left: 0, gone: 0, failed: 0 },
  },
};

export const goodreadsCooldownStatus = {
  ...goodreadsStatus,
  cooldown: { blocked: true, remaining_s: 1800, reason: "block page" },
};

export const cacheStatusRoutes = {
  "GET /v1/metadata-cache/amazon/status": amazonStatus,
  "GET /v1/metadata-cache/goodreads/status": goodreadsStatus,
};

export const cacheStatusCooldownRoutes = {
  "GET /v1/metadata-cache/amazon/status": amazonStatus,
  "GET /v1/metadata-cache/goodreads/status": goodreadsCooldownStatus,
};
