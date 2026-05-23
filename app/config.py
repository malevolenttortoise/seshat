"""
Configuration loading and persistence.

Two layers of config:

  1. **Environment variables** (read once at import time): things the
     deployment owner sets via `docker run -e ...`. These seed
     `settings.json` on first run only — after that, settings.json is
     the source of truth and env vars are ignored.
  2. **Saved settings** (`settings.json` under DATA_DIR): runtime-mutable
     state edited via the Settings UI. `load_settings()` always merges
     the on-disk file over `DEFAULT_SETTINGS`, so every key listed in
     DEFAULT_SETTINGS is guaranteed to be present in the returned dict.

INVARIANT for adding a new setting:
  1. Add it to DEFAULT_SETTINGS first with its canonical default value.
  2. Any inline `.get("key", FALLBACK)` calls scattered across the code
     MUST use the same FALLBACK as the entry here. Mismatched defaults
     silently diverge for users whose settings.json predates the key.
"""
import json
import logging
import os
import re as _re
from pathlib import Path

from app.runtime import IS_DOCKER, get_data_dir

_log = logging.getLogger("seshat.config")


# ─── Environment variables (first-run seeds) ─────────────────

ENV_WEBUI_HOST = os.getenv("WEBUI_HOST", "0.0.0.0")
ENV_WEBUI_PORT = int(os.getenv("WEBUI_PORT", "8789"))

# Verbose logging toggle (DEBUG level vs INFO).
ENV_VERBOSE_LOGGING = os.getenv("VERBOSE_LOGGING", "").lower() in ("true", "1", "yes")

# MAM debug-match endpoint toggle. Off by default — gates a developer
# inspection endpoint that returns the full per-pass / per-result
# scoring breakdown for any (title, author) query against MAM. Useful
# for diagnosing wrong-Possible matches and tuning thresholds.
ENV_MAM_DEBUG_MATCH = os.getenv("MAM_DEBUG_MATCH", "").lower() in ("true", "1", "yes")

# MAM session cookie — first-run seed only. After settings.json exists,
# the UI is the only way to update it.
ENV_MAM_SESSION_ID = os.getenv("MAM_SESSION_ID", "")

# MAM IRC bot credentials — first-run seeds. The lifespan reads the
# saved settings to decide whether to start the IRC listener at all,
# so all three of these must be populated for the listener to come up.
ENV_MAM_IRC_NICK = os.getenv("MAM_IRC_NICK", "")
ENV_MAM_IRC_ACCOUNT = os.getenv("MAM_IRC_ACCOUNT", "")
ENV_MAM_IRC_PASSWORD = os.getenv("MAM_IRC_PASSWORD", "")

# qBittorrent connection — first-run seeds.
ENV_QBIT_URL = os.getenv("QBIT_URL", "")
ENV_QBIT_USERNAME = os.getenv("QBIT_USERNAME", "")
ENV_QBIT_PASSWORD = os.getenv("QBIT_PASSWORD", "")

# qBittorrent download category that Seshat watches for completed
# torrents. Default matches the OP's existing qBit setup convention
# of `[mam-reseed]` (the bracket characters are part of the category
# name, not a glob — qBit accepts arbitrary strings here).
ENV_QBIT_WATCH_CATEGORY = os.getenv("QBIT_WATCH_CATEGORY", "[mam-reseed]")
ENV_QBIT_TAG = os.getenv("QBIT_TAG", "seshat-seed")

# Calibre library path (mounted into the container). The library directory
# that contains metadata.db. Empty by default — user configures via Settings.
ENV_CALIBRE_LIBRARY_PATH = os.getenv(
    "CALIBRE_LIBRARY_PATH",
    "/calibre" if IS_DOCKER else "",
)

# Staging directory: where downloaded books are copied for metadata review
# before being added to Calibre.
ENV_STAGING_PATH = os.getenv(
    "STAGING_PATH",
    "/staging" if IS_DOCKER else "",
)

# ntfy endpoint for notifications. Empty disables notifications.
ENV_NTFY_URL = os.getenv("NTFY_URL", "")

# Auth secret — for HTTP-only session cookies. Env var takes priority,
# then a file under DATA_DIR, then in-memory fallback.
ENV_AUTH_SECRET = os.getenv("SESHAT_AUTH_SECRET", "")

# Dry-run mode: connect to real IRC and parse real announces, but never fetch
# .torrent files or talk to qBittorrent. Used for testing without burning
# snatch budget.
ENV_DRY_RUN = os.getenv("SESHAT_DRY_RUN", "").lower() in ("true", "1", "yes")

# ── Discovery-domain env vars ───────────────────────────────
# Calibre library discovery paths.
CALIBRE_PATH = os.getenv("CALIBRE_PATH", "")
CALIBRE_EXTRA_PATHS = os.getenv("CALIBRE_EXTRA_PATHS", "")
CALIBRE_DB_PATH = os.getenv("CALIBRE_DB_PATH", "/calibre/metadata.db" if IS_DOCKER else "")
CALIBRE_LIBRARY_PATH = os.getenv("CALIBRE_LIBRARY_PATH", "/calibre" if IS_DOCKER else "")
SYNC_INTERVAL_MINUTES = int(os.getenv("SYNC_INTERVAL_MINUTES", "60"))
LOOKUP_INTERVAL_MINUTES = int(os.getenv("LOOKUP_INTERVAL_MINUTES", "4320"))
MAM_SCAN_INTERVAL_MINUTES = int(os.getenv("MAM_SCAN_INTERVAL_MINUTES", "360"))
ENV_HARDCOVER_API_KEY = os.getenv("HARDCOVER_API_KEY", "")
ENV_CALIBRE_WEB_URL = os.getenv("CALIBRE_WEB_URL", "")
ENV_CALIBRE_URL = os.getenv("CALIBRE_URL", "")
# Audiobookshelf API base URL (e.g. http://host:13378) — first-run seed
# for `abs_url`. The API key itself is never env-sourced; it belongs in
# the encrypted secrets store. Empty string leaves ABS disabled until
# the user configures it via Settings.
ENV_ABS_URL = os.getenv("ABS_URL", "")

LANGUAGE_OPTIONS = [
    "Afrikaans", "Albanian", "Arabic", "Armenian", "Basque", "Bengali",
    "Bulgarian", "Catalan", "Chinese", "Croatian", "Czech", "Danish",
    "Dutch", "English", "Estonian", "Filipino", "Finnish", "French",
    "Galician", "Georgian", "German", "Greek", "Gujarati", "Hebrew",
    "Hindi", "Hungarian", "Icelandic", "Indonesian", "Irish", "Italian",
    "Japanese", "Kannada", "Korean", "Latin", "Latvian", "Lithuanian",
    "Macedonian", "Malay", "Malayalam", "Maltese", "Marathi", "Mongolian",
    "Norwegian", "Persian", "Polish", "Portuguese", "Punjabi", "Romanian",
    "Russian", "Serbian", "Slovak", "Slovenian", "Spanish", "Swahili",
    "Swedish", "Tamil", "Telugu", "Thai", "Turkish", "Ukrainian", "Urdu",
    "Vietnamese", "Welsh",
]


# ─── Data directory ──────────────────────────────────────────

_data_dir_env = os.getenv("DATA_DIR", "")
DATA_DIR = Path(_data_dir_env) if _data_dir_env else get_data_dir()
APP_DB_PATH = DATA_DIR / "seshat.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
AUTH_SECRET_PATH = DATA_DIR / "auth_secret"

DATA_DIR.mkdir(parents=True, exist_ok=True)


# ─── DEFAULT_SETTINGS — canonical source of truth ────────────

DEFAULT_SETTINGS = {
    # ── MAM session ─────────────────────────────────────────
    "mam_session_id": "",
    "mam_last_validated_at": None,
    "mam_validation_ok": False,
    # IRC bot identity (NickServ-registered nick on irc.myanonamouse.net)
    "mam_irc_nick": "",
    "mam_irc_account": "",
    "mam_irc_password": "",
    # Pause the IRC listener entirely (used during cookie expiry, manual stop)
    "mam_irc_enabled": True,

    # ── Filtering ───────────────────────────────────────────
    # Categories Seshat is interested in. Normalized form (lowercase,
    # punctuation collapsed to single spaces). The user edits this in the
    # Settings UI; the filter consults it on every announce.
    "allowed_categories": [
        "ebooks action adventure",
        "ebooks science fiction",
        "ebooks fantasy",
        "ebooks urban fantasy",
        "ebooks general fiction",
        "ebooks mixed collections",
        "ebooks young adult",
    ],
    # Categories to exclude even when the parent format is allowed.
    # Normalized form. E.g. ["ebooks romance"] to block romance but
    # keep all other ebook subcategories.
    "excluded_categories": [],
    # Format-level gates. The "format" is the MAM category prefix
    # before " - " (e.g. "ebooks", "audiobooks", "comics graphic novels").
    # Empty allowed_formats = accept all formats that pass category gate.
    # The UI labels this gate "Media Type" as of v2.9.0 — the setting
    # key is kept as `allowed_formats` for settings.json backwards-compat.
    "allowed_formats": [],
    "excluded_formats": [],

    # v2.9.0 — per-media-type Format Priority. List order is the
    # priority order (top = highest). `enabled: true` means an
    # announce in that filetype is always grabbed regardless of
    # in-flight or owned siblings. `enabled: false` means the
    # announce is held / skipped per the format-priority dedup rules
    # (see app/orchestrator/format_dedup.py). The lists drive the
    # rules; the format set per media type is also the universe of
    # formats the dedup gate recognizes.
    "format_priority": {
        "ebook": [
            {"fmt": "epub", "enabled": True},
            {"fmt": "azw3", "enabled": False},
            {"fmt": "mobi", "enabled": False},
            {"fmt": "pdf",  "enabled": False},
        ],
        "audiobook": [
            {"fmt": "m4b", "enabled": True},
            {"fmt": "mp3", "enabled": False},
        ],
    },
    # How long to hold a disabled-format announce before grabbing it,
    # in case a higher-priority sibling arrives during the window.
    # 600 = 10 minutes. Tunable for slow uploaders who submit the
    # second-format torrent minutes after the first.
    "format_dedup_hold_seconds": 600,
    # Audiobook acceptance is derived as of v2.9.0 from the Media
    # Type filter (`allowed_formats`): when empty (= "accept all") or
    # when it contains "audiobooks", audiobook announces flow through
    # the filter and `allowed_audiobook_categories` gets merged into
    # the runtime category set. The legacy `accept_audiobook_announces`
    # boolean is migrated to "audiobooks" in `allowed_formats` by
    # `_apply_legacy_settings_migrations` on first v2.9.0 load.
    "allowed_audiobook_categories": [
        "audiobooks action adventure",
        "audiobooks science fiction",
        "audiobooks fantasy",
        "audiobooks urban fantasy",
        "audiobooks general fiction",
        "audiobooks young adult",
        "audiobooks mixed collections",
    ],
    # Language gate. Normalized lowercase. Empty = accept all languages.
    "allowed_languages": ["english"],
    # Uploaders whose torrents should NEVER be grabbed. Case-insensitive
    # username match. Prevents downloading your own uploads — MAM would
    # count that as a re-snatch. Default seeded with the operator's
    # own username to avoid accidents.
    "excluded_uploaders": [],

    # ── Grab policy (VIP / freeleech / wedge / ratio) ──────
    # These settings control the economic decision layer that runs
    # AFTER the filter gate says "allow" but BEFORE the actual grab.
    # The policy engine checks whether the torrent is "free" (VIP,
    # global FL, or wedge-applicable) and whether the user's ratio
    # can afford the download if it isn't.

    # If true, only grab VIP torrents (download doesn't count).
    "policy_vip_only": False,
    # If true, only grab torrents that are free (VIP, global FL,
    # personal FL, or wedge-applied). Non-free torrents are skipped.
    "policy_free_only": False,
    # VIP torrents bypass all other policy checks (ratio, wedge logic).
    "policy_vip_always_grab": True,
    # Spend a freeleech wedge to make a non-free torrent free.
    "policy_use_wedge": False,
    # Don't spend wedges if the user's wedge count would drop below
    # this threshold. 0 = spend all wedges freely.
    "policy_min_wedges_reserved": 0,
    # Skip non-free torrents if the user's ratio is below this value.
    # 0 = disable ratio checking (grab regardless of ratio).
    "policy_ratio_floor": 0.0,

    # ── Snatch budget (rate limit) ──────────────────────────
    # MAM caps active snatches. New users get 30, OP currently has 200.
    # A "snatch" is in-budget from grab time until the torrent has accumulated
    # 72 hours of seedtime in qBittorrent (or until it's removed from qBit).
    "snatch_budget_cap": 200,
    "snatch_seed_hours_required": 72,
    # Mode when budget is full: "queue" (fetch and hold locally, submit when
    # budget frees) or "drop" (skip the announce entirely, log to review queue).
    "snatch_full_mode": "queue",
    "snatch_queue_max": 200,

    # ── Download client ──────────────────────────────────────
    # Supported: qbittorrent, transmission, deluge, rtorrent
    "download_client_type": "qbittorrent",
    "qbit_url": "",
    "qbit_username": "",
    "qbit_password": "",
    "qbit_watch_category": "[mam-reseed]",
    # Unix timestamp — grandfather line for qBit orphan adoption.
    # The download watcher only adopts torrents with
    # `added_on >= qbit_orphan_adoption_since`. Older torrents in the
    # watch category (pre-existing from a prior install) are treated
    # as pre-existing and skipped, so a long-running qBit
    # instance with thousands of seeding books doesn't get
    # mass-adopted on first tick after deploying the adopter.
    # Initialized to `time.time()` once at lifespan startup when the
    # stored value is still 0 (the DEFAULT_SETTINGS sentinel). Never
    # auto-updated after that.
    "qbit_orphan_adoption_since": 0,
    # Comma-separated tag list applied to every torrent Seshat
    # submits to qBit. Lines up with the user's existing
    # manual-seed / autobrr-seed / seshat-seed convention so
    # which client added what is visible at a glance in the qBit
    # WebUI. Empty string disables tagging.
    "qbit_tag": "seshat-seed",
    # Base download directory for qBit. Seshat creates subfolders
    # under this root based on `download_folder_structure` below
    # (monthly/yearly/author/flat). Path is AS SEEN BY QBIT inside
    # its container; if Docker, use qBit's mount path, not Seshat's.
    # E.g. "/data/[mam-complete]".
    "qbit_download_path": "",
    # Path translation between qBit's container and Seshat's container.
    # qBit reports save_path using ITS mount paths (e.g. "/data/...").
    # Seshat needs to translate that to ITS mount paths to find files.
    # qbit_path_prefix: what qBit uses (e.g. "/data")
    # local_path_prefix: what Seshat sees (e.g. "/downloads")
    # The download watcher replaces qbit_path_prefix with local_path_prefix
    # when reading files, and does the reverse when passing save_path to qBit.
    "qbit_path_prefix": "/data",
    "local_path_prefix": "/downloads",
    # How often to poll qBit for completed torrents and seedtime updates.
    "qbit_poll_interval_seconds": 60,
    # Minimum gap (seconds) between successive POST /api/v2/torrents/add
    # calls Seshat makes to qBit. qBit fires a tracker announce per add;
    # bursts of 8+ adds in a few seconds trip MAM's per-IP tracker
    # throttle and turn every subsequent announce into a sticky timeout
    # for ~15 minutes (UAT 2026-05-22). The stagger spaces adds with
    # ±qbit_add_stagger_jitter_s of randomness so adjacent batches don't
    # ALL land at multiples of the same interval. Set to 0 to disable.
    # Read live from settings on every dispatch — no restart required.
    "qbit_add_stagger_s": 2.0,
    "qbit_add_stagger_jitter_s": 0.5,

    # ── Sinks (where completed books go) ────────────────────
    # Default sink: calibre. Per-category overrides via "category_routing".
    "default_sink": "calibre",
    "category_routing": {},  # {"audiobooks fantasy": "folder", ...}
    "folder_sink_path": "",  # for folder sink
    "audiobookshelf_library_path": "",  # for audiobookshelf sink
    # CWA (Calibre-Web-Automated) ingest directory. CWA watches this
    # folder and auto-imports any book files dropped here. Safest
    # Calibre integration — no direct metadata.db writes.
    "cwa_ingest_path": "",
    # Minimum gap (seconds) between successive book drops into the CWA
    # ingest folder. CWA's post-import duplicate scan runs inside the
    # single-threaded cps web process on a 5s debounce by default; when
    # two ingests overlap, the second's web-API callbacks time out and
    # cps loses its HTTP listener until the container is restarted
    # (reproduced 2026-05-11 with a 2-book approve-all). A 10s default
    # gap covers the 5s debounce + scan duration with margin. Per-
    # ingest-path lock — multi-CWA setups don't contend. Set to 0 to
    # disable (safe if you've turned off CWA's "Enable automatic
    # duplicate scans"); raise above 10 if you've increased CWA's
    # import-debounce setting.
    "cwa_min_inter_book_seconds": 10,
    # CWA push-back (v2.3.5) — when slim users want metadata edits to
    # land in their CWA-managed Calibre library. Seshat drives CWA's
    # `/admin/book/<id>` form POST handler (the one its own SPA uses).
    # Auth = login form → session cookie → CSRF token scraped from
    # rendered HTML. Password lives in the encrypted secrets store
    # under `cwa_password`, not here.
    "cwa_base_url": "",
    "cwa_username": "",

    # ── Audiobookshelf integration ──────────────────────────
    # Base URL of the ABS instance Seshat talks to (e.g.
    # "http://audiobookshelf:13378"). API key lives in the encrypted
    # secrets store under `abs_api_key`, not here.
    "abs_url": "",
    # Web UI URL for the dashboard quick-launch button. Usually
    # matches abs_url from the user's browser perspective; kept
    # separate so container-to-container and browser URLs can
    # differ (host.docker.internal vs public hostname).
    "abs_web_url": "",
    # Which ABS library the sink delivers into. Only needed when
    # default_sink="audiobookshelf"; the post-drop scan-trigger POST
    # targets this library id. Empty means "don't trigger a rescan"
    # (ABS's watcher still picks up the drop within ~60s).
    "abs_sink_library_id": "",
    # Tracking scope for audiobook-aware features (author watch,
    # missing-book detection, MAM scanning): "ebook", "audiobook",
    # or "both". "both" = owning either format satisfies ownership.
    # Per-author overrides live on the authors row (Phase 2+).
    "audiobook_tracking_mode": "both",
    # Audiobook format priority. When a torrent contains multiple
    # audio formats (rare — most audiobook releases stick to one),
    # the pipeline picks the primary file from the format listed
    # earliest. Default matches the industry preference:
    #   m4b — single-file with embedded chapters (best)
    #   m4a — single-file, no chapters
    #   mp3 — multi-part legacy format
    # Within the chosen format, the largest file still wins (same
    # as the baseline behaviour). No effect on single-format
    # torrents — the setting only matters on mixed-format bundles.
    "audiobook_format_priority": ["m4b", "m4a", "mp3"],

    # ── Calibre integration ─────────────────────────────────
    "calibre_library_path": "",
    # Web UI URLs for dashboard quick-launch buttons.
    "cwa_web_url": "",
    "calibre_web_url": "",
    # Staging directory where files land before metadata review + calibredb add.
    "staging_path": "",
    # Mandatory manual-review queue. When enabled, every downloaded
    # book lands in the review queue and waits for user approval
    # before being delivered to the configured sink. Per the v1.0
    # spec, this is ALWAYS on (power users can't skip review).
    "review_queue_enabled": True,
    # Directory where patched, ready-for-review book files are parked
    # while awaiting user decision. Each pending review gets its own
    # `grab-<id>/` subfolder. Distinct from staging_path, which is
    # used only during metadata extraction.
    "review_staging_path": "",
    # Bundle/collection detection. When True (default from v2.7.0),
    # the pipeline classifies multi-file torrents into one or more
    # `BookGroup`s — a bundle of three distinct works produces three
    # review-queue entries instead of one (with the other two books
    # silently dropped, as v2.6 and earlier did). Multi-format and
    # multi-part-audiobook torrents still resolve to a single group
    # via the stem-dedupe and audiobook-parts pre-checks, so the
    # default-ON behavior is structurally safe. Flip to False to fall
    # back to the pre-v2.7 "always one group per torrent" path if a
    # classifier misfire ever surfaces in production.
    "bundle_detection_enabled": True,
    # If review queue items aren't decided within N days, auto-add to Calibre
    # with whatever metadata the file ships with (no enrichment).
    "metadata_review_timeout_days": 14,
    # How often the review-timeout job runs (seconds). A daily tick
    # is plenty since the grace period is measured in days.
    "review_timeout_check_interval_seconds": 86400,
    # Delayed torrents folder: when the queue is full, the oldest
    # queue item's .torrent bytes get dumped here (FIFO rotation)
    # so a new accepted grab can take its slot without losing data.
    "delayed_torrents_path": "",

    # ── Metadata enrichment (Tier 4) ────────────────────────
    # Off by default — flip to True once you're ready for the
    # pipeline to start hitting outbound metadata scrapers for
    # every downloaded book. Cover images and rich metadata
    # land in the review queue automatically.
    "metadata_enrichment_enabled": False,
    # Provider priority. Walked in order; the first result that
    # scores >= metadata_accept_confidence short-circuits the rest.
    "metadata_provider_priority": [
        "goodreads",
        "amazon",
        "hardcover",
        "kobo",
        "ibdb",
        "google_books",
        "audible",
    ],
    # Audiobook-specific provider priority. Used when the pipeline
    # detects an audiobook grab (format=m4b/mp3/m4a or MAM category
    # starts with "audiobooks"). Audible leads because it carries
    # the audiobook-specific fields (narrator, duration, ASIN) and
    # hydrates its own hits through Audnexus internally; ebook
    # sources backfill description / ISBN / cover.
    "metadata_audiobook_priority": [
        "audible",
        "goodreads",
        "hardcover",
        "google_books",
    ],
    # Phase 7 unified metadata source configuration. `metadata_sources`
    # is per-source toggles + rate limit; `metadata_priority` is per-
    # content-type ordered name lists (priority rank comes from list
    # position). Seeded empty — lifespan startup runs a one-shot
    # migration from the legacy `*_enabled` / `rate_*` /
    # `metadata_provider_priority` / `metadata_audiobook_priority`
    # settings if these are empty. After migration the new shape is
    # authoritative; the legacy keys stay shadow-synced at PATCH
    # time so any code still reading the old names keeps working.
    "metadata_sources": {},
    "metadata_priority": {
        "ebook": [],
        "audiobook": [],
    },
    # Audible regional catalog selector. Maps English-speaking
    # markets first — .com, .co.uk, .com.au, .ca — plus non-English
    # markets via Audnexus's region codes. "us" stays the safe
    # default because .com has the largest catalog regardless of
    # the user's Audible account region.
    "audible_region": "us",
    # Providers the user has explicitly disabled (names that appear
    # in metadata_provider_priority but should be skipped). Names
    # here must match MetaSource.name.
    "metadata_disabled_sources": [],
    # Per-source timeout in seconds. A single stuck scraper can't
    # block the pipeline longer than this.
    "metadata_per_source_timeout": 15.0,
    # Confidence in [0, 1] that short-circuits the provider loop.
    # Tuned so exact title+author matches stop immediately and
    # fuzzy matches fall through to the next provider.
    "metadata_accept_confidence": 0.8,

    # ── Notifications ───────────────────────────────────────
    "ntfy_url": "",
    "ntfy_topic": "seshat",
    "daily_digest_enabled": True,
    "daily_digest_hour": 9,  # local time, 24h
    # Per-event notifications: fire a ntfy for every grab submitted
    # and every download that finishes. Off by default — the digests
    # usually give enough signal without firehose-grade spam.
    "per_event_notifications": False,
    # Granular notification type toggles.
    "notify_on_grab": True,
    "notify_on_download_complete": True,
    "notify_on_pipeline_error": True,
    # v2.12.0 — new per-event toggles. Each gates one previously-ungated
    # ntfy emit site that the v2.12.0 audit caught firing regardless of
    # user preference.
    "notify_on_review_queued": True,       # `notify_pipeline_complete(sink="review_queue")` — book ready for review-queue approval
    "notify_on_library_ingest": True,      # `notify_pipeline_complete(sink="<library_app>")` — book landed in CWA / Audiobookshelf / etc.
    "notify_on_buffer_gate_block": True,   # `notify_buffer_gate_block` — autograb refused due to buffer (throttled 6h)
    # v2.13.0 — fires on the weekly Goodreads canary probe when it
    # detects a soft-block (HTTP 202 / empty 2xx body). Default True;
    # users on the Phase-A no-cookie path want to know when the
    # bypass stops working so they can refresh credentials or
    # investigate.
    "notify_on_goodreads_canary_failed": True,
    # v2.21.0 Phase G — metadata-cache worker notifications. Three
    # tiers: error / warning are on by default (operator should hear
    # about real problems), daily-summary + new-book are opt-in
    # (informational, not actionable). Source-agnostic naming —
    # extends to a future Goodreads cache without renaming.
    "notify_on_metadata_cache_error": True,
    "notify_on_metadata_cache_warning": True,
    "notify_on_metadata_cache_daily_summary": False,
    "notify_on_metadata_cache_new_book": False,
    # Hour-of-day (0-23 local time) for the metadata-cache daily
    # summary ntfy + counter reset. Defaults to 9am to mirror the
    # `daily_digest_hour` cadence. The scheduler job that fires this
    # also resets the in-memory `today_scan_count` / `today_block_count`
    # columns so the daily-summary number is for the past 24h, not
    # all-time-since-deploy.
    "metadata_cache_daily_summary_hour": 9,
    # Optional rotated file handler for the metadata-cache worker
    # logger. Default OFF — the worker's INFO/WARN/ERROR lines already
    # show up in the main container log under the
    # `seshat.discovery.metadata_cache_worker` namespace. The rotated
    # file is for users who want a dedicated tail target.
    "metadata_cache_log_file_enabled": False,
    "metadata_cache_log_file_max_bytes": 1_000_000,
    "metadata_cache_log_file_backup_count": 3,
    # Seconds with no `last_heartbeat_at` write before the watchdog
    # fires an error ntfy. 5 min ≈ 5x the worst-case jittered tick
    # cadence (90s scan + 60s idle), so transient delays don't trip
    # the alarm.
    "metadata_cache_stall_threshold_s": 300,
    "notify_daily_accepted": True,
    "notify_daily_tentative": True,
    "notify_daily_ignored": True,
    "notify_weekly_digest": True,
    # Download folder structure options.
    # "monthly" = [YYYY-MM]/ subfolders (default)
    # "yearly"   = [YYYY]/ subfolders
    # "author"   = Author Name/ subfolders
    # "flat"     = all in root download path
    # "template" = user-defined nesting (see download_folder_template)
    "download_folder_structure": "monthly",
    # Phase 5 — format string for `download_folder_structure="template"`.
    # Tokens: {author}, {series}, {title}. Empty defaults to "{author}",
    # which matches the legacy "author" mode exactly. Empty segments
    # (e.g. {series} on a standalone book) are dropped, not left as
    # empty directories. See app/orchestrator/download_folders.py.
    "download_folder_template": "",
    # Emergency export folder: if the configured sink (CWA/Calibre)
    # is unreachable after multiple retries, books are dumped here
    # so they're not lost. The user can manually import them later.
    "emergency_export_path": "",
    # How many times to retry sink delivery before dumping to the
    # emergency folder. Each retry happens on the next review-timeout
    # tick (daily by default).
    "sink_max_retries": 3,

    # ── MAM economy (Tier 1 MouseSearch port) ──────────────
    # All enable flags default False. The first deploy of a new
    # Seshat version is a silent no-op loop — users opt into each
    # feature explicitly via Settings. Manual "Buy now" clicks from
    # the UI bypass `*_enabled` (so users can test integration) but
    # still honor `mam_economy_last_*_buy_at` as a shared-timestamp
    # lockout with the scheduler.

    # VIP auto-buy loop
    "mam_economy_vip_enabled": False,
    "mam_economy_vip_interval_hours": 24,
    # Refuse to auto-buy when seedbonus is under this floor. 0
    # disables the floor (will fire as long as the weeks purchase is
    # individually affordable). Protects against auto-buy draining
    # the pool after a big manual spend.
    "mam_economy_vip_min_bonus": 0,
    # Weeks to buy per firing tick. 4/8/12 are fixed MAM amounts;
    # "max" lets MAM decide how much to credit based on available
    # BP and the 90-day VIP cap. Stored as int in settings.json —
    # "max" is a string that JSON handles transparently.
    "mam_economy_vip_weeks": 4,
    "mam_economy_last_vip_buy_at": 0.0,

    # Upload-credit auto-buy loop (three independent triggers —
    # ratio → buffer → bonus in priority order, first to fire wins)
    "mam_economy_upload_enabled": False,
    "mam_economy_upload_interval_hours": 6,
    "mam_economy_upload_ratio_trigger": False,
    "mam_economy_upload_ratio_floor": 1.5,
    "mam_economy_upload_ratio_chunk_gb": 50,
    "mam_economy_upload_buffer_trigger": False,
    "mam_economy_upload_buffer_floor_gb": 10,
    "mam_economy_upload_buffer_chunk_gb": 50,
    "mam_economy_upload_bonus_trigger": False,
    "mam_economy_upload_bonus_ceiling": 5000,
    "mam_economy_last_upload_buy_at": 0.0,

    # Pre-download buffer gate (commit 5 will wire this into the
    # policy engine / dispatcher; the key is defined here so the
    # settings migration is a single commit).
    "mam_economy_buffer_gate_enabled": False,
    "mam_economy_buffer_gate_safety_margin_gb": 1,

    # Per-grab wedge / personal-FL offer flags (commit 6 wires the
    # inject router + frontend). Both default False so the first
    # deploy shows no new checkboxes on the manual inject dialog.
    # `manual_wedge_offer_enabled` controls the "use a wedge for
    # this one" checkbox (drains pool, overrides global
    # policy_use_wedge=False on a per-grab basis).
    # `fl_wedge_offer_enabled` controls the "buy personal FL (50k
    # BP) for this one" checkbox (calls bonusBuy spendtype=personalFL).
    "mam_economy_manual_wedge_offer_enabled": False,
    "mam_economy_fl_wedge_offer_enabled": False,

    # First-run intro banner on MamPage — dismissed once via the
    # Settings UI and never shown again.
    "mam_economy_intro_dismissed": False,

    # Dry-run / preview mode. When true, bonusBuy.php wrappers short-
    # circuit to a canned BuyResult(success=True) without hitting MAM,
    # audit rows get a `[DRY RUN]` prefix so the history tile shows
    # simulated rows distinctly, and the scheduler/router skip bumping
    # the shared `mam_economy_last_*_buy_at` timestamps (so toggling
    # back off doesn't leave phantom lockouts). Useful for: demoing
    # the UI without burning BP, practicing operator workflows, and
    # catching UI regressions in the config -> buy -> audit chain.
    "mam_economy_dry_run": False,

    # ── Cron / scheduled jobs ───────────────────────────────
    # MAM keeps a session cookie alive as long as we make at least one
    # API call within a 15-day window. Seshat's cookie auto-rotation
    # only fires when something else triggers a MAM call (an inject,
    # an IRC-driven grab) — if Seshat sits idle for 15+ days the
    # cookie expires silently. The keep-alive job hits MAM's search
    # endpoint on a fixed schedule WELL inside that window so the
    # rotation handler always has something to chew on.
    #
    # Default 168 hours (7 days) — half the 15-day window gives us a
    # generous safety margin. Even if the container crashes right
    # before the job fires, the next restart still has ~7-8 days of
    # grace before the cookie would actually expire.
    "cookie_keepalive_interval_hours": 168,
    # How often to retry grabs that failed with cookie_expired. The job
    # is a no-op when there are no failed grabs, so this mostly affects
    # latency between cookie rotation and automatic retry.
    "cookie_retry_interval_seconds": 300,

    # ── Pipeline enable/disable toggles ──────────────────────
    # Master switches for each stage of the pipeline. Disabling a
    # stage skips it entirely — useful for testing, maintenance, or
    # going away and not wanting automatic processing to fire.
    "pipeline_auto_train_enabled": True,

    # ── Discovery domain (library scanning & metadata lookup) ─
    "hardcover_api_key": "",
    # Per-source enable/disable and rate limits are now stored under
    # the unified `metadata_sources` dict (see Phase 7). The legacy
    # scatter of `goodreads_enabled` / `rate_goodreads` / etc. was
    # retired once lookup.py started reading from `metadata_sources`
    # directly via the derivation helpers in app.metadata.source_config.
    "google_books_auto_disabled_at": None,
    # v2.13.0 — Goodreads Cloudflare soft-block state. Written by
    # `app/metadata/goodreads_session.py` on every response. Read by
    # the enricher dispatcher (skip Goodreads when soft_blocked) and
    # by the Settings GoodreadsStatusCard. Protected from PATCH via
    # `_RUNTIME_STATE_KEYS` in app/routers/settings.py.
    "goodreads_session_state": "unknown",
    "goodreads_session_state_since": None,
    "goodreads_session_last_status": None,
    "theme": "dark",
    "languages": ["English"],
    "lookup_interval_days": 3,
    "library_sync_interval_minutes": 60,
    # Per-library override — when > 0, audiobookshelf libraries
    # sync on this interval independently of the global one above.
    # 0 (default) inherits library_sync_interval_minutes. The
    # scheduler still fires every library_sync_interval_minutes;
    # per-library gates inside sync_all_libraries skip individual
    # libraries until their own interval has elapsed.
    "abs_sync_interval_minutes": 0,
    "author_scanning_enabled": True,
    "author_scan_owned_only": False,
    "exclude_audiobooks": True,
    "calibre_url": "",
    # Discovery-side MAM scanning (search for missing books on MAM).
    "mam_enabled": False,
    "mam_scanning_enabled": True,
    "mam_skip_ip_update": True,
    "mam_scan_interval_minutes": 360,
    # Days within which a Possible/Not Found result is considered
    # "recently scanned" and skipped on the next bulk scan. Prevents
    # the scan front from getting stuck cycling through the same
    # slow-moving tail every tick. 0 = always re-scan everything
    # (legacy behavior). Manual sidebar rescans always bypass this.
    "mam_recent_scan_skip_days": 7,
    "mam_format_priority": ["epub", "azw", "azw3", "pdf", "djvu", "azw4"],
    "rate_mam": 2,
    "last_mam_validated_at": None,
    # Per-library state.
    "active_library": "",
    "library_mtimes": {},
    "library_sync_state": {},
    "library_sources": [],
    # Discovery-side notification toggles.
    "ntfy_on_scan_complete": True,
    "ntfy_on_new_books": True,
    "ntfy_on_mam_complete": True,
    "ntfy_on_pipeline_sent": True,
    "ntfy_on_library_sync": False,
    "ntfy_on_mam_cookie_rotated": False,
    "ntfy_digest_enabled": False,
    "ntfy_digest_schedule": "daily",

    # ── Operational ─────────────────────────────────────────
    "verbose_logging": False,
    "dry_run": False,  # mirror of SESHAT_DRY_RUN, runtime-toggleable
    "mam_debug_match_enabled": False,
    # Aggressive cover-pHash demotion: when True, demotion fires even
    # without a cover-promote anchor — wrong-Possible candidates get
    # filtered out of the pool, cascade falls through to Possible
    # (typically) or Not Found. When False, promoter-anchored mode
    # (only filter when at least one candidate cover-promotes — safer
    # for Cohort C books whose right tid has visually-different cover
    # but no other promoter exists). Default True per Mark's preference
    # (minimize Possible-band noise, accept residual Cohort C risk
    # given the rescue mechanisms in B3a/b that catch most cases).
    "mam_aggressive_cover_demotion": True,
    "setup_complete": False,
}


def apply_logging(verbose: bool = False):
    """Configure log levels based on the verbose toggle."""
    level = logging.DEBUG if verbose else logging.INFO
    for name in [
        "seshat",
        "seshat.config",
        "seshat.database",
        # Pipeline domain
        "seshat.mam",
        "seshat.mam.irc",
        "seshat.mam.cookie",
        "seshat.mam.grab",
        "seshat.filter",
        "seshat.clients",
        "seshat.sinks",
        "seshat.metadata",
        "seshat.notify",
        # Discovery domain
        "seshat.discovery",
        "seshat.goodreads",
        "seshat.hardcover",
        "seshat.kobo",
        "seshat.lookup",
        "seshat.calibre_sync",
    ]:
        logging.getLogger(name).setLevel(level)
    # httpx is too noisy at DEBUG.
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("seshat").info(
        f"Logging set to {'VERBOSE (DEBUG)' if verbose else 'NORMAL (INFO)'}"
    )


# ─── Settings cache ──────────────────────────────────────────
# Cache the parsed dict keyed by the settings file's mtime. Any
# save_settings() bumps the mtime, which invalidates the cache on the
# next load_settings() call automatically.
_settings_cache: dict = {"mtime": object(), "data": None}


def _apply_legacy_settings_migrations(settings: dict) -> bool:
    """Mutate `settings` in place to rewrite legacy shapes.

    Returns True if anything changed (caller persists if so).
    Idempotent — running on already-migrated settings is a no-op.

    v2.9.0 migration: `accept_audiobook_announces` boolean → membership
    of "audiobooks" in `allowed_formats`. The legacy flag did two
    things at runtime:
      1. Merged `allowed_audiobook_categories` into runtime categories.
      2. Auto-added "audiobooks" to `allowed_formats` when that list
         was non-empty.
    Step (1) is now driven by `audiobooks` being filter-allowed
    (see `_build_filter_config` in app/main.py). Step (2) is folded
    into the saved `allowed_formats` directly so the UI shows the
    user's true acceptance set without runtime mutation.

    Migration rule:
      * accept_audiobook_announces was True AND allowed_formats was
        non-empty AND "audiobooks" not in it → add it.
      * accept_audiobook_announces was True AND allowed_formats was
        empty → leave allowed_formats empty (empty = accept all,
        which already accepts audiobooks).
      * accept_audiobook_announces was False → no change to
        allowed_formats; the audiobooks chip stays off.
      * After applying, drop the legacy key.
    """
    changed = False
    if "accept_audiobook_announces" in settings:
        legacy_on = bool(settings.get("accept_audiobook_announces"))
        if legacy_on:
            formats = list(settings.get("allowed_formats") or [])
            if formats and "audiobooks" not in formats:
                formats.append("audiobooks")
                settings["allowed_formats"] = formats
                changed = True
        del settings["accept_audiobook_announces"]
        changed = True
    return changed


def load_settings() -> dict:
    """Load settings.json, merged over DEFAULT_SETTINGS, with mtime cache."""
    try:
        cur_mtime = SETTINGS_PATH.stat().st_mtime if SETTINGS_PATH.exists() else None
    except OSError:
        cur_mtime = None

    if _settings_cache["data"] is not None and cur_mtime == _settings_cache["mtime"]:
        return _settings_cache["data"]

    if SETTINGS_PATH.exists():
        try:
            with open(SETTINGS_PATH) as f:
                saved = json.load(f)
            merged = {**DEFAULT_SETTINGS, **saved}
            # Apply legacy-shape migrations BEFORE caching so callers
            # see only the current shape. If anything changed, persist
            # so the file on disk matches what the running process holds.
            if _apply_legacy_settings_migrations(merged):
                try:
                    save_settings(merged)
                    _log.info(
                        "Applied legacy settings migrations and resaved "
                        "settings.json"
                    )
                except Exception:
                    _log.exception(
                        "Failed to persist migrated settings; in-memory "
                        "copy is correct but disk still has legacy shape"
                    )
            try:
                cur_mtime = SETTINGS_PATH.stat().st_mtime
            except OSError:
                pass
            _settings_cache["data"] = merged
            _settings_cache["mtime"] = cur_mtime
            return merged
        except Exception as e:
            _log.warning(f"Failed to read {SETTINGS_PATH}: {e}; falling back to defaults")

    # First run — start from defaults and seed from env vars.
    settings = dict(DEFAULT_SETTINGS)
    _apply_env_overrides(settings)
    save_settings(settings)
    try:
        _settings_cache["mtime"] = SETTINGS_PATH.stat().st_mtime
    except OSError:
        _settings_cache["mtime"] = None
    _settings_cache["data"] = settings
    return settings


def _apply_env_overrides(settings: dict):
    """Seed settings from env vars on first run only."""
    if ENV_MAM_SESSION_ID and not settings.get("mam_session_id"):
        settings["mam_session_id"] = ENV_MAM_SESSION_ID
    if ENV_MAM_IRC_NICK and not settings.get("mam_irc_nick"):
        settings["mam_irc_nick"] = ENV_MAM_IRC_NICK
    if ENV_MAM_IRC_ACCOUNT and not settings.get("mam_irc_account"):
        settings["mam_irc_account"] = ENV_MAM_IRC_ACCOUNT
    if ENV_MAM_IRC_PASSWORD and not settings.get("mam_irc_password"):
        settings["mam_irc_password"] = ENV_MAM_IRC_PASSWORD
    if ENV_QBIT_URL and not settings.get("qbit_url"):
        settings["qbit_url"] = ENV_QBIT_URL
    if ENV_QBIT_USERNAME and not settings.get("qbit_username"):
        settings["qbit_username"] = ENV_QBIT_USERNAME
    if ENV_QBIT_PASSWORD and not settings.get("qbit_password"):
        settings["qbit_password"] = ENV_QBIT_PASSWORD
    # qbit_watch_category has a non-empty default ("[mam-reseed]"), so the
    # usual `not settings.get(...)` guard would silently ignore an env var
    # override. We compare against the default instead so the env var only
    # wins on first run, never overrides a value the user has explicitly
    # changed via the (future) Settings UI.
    if (
        ENV_QBIT_WATCH_CATEGORY
        and settings.get("qbit_watch_category") == DEFAULT_SETTINGS["qbit_watch_category"]
    ):
        settings["qbit_watch_category"] = ENV_QBIT_WATCH_CATEGORY
    # qbit_tag also has a non-empty default ("seshat-seed"); same
    # rule — env var only wins on first run vs the default.
    if (
        ENV_QBIT_TAG
        and settings.get("qbit_tag") == DEFAULT_SETTINGS["qbit_tag"]
    ):
        settings["qbit_tag"] = ENV_QBIT_TAG
    if ENV_CALIBRE_LIBRARY_PATH and not settings.get("calibre_library_path"):
        settings["calibre_library_path"] = ENV_CALIBRE_LIBRARY_PATH
    if ENV_STAGING_PATH and not settings.get("staging_path"):
        settings["staging_path"] = ENV_STAGING_PATH
    if ENV_NTFY_URL and not settings.get("ntfy_url"):
        settings["ntfy_url"] = ENV_NTFY_URL
    if ENV_VERBOSE_LOGGING and not settings.get("verbose_logging"):
        settings["verbose_logging"] = True
    if ENV_DRY_RUN and not settings.get("dry_run"):
        settings["dry_run"] = True
    if ENV_MAM_DEBUG_MATCH and not settings.get("mam_debug_match_enabled"):
        settings["mam_debug_match_enabled"] = True
    # Discovery-domain env var seeds.
    if ENV_HARDCOVER_API_KEY and not settings.get("hardcover_api_key"):
        settings["hardcover_api_key"] = ENV_HARDCOVER_API_KEY
    if ENV_CALIBRE_WEB_URL and not settings.get("calibre_web_url"):
        settings["calibre_web_url"] = ENV_CALIBRE_WEB_URL
    if ENV_CALIBRE_URL and not settings.get("calibre_url"):
        settings["calibre_url"] = ENV_CALIBRE_URL
    if ENV_ABS_URL and not settings.get("abs_url"):
        settings["abs_url"] = ENV_ABS_URL


def save_settings(settings: dict):
    """Persist settings.json and warm the cache."""
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)
    try:
        _settings_cache["mtime"] = SETTINGS_PATH.stat().st_mtime
    except OSError:
        _settings_cache["mtime"] = None
    _settings_cache["data"] = dict(settings)


# ─── Library discovery ───────────────────────────────────────

def slugify(name: str) -> str:
    """Convert a folder name to a safe slug for DB filenames."""
    s = name.lower().strip()
    s = _re.sub(r'[^a-z0-9]+', '-', s)
    s = s.strip('-')
    return s or 'default'


def get_extra_mount_paths() -> list[str]:
    """Collect extra mount paths from all registered library apps."""
    from app.library_apps import get_all_apps
    all_paths: list[str] = []
    for _app_type, app in get_all_apps().items():
        for p in app.get_extra_paths():
            if p not in all_paths:
                all_paths.append(p)
    if CALIBRE_EXTRA_PATHS:
        for p in [x.strip() for x in CALIBRE_EXTRA_PATHS.split(",") if x.strip()]:
            try:
                exists = Path(p).exists()
            except (PermissionError, OSError):
                exists = False
            if exists and p not in all_paths:
                all_paths.append(p)
    return all_paths


def discover_libraries(settings=None) -> list[dict]:
    """Find all libraries from all registered source apps.

    Priority:
    1. User-configured library_sources in settings
    2. Registered library apps whose `app_type` did NOT contribute to
       Priority 1 (each checks its own env var / settings fallback)
    3. CALIBRE_DB_PATH env var (legacy single-library fallback)

    Pre-v1.3 bug: Priority 1 returned early when it found anything,
    which blocked the API-based AudiobookshelfApp (no library_sources
    entry, discovers via `abs_url` setting) from ever running when a
    user had Calibre configured via library_sources. Fix: compose
    Priority 1 + 2 instead of branching. Each app_type still only
    contributes once — Priority 2 skips any app_type that Priority 1
    already covered.
    """
    from app.library_apps import get_all_apps

    libraries: list[dict] = []
    seen_slugs: set[str] = set()
    covered_app_types: set[str] = set()

    def _add_library(lib_dict):
        slug = lib_dict["slug"]
        base_slug = slug
        counter = 2
        while slug in seen_slugs:
            slug = f"{base_slug}-{counter}"
            counter += 1
        seen_slugs.add(slug)
        lib_dict["slug"] = slug
        libraries.append(lib_dict)

    # Priority 1: User-configured library sources (from Settings UI)
    if settings and settings.get("library_sources"):
        for src in settings["library_sources"]:
            src_path = src.get("path", "")
            src_type = src.get("type", "root")
            src_app = src.get("app_type", "calibre")
            if not src_path:
                continue
            app = get_all_apps().get(src_app)
            if not app:
                _log.warning(f"Unknown app type '{src_app}' in library_sources, skipping")
                continue
            before_count = len(libraries)
            if src_type == "root":
                for lib in app.discover(src_path):
                    _add_library(lib)
            elif src_type == "direct":
                mdb = Path(src_path)
                try:
                    mdb_exists = mdb.exists()
                except (PermissionError, OSError) as e:
                    _log.warning(f"Direct library path unreadable: {src_path} ({e})")
                    mdb_exists = False
                if mdb_exists and mdb.name == app.db_filename:
                    _add_library({
                        "name": mdb.parent.name,
                        "slug": slugify(mdb.parent.name),
                        "app_type": app.app_type,
                        "content_type": app.content_type,
                        "display_name": app.display_name,
                        "source_db_path": str(mdb),
                        "library_path": str(mdb.parent),
                    })
                else:
                    _log.warning(f"Direct library path not found or invalid: {src_path}")
            # Only mark as covered if the entry actually produced a
            # library — lets Priority 2 retry via env var for an entry
            # whose configured path was invalid.
            if len(libraries) > before_count:
                covered_app_types.add(src_app)

    # Priority 2: Registered library apps not covered by Priority 1.
    # API-based apps (ABS) never appear in library_sources so they
    # always land here; file-based apps that Priority 1 already
    # handled are skipped.
    for _app_type, app in get_all_apps().items():
        if _app_type in covered_app_types:
            continue
        root_path = app.get_root_path()
        if root_path:
            found = app.discover(root_path)
            for lib in found:
                _add_library(lib)

    if libraries:
        return libraries

    # Priority 3: Legacy CALIBRE_DB_PATH (single direct path)
    if CALIBRE_DB_PATH:
        legacy_mdb = Path(CALIBRE_DB_PATH)
        try:
            legacy_exists = legacy_mdb.exists()
        except (PermissionError, OSError) as e:
            _log.warning(f"Legacy CALIBRE_DB_PATH unreadable: {CALIBRE_DB_PATH} ({e})")
            legacy_exists = False
        if legacy_exists:
            _add_library({
                "name": legacy_mdb.parent.name,
                "slug": slugify(legacy_mdb.parent.name),
                "app_type": "calibre",
                "content_type": "ebook",
                "display_name": "Calibre",
                "source_db_path": str(legacy_mdb),
                "library_path": str(legacy_mdb.parent),
            })

    return libraries
