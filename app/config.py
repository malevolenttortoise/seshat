"""
Configuration loading and persistence.

Two layers of config, mirroring the AthenaScout pattern:

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

# Web server bind. NOT 8787 — that's AthenaScout.
ENV_WEBUI_HOST = os.getenv("WEBUI_HOST", "0.0.0.0")
ENV_WEBUI_PORT = int(os.getenv("WEBUI_PORT", "8789"))

# Verbose logging toggle (DEBUG level vs INFO).
ENV_VERBOSE_LOGGING = os.getenv("VERBOSE_LOGGING", "").lower() in ("true", "1", "yes")

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

# Auth secret — for HTTP-only session cookies. Same handling as AthenaScout:
# env var takes priority, then a file under DATA_DIR, then in-memory fallback.
ENV_AUTH_SECRET = os.getenv("SESHAT_AUTH_SECRET", "")

# Dry-run mode: connect to real IRC and parse real announces, but never fetch
# .torrent files or talk to qBittorrent. Used for testing without burning
# snatch budget.
ENV_DRY_RUN = os.getenv("SESHAT_DRY_RUN", "").lower() in ("true", "1", "yes")

# ── Discovery-domain env vars (from AthenaScout) ────────────
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
    "allowed_formats": [],
    "excluded_formats": [],
    # Audiobook acceptance toggle. When True, the filter builder
    # merges `allowed_audiobook_categories` into `allowed_categories`
    # (so audiobook announces pass the category gate) and adds
    # "audiobooks" to `allowed_formats` when `allowed_formats` is
    # non-empty (preserving explicit format gating). When False, this
    # is all a no-op — ebook-only deployments stay exactly as they
    # were. Keep ebook and audiobook configuration separate so the
    # user can toggle audiobook acceptance without editing their
    # existing ebook category list.
    "accept_audiobook_announces": False,
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
    # Whether to look up the torrent's VIP/FL status via the MAM
    # search API when the IRC announce alone isn't enough. Adds one
    # HTTP round-trip per announce that passes the filter.
    "policy_lookup_torrent_info": True,

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
    # watch category (pre-Seshat / Hermeece-era / prior install) are
    # treated as pre-existing and skipped, so a long-running qBit
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
    # Base download directory for qBit. When monthly_download_folders is
    # True, Seshat creates [YYYY-MM]/ subfolders here and tells qBit
    # to save each download in the current month's folder.
    # This should match the path AS SEEN BY QBIT (inside qBit's container
    # if using Docker). E.g. "/data/[mam-complete]".
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
    # Organize downloads into monthly subfolders ([2026-04]/, [2026-05]/).
    "monthly_download_folders": True,
    # How often to poll qBit for completed torrents and seedtime updates.
    "qbit_poll_interval_seconds": 60,

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
    # starts with "audiobooks"). Audible + Audnexus lead because
    # they carry the audiobook-specific fields (narrator, duration,
    # ASIN); ebook sources backfill description / ISBN / cover.
    "metadata_audiobook_priority": [
        "audible",
        "audnexus",
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
    "notify_daily_accepted": True,
    "notify_daily_tentative": True,
    "notify_daily_ignored": True,
    "notify_weekly_digest": True,
    # Download folder structure options.
    # "monthly" = [YYYY-MM]/ subfolders (default)
    # "yearly"  = [YYYY]/ subfolders
    # "author"  = Author Name/ subfolders
    # "flat"    = all in root download path
    "download_folder_structure": "monthly",
    # Emergency export folder: if the configured sink (CWA/Calibre)
    # is unreachable after multiple retries, books are dumped here
    # so they're not lost. The user can manually import them later.
    "emergency_export_path": "",
    # How many times to retry sink delivery before dumping to the
    # emergency folder. Each retry happens on the next review-timeout
    # tick (daily by default).
    "sink_max_retries": 3,

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
    "weekly_audit_day": "sunday",
    "weekly_audit_hour": 3,

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
    "mam_format_priority": ["epub", "azw", "azw3", "pdf", "djvu", "azw4"],
    "rate_mam": 2,
    "last_mam_validated_at": None,
    # Per-library state.
    "active_library": "",
    "library_mtimes": {},
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
# Same pattern as AthenaScout: cache the parsed dict keyed by the
# settings file's mtime. Any save_settings() bumps the mtime, which
# invalidates the cache on the next load_settings() call automatically.
_settings_cache: dict = {"mtime": object(), "data": None}


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


# ─── Library discovery (from AthenaScout) ────────────────────

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
