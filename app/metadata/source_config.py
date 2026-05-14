"""
Unified metadata source configuration.

Seshat's metadata / discovery sources were configured across many
scattered settings: `goodreads_enabled`, `rate_goodreads`,
`metadata_provider_priority`, `metadata_audiobook_priority`, and
friends. Phase 7 consolidates them into two keys:

    settings["metadata_sources"] = {
        "goodreads":   {"rate_limit": 2.0,
                        "ebook_enrich": True,  "ebook_scan": True,
                        "audiobook_enrich": False, "audiobook_scan": False},
        "audible":     {"rate_limit": 0.5,
                        "ebook_enrich": False, "ebook_scan": False,
                        "audiobook_enrich": True,  "audiobook_scan": True},
        ...
    }

    settings["metadata_priority"] = {
        "ebook":     ["mam", "goodreads", "hardcover", ...],
        "audiobook": ["mam", "audible", "hardcover", ...],
    }

The ORDER in `metadata_priority[content_type]` defines rank. The
toggles in `metadata_sources[name]` decide which sources run for
which surface (enrich vs scan). Enrich and scan share the same
priority order per content type, but can opt in/out independently.

This module exposes:

* `migrate_legacy_settings(settings)` — one-shot pure function that
  returns a NEW settings dict populated from the legacy keys. Safe
  to call repeatedly; returns early when both new keys already have
  content (idempotent).
* `derive_enrich_priority(settings, audiobook)` — live derivation
  consumed by `_build_metadata_enricher` in main.py.
* `derive_scan_priority(settings, content_type)` — same shape for
  the discovery-side author / library scanners.
* `sync_legacy_keys(settings)` — dual-write helper used by the
  /v1/metadata-sources PATCH path so old code still reading
  `goodreads_enabled` / `rate_goodreads` / `metadata_provider_priority`
  sees consistent values.

Every function is a pure transform over a settings dict — no I/O,
no globals — so unit tests can exercise the edge cases without
touching the real config file.
"""
from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger("seshat.metadata.source_config")


# All sources the app knows about. The migration seeds an entry for
# each of these (even if the legacy config didn't list them) so the
# new UI always shows every supported source. `available_for`
# describes where a source can run — e.g. Audible only makes sense
# for audiobook content, so its ebook toggles stay hidden/disabled
# in the UI.
#
# Audnexus is deliberately NOT listed here even though the
# `AudnexusSource` class still exists. It has no title/author search
# endpoint, so as a standalone toggleable source it would always log
# "no match". AudibleSource instantiates its own AudnexusSource
# internally to hydrate Audible catalog hits, and the pipeline calls
# `fetch_by_asin()` directly for m4b ASIN lookups — enabling/
# disabling Audible is already the user-facing control for the
# whole Audible+Audnexus chain.
KNOWN_SOURCES: dict[str, dict[str, Any]] = {
    "mam":         {"display": "MyAnonamouse",  "available_for": ("ebook", "audiobook"), "default_rate": 2.0, "mam_only": True},
    # Goodreads — v2.13.0 Stage 6: 5s ±1s jitter default (was 2.0s).
    # Conservative phased-rollout pace lets curl_cffi Chrome120 TLS
    # impersonation clear Cloudflare without tripping density-based
    # 202s. Existing installs keep their saved value; only fresh
    # installs get 5.0s. Power users can dial up to 8s+ for extra
    # margin or down for faster scans once Phase-A UAT confirms the
    # bypass holds.
    "goodreads":   {"display": "Goodreads",     "available_for": ("ebook", "audiobook"), "default_rate": 5.0},
    # Amazon: 30s default rate (was 2.0 through v2.10.x). v2.11.0
    # confirmed via single-shot probe that Amazon's bot detection
    # is density-based, not fingerprint-based: a one-off query
    # succeeds with our existing UA, but sustained 0.5-1 req/s
    # trips the counter within ~6-10 requests. 30s/req keeps us
    # well under the threshold for sustained discovery; for
    # enricher use the rate doesn't matter (per-book lookups are
    # naturally minutes apart from snatching cadence). User can
    # tighten the rate via Settings but Amazon discovery is off
    # by default anyway.
    "amazon":      {"display": "Amazon",        "available_for": ("ebook", "audiobook"), "default_rate": 30.0},
    "hardcover":   {"display": "Hardcover",     "available_for": ("ebook", "audiobook"), "default_rate": 1.0},
    "kobo":        {"display": "Kobo",          "available_for": ("ebook",),             "default_rate": 3.0},
    "ibdb":        {"display": "IBDB",          "available_for": ("ebook",),             "default_rate": 1.0},
    "google_books": {"display": "Google Books", "available_for": ("ebook", "audiobook"), "default_rate": 1.5},
    "openlibrary": {"display": "Open Library",  "available_for": ("ebook", "audiobook"), "default_rate": 1.0},
    "audible":     {"display": "Audible",       "available_for": ("audiobook",),         "default_rate": 0.5},
}


# Ship-with defaults derived from live-observed behaviour. Applied
# on fresh-install (no legacy settings present). Users can toggle
# anything after the fact via the Metadata Sources panel.
#
# `mandatory` (v2.3.2): when True, the source-scan layer fast-paths
# only on books THIS source has already URL'd. Books missing this
# source's URL trigger a DETAIL fetch every scan until the source
# either matches them or is no longer enabled. When False, the
# source fast-paths on any book that has at least one URL from any
# enabled source — preserves the pre-v2.3.2 behavior for
# supplementary sources where DETAIL on every unmatched book would
# be wasted effort. Default True on the primary tier (Goodreads /
# Hardcover for ebook; Audible for audiobook), False elsewhere.
_DEFAULT_NEW_INSTALL_STATE: dict[str, dict[str, Any]] = {
    "mam":         {"ebook_enrich": True,  "ebook_scan": True,  "audiobook_enrich": True,  "audiobook_scan": True,  "mandatory": False},
    "goodreads":   {"ebook_enrich": True,  "ebook_scan": True,  "audiobook_enrich": True,  "audiobook_scan": True,  "mandatory": True},
    # Amazon — v2.11.0 Stage 5++: Author-Store discovery re-enabled.
    # The pre-Stage-5++ density problem (45 detail GETs per author tripped
    # Akamai after 6-10 requests) is solved by the Author-Store flow:
    # 1 GET of /stores/author/{id}/allbooks returns 85 fully-populated
    # products from embedded JSON; remaining ASINs fetched via batched
    # POST /juvec. ~7 requests per author instead of 45, all behind
    # curl_cffi Chrome-120 impersonation.
    # `format` + `language` drive Amazon's own server-side filter API
    # via authorFilters in the /juvec POST body. Defaults to Kindle +
    # English (Mark's primary discovery target). UI dropdowns under
    # Settings → Sources → Amazon will offer paperback / hardcover /
    # mass_market and the other languages from content.languageFilter.
    # v2.11.1: audiobook_scan re-enabled now that the Author-Store
    # flow can server-side filter by audio bindings via /juvec
    # `authorFilters.format=["audible_audiobook"]`. mediaMatrix maps
    # ebook-canonical works to their Audible variants for free.
    # Audible-overlap is non-issue (merge layer dedupes by audible_id).
    # `audiobook_format` defaults to "audible_audiobook" (the
    # Audible-distributed digital format — dominant audio offering on
    # Amazon); UI exposes "Audio CD" / "MP3 CD" / "Preloaded Digital
    # Audio" for the niches.
    "amazon":      {"ebook_enrich": True,  "ebook_scan": True,  "audiobook_enrich": True,  "audiobook_scan": True,  "mandatory": False, "format": "kindle", "language": "English", "audiobook_format": "audible_audiobook"},
    "hardcover":   {"ebook_enrich": True,  "ebook_scan": True,  "audiobook_enrich": True,  "audiobook_scan": True,  "mandatory": True},
    # Kobo — v2.11.0 ships parallel detail-fetch via asyncio.Semaphore.
    # `concurrency` sets the worker count; each worker still respects
    # `rate_limit` between its own requests, so the effective request
    # rate is ~concurrency/rate_limit. Defaults give 4/3.0 = 1.33 req/s,
    # which stays below Cloudflare's Kobo soft-block threshold. Raising
    # concurrency without also raising rate_limit will trigger soft-blocks.
    "kobo":        {"ebook_enrich": True,  "ebook_scan": True,  "audiobook_enrich": False, "audiobook_scan": False, "mandatory": False, "concurrency": 4},
    "ibdb":        {"ebook_enrich": False, "ebook_scan": False, "audiobook_enrich": False, "audiobook_scan": False, "mandatory": False},
    # Google Books defaults off for audiobook enrich — rate-limited
    # and carries no audiobook-specific fields (narrator, duration,
    # ASIN). Keeps firing for ebook grabs where it's useful.
    "google_books": {"ebook_enrich": True, "ebook_scan": True,  "audiobook_enrich": False, "audiobook_scan": False, "mandatory": False},
    # Open Library — free, no key. v2.11.0: promoted to primary-tier
    # coverage role after harness showed 93% hit rate (vs 43%
    # Hardcover-mainstream-only). Cross-script aggregation + variant
    # query make it the best generalist for indie + translated work.
    "openlibrary": {"ebook_enrich": True,  "ebook_scan": True,  "audiobook_enrich": True,  "audiobook_scan": True,  "mandatory": False},
    "audible":     {"ebook_enrich": False, "ebook_scan": False, "audiobook_enrich": True,  "audiobook_scan": True,  "mandatory": True},
}


# Default priority order used on fresh installs. MAM always first —
# it's free and authoritative.
#
# v2.11.0 reshuffle — backed by the post-resolver-fix validation
# harness on 2026-05-13 (`docs/validation/sources-20260513-112040.md`):
#   - Hardcover #2 (was #4) — 14/14 = 100% hit rate with harness
#     throttle-hardening, fast (~2s/author), best ratings/social
#     signal. Yesterday's "Hardcover blind to indie" verdict was
#     pure harness artifact; HC actually has Robyn Bee + Hasekura
#     + every other benchmark author.
#   - OpenLibrary #3 (was #8) — 13/14 = 93% hit rate, free, the
#     only source that recovered Hasekura's full 94-book biblio
#     via cross-script aggregation. Strongest indie + translated
#     coverage.
#   - Goodreads stays at #4 (was #2) — depth gain not coverage
#     critical once we have HC+OL+GB+Kobo covering 13/14 without
#     it. When Phase 5.5 cookies land it adds depth, but the new
#     priority order doesn't assume it's available.
#   - Amazon dropped from discovery list — enricher only (see
#     _DEFAULT_NEW_INSTALL_STATE.amazon.ebook_scan=False).
#   - IBDB last — niche, downstream-filtered to ~1 book per author
#     in real production scans.
_DEFAULT_EBOOK_PRIORITY: list[str] = [
    "mam", "hardcover", "openlibrary", "goodreads",
    "google_books", "kobo", "amazon", "ibdb", "audible",
]
_DEFAULT_AUDIOBOOK_PRIORITY: list[str] = [
    "mam", "audible", "hardcover", "openlibrary",
    "goodreads", "google_books", "amazon", "kobo", "ibdb",
]


# ─── Migration ────────────────────────────────────────────────


def migrate_legacy_settings(settings: dict) -> bool:
    """Populate `metadata_sources` + `metadata_priority` from legacy keys.

    Mutates `settings` in-place. Returns True when a migration ran or
    a new-source backfill ran, False when nothing needed doing.

    Two distinct passes:

    1. **Legacy migration** — runs once per install, when the old
       per-source flat keys (`goodreads_enabled`, `rate_hardcover`,
       `metadata_provider_priority`, etc.) need rolling up into the
       unified `metadata_sources` + `metadata_priority` shape.

    2. **New-source backfill** — runs every time settings load. Adds
       any source that's been added to `KNOWN_SOURCES` since the
       install was last upgraded but isn't present in the persisted
       `metadata_sources` dict. Without this pass, sources added in
       new releases (e.g. `openlibrary` in v2.10.6) would silently
       never run for upgraded installs because the discovery scanner
       filters by per-source toggles in `metadata_sources`.

    On fresh install (all legacy keys empty/missing), the legacy
    migration seeds defaults from `_DEFAULT_NEW_INSTALL_STATE`.
    """
    # Retired-source scrub: `audnexus` was briefly exposed as a
    # standalone toggleable source in v1.4.0. It has no title/author
    # search endpoint, so it always logged "no match" — confusing
    # users into thinking the Audnexus catalog was unreliable, when
    # in fact Audible was hydrating its own hits through Audnexus
    # internally the whole time. Drop the row so the panel stops
    # showing a misleading toggle.
    _strip_retired_sources(settings)

    existing_sources = settings.get("metadata_sources") or {}
    existing_priority = settings.get("metadata_priority") or {}

    has_sources = bool(existing_sources)
    has_priority = bool(
        (existing_priority.get("ebook") or [])
        or (existing_priority.get("audiobook") or [])
    )

    # ── Pass 1: legacy migration (runs once per install) ──
    legacy_ran = False
    if not (has_sources and has_priority):
        ebook_priority = _derive_priority_list(
            settings, "metadata_provider_priority", _DEFAULT_EBOOK_PRIORITY,
        )
        audiobook_priority = _derive_priority_list(
            settings, "metadata_audiobook_priority", _DEFAULT_AUDIOBOOK_PRIORITY,
        )

        sources: dict[str, dict[str, Any]] = {}
        for name, meta in KNOWN_SOURCES.items():
            sources[name] = _build_source_entry(name, meta, settings)

        settings["metadata_sources"] = sources
        settings["metadata_priority"] = {
            "ebook": ebook_priority,
            "audiobook": audiobook_priority,
        }
        _log.info(
            "metadata sources migrated: %d sources, ebook priority=%d, "
            "audiobook priority=%d",
            len(sources), len(ebook_priority), len(audiobook_priority),
        )
        legacy_ran = True

    # ── Pass 2: new-source backfill (runs every load) ──
    backfill_ran = _backfill_known_sources(settings)

    return legacy_ran or backfill_ran


def _backfill_known_sources(settings: dict) -> bool:
    """Add any KNOWN_SOURCES entry / priority slot missing from settings.

    Idempotent — silently no-ops when settings already contain every
    known source. Mutates `settings` in-place; returns True iff any
    additions were made.

    Per-source toggle defaults come from `_DEFAULT_NEW_INSTALL_STATE`
    (falling back to "all off + non-mandatory" if that dict ever
    misses an entry — defensive, shouldn't fire in practice).
    Per-source rate limits come from `KNOWN_SOURCES[name].default_rate`.

    Priority slots are appended at the end of each list (lowest tier)
    so the existing user-curated order isn't disturbed. Power-user
    operators reorder via the Settings → Sources panel after the
    new source first appears.
    """
    sources = settings.setdefault("metadata_sources", {})
    priority = settings.setdefault(
        "metadata_priority", {"ebook": [], "audiobook": []},
    )
    priority.setdefault("ebook", [])
    priority.setdefault("audiobook", [])

    added_sources: list[str] = []
    for name, meta in KNOWN_SOURCES.items():
        if name in sources:
            continue
        defaults = _DEFAULT_NEW_INSTALL_STATE.get(name) or {
            "ebook_enrich": False, "ebook_scan": False,
            "audiobook_enrich": False, "audiobook_scan": False,
            "mandatory": False,
        }
        sources[name] = {
            **defaults,
            "rate_limit": meta.get("default_rate", 1.0),
        }
        added_sources.append(name)

    added_priority: list[tuple[str, str]] = []  # (list_name, source_name)
    for source_name in _DEFAULT_EBOOK_PRIORITY:
        if source_name in priority["ebook"]:
            continue
        if source_name in KNOWN_SOURCES and "ebook" in (
            KNOWN_SOURCES[source_name].get("available_for") or ()
        ):
            priority["ebook"].append(source_name)
            added_priority.append(("ebook", source_name))
    for source_name in _DEFAULT_AUDIOBOOK_PRIORITY:
        if source_name in priority["audiobook"]:
            continue
        if source_name in KNOWN_SOURCES and "audiobook" in (
            KNOWN_SOURCES[source_name].get("available_for") or ()
        ):
            priority["audiobook"].append(source_name)
            added_priority.append(("audiobook", source_name))

    if added_sources or added_priority:
        _log.info(
            "metadata-sources backfill: added %d source toggle(s)=%r, "
            "%d priority slot(s)=%r",
            len(added_sources), added_sources,
            len(added_priority), added_priority,
        )
        return True
    return False


# Sources that were once registered as user-facing toggles but have
# since been retired. Kept as a named set so the scrub helper below
# stays grep-able and future removals can append without editing the
# migration body.
_RETIRED_SOURCES: frozenset[str] = frozenset({
    "audnexus",
})


def _strip_retired_sources(settings: dict) -> None:
    """Drop retired source names from every place they could surface.

    Pure in-place mutation. Runs at the top of `migrate_legacy_settings`
    on every call so existing settings.json files get cleaned on first
    load after the upgrade.

    Covers:
      * `metadata_sources` — unified shape, remove the key
      * `metadata_priority.{ebook,audiobook}` — unified priority lists
      * `metadata_provider_priority` — legacy ebook priority
      * `metadata_audiobook_priority` — legacy audiobook priority

    The legacy-list scrub matters because the migration path derives
    the new priority from those keys when they're non-empty, so a
    retired name would otherwise re-seed into the new shape.
    """
    sources = settings.get("metadata_sources")
    if isinstance(sources, dict):
        for name in _RETIRED_SOURCES:
            sources.pop(name, None)

    priority = settings.get("metadata_priority")
    if isinstance(priority, dict):
        for key in ("ebook", "audiobook"):
            lst = priority.get(key)
            if isinstance(lst, list):
                priority[key] = [n for n in lst if n not in _RETIRED_SOURCES]

    for legacy_key in ("metadata_provider_priority", "metadata_audiobook_priority"):
        lst = settings.get(legacy_key)
        if isinstance(lst, list):
            settings[legacy_key] = [n for n in lst if n not in _RETIRED_SOURCES]


def _derive_priority_list(
    settings: dict, legacy_key: str, fallback: list[str],
) -> list[str]:
    """Build a priority list: legacy value if non-empty, else fallback."""
    raw = settings.get(legacy_key) or []
    if not isinstance(raw, list):
        return list(fallback)
    cleaned = [str(n).strip() for n in raw if isinstance(n, str) and n.strip()]
    if cleaned:
        # Ensure MAM is pinned at position 0 even if the legacy
        # priority list didn't include it — discovery-side lists
        # historically omitted MAM (which is ebook-only there).
        if "mam" not in cleaned:
            cleaned = ["mam"] + cleaned
        return cleaned
    return list(fallback)


def _build_source_entry(
    name: str, meta: dict, settings: dict,
) -> dict[str, Any]:
    """Construct one `metadata_sources[name]` row from legacy keys."""
    defaults = _DEFAULT_NEW_INSTALL_STATE.get(name, {
        "ebook_enrich": False, "ebook_scan": False,
        "audiobook_enrich": False, "audiobook_scan": False,
        "mandatory": False,
    })

    # Read legacy scan toggle: `<name>_enabled`. Three cases:
    #   1. MAM — always use ship-with defaults; `mam_enabled` guards
    #      the whole IRC listener, not source scanning.
    #   2. Legacy key ABSENT — fall through to the ship-with default
    #      for each surface independently. Sources that never had a
    #      legacy `*_enabled` key should inherit their per-surface
    #      defaults rather than a single fallback.
    #   3. Legacy key PRESENT — use that one bool to drive both
    #      surfaces, filtered by `available_for` so Kobo (ebook-only)
    #      doesn't flip its audiobook toggle on.
    legacy_scan_key = f"{name}_enabled"
    legacy_present = legacy_scan_key in settings
    if name == "mam" or not legacy_present:
        ebook_scan = defaults["ebook_scan"]
        audiobook_scan = defaults["audiobook_scan"]
    else:
        scan_enabled = bool(settings.get(legacy_scan_key))
        avail = meta.get("available_for", ())
        ebook_scan = scan_enabled and "ebook" in avail
        audiobook_scan = scan_enabled and "audiobook" in avail

    ebook_priority_list = settings.get("metadata_provider_priority") or []
    audiobook_priority_list = settings.get("metadata_audiobook_priority") or []
    ebook_enrich = (
        name in ebook_priority_list
        if ebook_priority_list else defaults["ebook_enrich"]
    )
    audiobook_enrich = (
        name in audiobook_priority_list
        if audiobook_priority_list else defaults["audiobook_enrich"]
    )

    rate_limit = _legacy_rate_for(name, settings, meta["default_rate"])

    entry: dict[str, Any] = {
        "rate_limit": float(rate_limit),
        "ebook_enrich": bool(ebook_enrich),
        "ebook_scan": bool(ebook_scan),
        "audiobook_enrich": bool(audiobook_enrich),
        "audiobook_scan": bool(audiobook_scan),
        "mandatory": bool(defaults.get("mandatory", False)),
    }
    # Preserve source-specific extras (e.g. Amazon's `format` /
    # `language` strings that drive the Author-Store filter API).
    # The 5 toggles + `mandatory` are explicitly built above; any
    # other key in defaults is a source-private config string that
    # should round-trip into metadata_sources unchanged.
    _STANDARD_KEYS = {
        "ebook_enrich", "ebook_scan",
        "audiobook_enrich", "audiobook_scan",
        "mandatory",
    }
    for k, v in defaults.items():
        if k not in _STANDARD_KEYS:
            entry[k] = v
    return entry


def _legacy_rate_for(
    name: str, settings: dict, default: float,
) -> float:
    """Read `rate_<name>` from legacy settings, with fallback."""
    key = f"rate_{name}"
    raw = settings.get(key)
    if raw is None:
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


# ─── Derivation (live reads) ──────────────────────────────────


def derive_enrich_priority(
    settings: dict, *, audiobook: bool,
) -> list[str]:
    """Ordered list of sources to run for enrichment.

    Reads `metadata_priority[content_type]` and filters to sources
    whose `*_enrich` toggle is True in `metadata_sources`. This is
    what `_build_metadata_enricher` consumes to construct the
    per-surface source list.
    """
    content_type = "audiobook" if audiobook else "ebook"
    surface = "audiobook_enrich" if audiobook else "ebook_enrich"
    return _filter_priority(settings, content_type, surface)


def derive_scan_priority(
    settings: dict, *, audiobook: bool,
) -> list[str]:
    """Ordered list of sources to run for discovery-side scanning."""
    content_type = "audiobook" if audiobook else "ebook"
    surface = "audiobook_scan" if audiobook else "ebook_scan"
    return _filter_priority(settings, content_type, surface)


def _filter_priority(
    settings: dict, content_type: str, surface: str,
) -> list[str]:
    priority = (settings.get("metadata_priority") or {}).get(content_type) or []
    sources = settings.get("metadata_sources") or {}
    out: list[str] = []
    for name in priority:
        entry = sources.get(name)
        if entry and entry.get(surface):
            out.append(name)
    return out


def get_source_rate_limit(settings: dict, name: str) -> float:
    """Rate limit (queries/sec) for a single source, with fallback."""
    entry = (settings.get("metadata_sources") or {}).get(name) or {}
    raw = entry.get("rate_limit")
    if raw is None:
        meta = KNOWN_SOURCES.get(name, {})
        return float(meta.get("default_rate", 1.0))
    try:
        return float(raw)
    except (TypeError, ValueError):
        meta = KNOWN_SOURCES.get(name, {})
        return float(meta.get("default_rate", 1.0))


def is_source_mandatory(settings: dict, name: str) -> bool:
    """Return True when the source-scan layer should keep doing
    DETAIL fetches on books missing this source's URL, even when
    other sources have already URL'd the book.

    Falls back to the v2.3.2 ship-with default for the source when
    the entry is missing the `mandatory` key — keeps upgraded
    settings.json files (pre-v2.3.2) behaving sensibly without
    requiring an explicit migration write. Sources unknown to the
    app default to False.
    """
    entry = (settings.get("metadata_sources") or {}).get(name) or {}
    raw = entry.get("mandatory")
    if raw is None:
        return bool(_DEFAULT_NEW_INSTALL_STATE.get(name, {}).get(
            "mandatory", False,
        ))
    return bool(raw)


# ─── Dual-write (keep legacy keys in sync) ────────────────────


def sync_legacy_keys(settings: dict) -> None:
    """Mirror MAM's rate_limit from the unified shape onto `rate_mam`.

    Phase 7 retired the per-source `*_enabled` bools, `rate_<name>`
    floats for every metadata source, and both `metadata_provider_priority`
    / `metadata_audiobook_priority` legacy keys — every consumer reads
    from `metadata_sources` + `metadata_priority` via the derivation
    helpers now.

    `rate_mam` survives intact because it has ~7 non-metadata-source
    call sites (the pipeline's MAM batch scan, schedulers, etc.) that
    weren't worth migrating for this pass. The Metadata Sources panel
    is still where the user edits MAM rate, so we keep the mirror
    here to avoid forcing a second editor UI for one number.
    """
    sources = settings.get("metadata_sources") or {}
    mam_entry = sources.get("mam") or {}
    if "rate_limit" in mam_entry:
        settings["rate_mam"] = float(mam_entry["rate_limit"])
