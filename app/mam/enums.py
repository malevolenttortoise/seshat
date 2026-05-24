"""
MAM enum fetcher.

Exposes the category, language, and format enumerations the UI needs
to populate its filter-editor dropdowns. Three layers, tried in order:

  1. **Runtime cache** — in-memory dict keyed by enum name, populated
     the first time an enum is fetched. Held for the process lifetime;
     cleared via `refresh()` when the user wants a fresh pull.
  2. **Live MAM fetch** — hits `categories.php` (and the language
     endpoint) through the same cookie-rotating client used by the
     rest of `app.mam`. Cached in the runtime dict on success.
  3. **Bundled fallback** — ships `app/mam/categories.json`, a known-
     good snapshot captured 2026-04 from `categories.php`. Used when
     no cookie is configured, or when MAM is unreachable.

Languages are NOT exposed via a MAM API as far as we know — MAM uses
a static list (English, Spanish, German, ...) that's baked into the
search form HTML. Hard-coding is fine; new languages would be a big
announcement.

Formats are derived from the category main_id → name map:

    AudioBooks → "audiobooks"
    E-Books    → "ebooks"
    Musicology → "musicology"
    Radio      → "radio"

which matches the prefix the filter's `extract_format` pulls out of
MAM category strings. The filter uses normalized form, so we
normalize here too.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.filter.normalize import normalize_category
from app.mam.cookie import _do_get

_log = logging.getLogger("seshat.mam.enums")

_BUNDLED_CATEGORIES_PATH = Path(__file__).parent / "categories.json"
_BUNDLED_V2_PATH = Path(__file__).parent / "categories_v2.json"

# Languages MAM's web search form accepts. Kept lowercase for
# direct comparison against `Announce.language`.
_STATIC_LANGUAGES: tuple[str, ...] = (
    "english",
    "afrikaans",
    "arabic",
    "bengali",
    "bosnian",
    "bulgarian",
    "burmese",
    "cantonese",
    "catalan",
    "chinese",
    "croatian",
    "czech",
    "danish",
    "dutch",
    "estonian",
    "finnish",
    "french",
    "german",
    "greek",
    "hebrew",
    "hindi",
    "hungarian",
    "icelandic",
    "indonesian",
    "italian",
    "japanese",
    "javanese",
    "korean",
    "latin",
    "latvian",
    "lithuanian",
    "malay",
    "malayalam",
    "marathi",
    "nepali",
    "norwegian",
    "persian",
    "polish",
    "portuguese",
    "punjabi",
    "romanian",
    "russian",
    "serbian",
    "slovak",
    "slovenian",
    "somali",
    "spanish",
    "swahili",
    "swedish",
    "tagalog",
    "tamil",
    "telugu",
    "thai",
    "turkish",
    "ukrainian",
    "urdu",
    "vietnamese",
    "welsh",
    "yiddish",
)


@dataclass(frozen=True)
class CategoryEntry:
    id: str
    name: str
    main_id: str
    main_name: str
    # Normalized "main subcategory" form the filter gate uses,
    # e.g. "ebooks fantasy" for E-Books → Fantasy.
    normalized: str


# ─── Runtime cache ──────────────────────────────────────────────


_cache: dict[str, object] = {}


def _clear_cache() -> None:
    _cache.clear()


async def get_categories(
    *, use_mam: bool = True, token: str = ""
) -> list[CategoryEntry]:
    """Return every MAM category flattened to a list of CategoryEntry.

    Uses the runtime cache if populated, otherwise tries a live MAM
    fetch (when `use_mam` is True AND `token` is set), then falls
    back to the bundled JSON snapshot. Always returns a non-empty
    list unless the bundled file is missing AND MAM is unreachable.
    """
    cached = _cache.get("categories")
    if cached is not None:
        return cached  # type: ignore[return-value]

    raw: Optional[list[dict]] = None
    if use_mam and token:
        try:
            raw = await _fetch_categories_from_mam(token)
        except Exception as e:
            _log.info("mam.enums: live fetch failed (%s); using bundled", e)

    if raw is None:
        raw = _load_bundled_categories()

    flat: list[CategoryEntry] = []
    for group in raw:
        main_id = str(group.get("main_id", ""))
        main_name = str(group.get("name", ""))
        for sub in group.get("categories", []):
            sub_name = str(sub.get("name", ""))
            flat.append(
                CategoryEntry(
                    id=str(sub.get("id", "")),
                    name=sub_name,
                    main_id=main_id,
                    main_name=main_name,
                    normalized=normalize_category(f"{main_name} - {sub_name}"),
                )
            )
    _cache["categories"] = flat
    return flat


def catname_for_id(cat_id: str | int) -> str | None:
    """Resolve a MAM subcategory numeric id to its `catname` form.

    Returns e.g. `"Ebooks - Fantasy"` for `"63"`. Returns `None` if
    the id isn't in the bundled snapshot. The returned string matches
    MAM's per-torrent `catname` field shape (lowercase 'b' in "Ebooks"),
    not the categories endpoint's "E-Books" hyphenated form — the
    backfill in `app/database.py::_backfill_numeric_grab_categories`
    relies on this normalization so cleaned rows match what new grabs
    produce.
    """
    target = str(cat_id).strip()
    if not target:
        return None
    for group in _load_bundled_categories():
        main_name = str(group.get("name", "")).replace("E-Books", "Ebooks")
        for sub in group.get("categories", []):
            if str(sub.get("id", "")) == target:
                sub_name = str(sub.get("name", "")).strip()
                return f"{main_name} - {sub_name}" if sub_name else main_name
    return None


def get_languages() -> list[str]:
    """Return the static MAM language list (lowercase)."""
    return list(_STATIC_LANGUAGES)


async def get_formats() -> list[str]:
    """Return the top-level format names derived from the category tree.

    These match the strings the filter's `extract_format` returns, so
    filter settings populated from this list will compare correctly
    against live announces.
    """
    cats = await get_categories()
    seen: dict[str, None] = {}
    for c in cats:
        # normalized main name: "E-Books" → "e books" → hmm.
        # The filter uses `extract_format` which normalizes the
        # whole category; for consistency we normalize the main
        # name the same way and take that.
        key = normalize_category(c.main_name)
        seen.setdefault(key, None)
    return list(seen.keys())


def get_v2_enums() -> dict:
    """Return the MAM v2 category system (bundled snapshot).

    V2 is the upcoming category overhaul (expected 2026): 8 media
    types instead of 4, Fiction/Nonfiction main split instead of
    AudioBooks/E-Books/Musicology/Radio, and 61 multi-select content
    tags replacing the old single-select subcategories.

    Returned shape:
      {"media_types": [...], "main_categories": [...],
       "content_tags": [...], "languages": [...]}
    """
    cached = _cache.get("v2_enums")
    if cached is not None:
        return cached  # type: ignore[return-value]
    try:
        with _BUNDLED_V2_PATH.open("rb") as f:
            data = json.load(f)
    except Exception:
        _log.exception("mam.enums: bundled categories_v2.json unreadable")
        data = {"media_types": [], "main_categories": [],
                "content_tags": [], "languages": []}
    _cache["v2_enums"] = data
    return data


async def refresh(token: str = "") -> int:
    """Force a fresh fetch from MAM.

    Returns the number of categories loaded after the refresh.
    Failures fall back to the bundled JSON.
    """
    _clear_cache()
    cats = await get_categories(use_mam=True, token=token)
    return len(cats)


# ─── Internal ──────────────────────────────────────────────────


def _load_bundled_categories() -> list[dict]:
    try:
        with _BUNDLED_CATEGORIES_PATH.open("rb") as f:
            return json.load(f)
    except Exception:
        _log.exception("mam.enums: bundled categories.json unreadable")
        return []


async def _fetch_categories_from_mam(token: str) -> list[dict]:
    """Hit categories.php and parse the JSON payload.

    MAM's categories.php returns raw JSON when the `json` query
    parameter is set. Response shape matches the bundled file.
    """
    url = "https://www.myanonamouse.net/categories.php?json=1"
    response = await _do_get(url, token=token)
    response.raise_for_status()
    return response.json()
