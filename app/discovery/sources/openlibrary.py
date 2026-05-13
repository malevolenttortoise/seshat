"""
Open Library discovery source — sanctioned REST API.

Open Library publishes a free, no-key REST API at openlibrary.org.
Two-phase shape mirrors the v2.10.5 Hardcover pattern:

  1. Author lookup via `/search/authors.json?q={name}`. Returns a
     ranked list of author records with `key`, `name`, and
     `work_count`. Disambiguate by strict name match + work_count
     tiebreaker.
  2. Walk the author's works via `/authors/{key}/works.json`,
     paginated with `limit` + `offset`. One round-trip per page.

Open Library is data-quality dependent — coverage is good for
older / well-cataloged books, sparse for indie self-pub. We
accept everything OL returns and let downstream consensus
handle merge with richer sources (Hardcover, Goodreads-when-
available).

Series information: OL doesn't have a first-class series
concept. We attempt extraction from the work's title pattern
("Mistborn (Mistborn, #1)") and fall back to standalone. Most
series-rich books will get their series filled in by other
sources during the consensus merge.

Editions / ISBN / page count / language: deferred to v2.11.x.
Each work would require an additional `/works/{key}/editions.json`
fetch — N+1 cost would dominate scan time. The work-level
metadata (title, description, cover, first_publish_year) is
enough for discovery; richer sources backfill the rest.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Optional

from app.discovery.sources.base import (
    AuthorResult,
    BaseSource,
    BookResult,
    SeriesResult,
)

logger = logging.getLogger("seshat.discovery.openlibrary")

BASE = "https://openlibrary.org"
COVER_BASE = "https://covers.openlibrary.org/b/id"


# Series-from-title extractor — Open Library titles often carry
# series info in the title field as "(Series Name, #N)" or
# "Series Name, Book N". Best-effort; misses go to standalone.
_SERIES_RX = re.compile(
    r"\s*\((?P<name>[^,()#]+?)(?:,?\s*#?(?P<num>\d+(?:\.\d+)?))?\)\s*$"
)

# Common edition / format decorations that get parenthesized in OL
# titles and would otherwise be misread as a series name. Exact-
# lowercase lookup PLUS regexes for:
#   - Numbered editions: "2nd Edition", "10th Edition", "3rd Ed",
#     "1st Printing" etc.
#   - Generic " edition" / " ed." suffixes (catches the v2.11.0 UAT
#     bugs "Japanese Edition", "Numbered Edition", "Spanish Edition",
#     etc. — anything ending in "edition" that the exact list misses)
_EDITION_REJECT_EXACT = frozenset({
    "edition", "annotated", "illustrated", "unabridged", "abridged",
    "revised", "expanded", "deluxe edition", "anniversary edition",
    "collector's edition", "special edition", "hardcover", "paperback",
    "ebook", "audiobook", "audio", "kindle edition", "large print",
    "boxed set", "box set", "omnibus",
})
_EDITION_REJECT_RX = re.compile(
    r"(?:"
    r"^\d+(?:st|nd|rd|th)\s+(?:edition|ed\.?|printing)$"  # "2nd Edition"
    r"|"
    r"^.*\s+(?:edition|ed\.?|printing)$"                  # "Japanese Edition"
    r")",
    re.IGNORECASE,
)


# MARC → ISO 639-1 language map. Matches the per-book MetaSource shape
# in `app.metadata.sources.openlibrary`. Unknown codes pass through
# as None so we don't fabricate language labels for downstream filtering.
_OL_LANGUAGE_MAP = {
    "eng": "en",
    "spa": "es",
    "fre": "fr", "fra": "fr",
    "ger": "de", "deu": "de",
    "ita": "it",
    "jpn": "ja",
    "rus": "ru",
    "chi": "zh", "zho": "zh",
    "por": "pt",
    "dut": "nl", "nld": "nl",
    "kor": "ko",
    "pol": "pl",
    "swe": "sv",
    "tur": "tr",
    "ara": "ar",
}


def _extract_language(work: dict) -> Optional[str]:
    """Pick the first known language from an OL work entry.

    OL exposes work-level language as `[{"key": "/languages/eng"}, ...]`
    on the works.json response. Returns ISO 639-1 (2-letter) when
    mappable; None otherwise (downstream `_lang_ok` treats None as
    "unknown, let it through" — so populating this field is what
    actually drives the language filter for OL-sourced books).
    """
    codes = work.get("languages") or []
    for entry in codes:
        if isinstance(entry, dict):
            raw = entry.get("key") or entry.get("name") or ""
            raw = raw.rsplit("/", 1)[-1]
        elif isinstance(entry, str):
            raw = entry
        else:
            continue
        mapped = _OL_LANGUAGE_MAP.get(raw.lower())
        if mapped:
            return mapped
    return None


def _extract_series_from_title(title: str) -> tuple[Optional[str], Optional[float], str]:
    """Pull a trailing "(Series, #N)" suffix off a title.

    Returns (series_name, series_index, cleaned_title). When no
    pattern matches, returns (None, None, title) unchanged.
    """
    if not title:
        return None, None, title
    m = _SERIES_RX.search(title)
    if not m:
        return None, None, title
    name = m.group("name").strip()
    num_raw = m.group("num")
    try:
        idx = float(num_raw) if num_raw else None
    except (ValueError, TypeError):
        idx = None
    cleaned = title[:m.start()].strip()
    return name or None, idx, cleaned or title


def _norm_for_match(name: str) -> str:
    """Normalize an author name for the strict-match disambiguation gate.
    Lowercases, strips periods/spaces. Same shape as the v2.10.5
    Hardcover resolver."""
    return name.lower().replace(".", "").replace(" ", "")


# Regex helpers for the v2.11.0 punct+whitespace-strip resolver tier.
# OL's full-text search is whitespace-sensitive for initials: querying
# "K.D. Robertson" returns 0 hits, "K. D. Robertson" returns 2. Generate
# both forms so a single resolver call covers either input shape.
_RX_COMPACT_INITIALS = re.compile(r"\b([A-Z])\.([A-Z])\.")          # "K.D." → match groups for "K.", "D."
_RX_SPACED_INITIALS = re.compile(r"\b([A-Z])\.\s+([A-Z])\.")        # "K. D." → reverse


def _query_variants(author_name: str) -> list[str]:
    """Produce alternate OL-search query strings for `author_name`.

    Returns the input verbatim first, followed by any variant forms
    that differ. Currently:
      - Compact initials → spaced: "K.D. Robertson" → "K. D. Robertson"
      - Spaced initials → compact: "K. D. Robertson" → "K.D. Robertson"

    Empty input or single-word names short-circuit to `[author_name]`.
    """
    variants: list[str] = [author_name]
    if not author_name:
        return variants

    spaced = _RX_COMPACT_INITIALS.sub(r"\1. \2.", author_name)
    if spaced != author_name:
        variants.append(spaced)

    compact = _RX_SPACED_INITIALS.sub(r"\1.\2.", author_name)
    if compact != author_name and compact not in variants:
        variants.append(compact)

    return variants


def _has_cjk(s: str) -> bool:
    """True if `s` contains any CJK ideograph / hiragana / katakana.

    Used as the gate for the cross-script aggregation rule: OL's
    `/search/authors.json` for a romanized Japanese name (e.g.
    "Isuna Hasekura") frequently returns the canonical native-script
    record (e.g. 支倉凍砂, work_count=79) alongside lower-work-count
    English transliterations (work_count=5). When the CJK record
    dominates work count, it's almost always the canonical author
    and should be aggregated into the result set.
    """
    if not s:
        return False
    for ch in s:
        try:
            cat = unicodedata.name(ch, "")
        except ValueError:
            continue
        if cat.startswith(("CJK", "HIRAGANA", "KATAKANA", "HANGUL")):
            return True
    return False


class OpenLibrarySource(BaseSource):
    name = "openlibrary"
    default_headers = {
        "Accept": "application/json",
        "User-Agent": "Seshat/2.10 (https://github.com/malevolenttortoise/seshat)",
    }
    default_timeout = 30.0

    def __init__(self, rate_limit: float = 1.0):
        super().__init__(rate_limit=rate_limit)

    async def search_author(self, author_name: str) -> Optional[AuthorResult]:
        """Resolve `author_name` to OL author key(s) + walk their works.

        Mirrors the v2.10.5 Hardcover pattern: phase 1 finds canonical
        author record(s), phase 2 paginates each one's works relation.
        Returns a populated `AuthorResult` so the downstream `_try_source`
        fast path skips the redundant `get_author_books` call.

        v2.11.0: phase 1 may return multiple keys when OL has split an
        author across records (e.g. translated/CJK + Latin transcription).
        Works are aggregated across all matched records and deduplicated
        by work-key so the canonical record's full bibliography surfaces.
        """
        ol_keys = await self._resolve_author_keys(author_name)
        if not ol_keys:
            logger.info(
                "  OpenLibrary: no author match for '%s'", author_name,
            )
            return None

        seen_work_keys: set[str] = set()
        all_works: list[dict] = []
        for key in ol_keys:
            works = await self._fetch_all_author_works(key)
            for w in works:
                wkey = (w.get("key") or "").rsplit("/", 1)[-1]
                if not wkey:
                    continue
                if wkey in seen_work_keys:
                    continue
                seen_work_keys.add(wkey)
                all_works.append(w)

        primary_key = ol_keys[0]
        if not all_works:
            logger.info(
                "  OpenLibrary: author %s '%s' returned 0 works "
                "(aggregated across %d record(s))",
                primary_key, author_name, len(ol_keys),
            )
            return AuthorResult(
                name=author_name, external_id=primary_key,
            )

        if len(ol_keys) > 1:
            logger.info(
                "  OpenLibrary: author '%s' → %d works aggregated across "
                "%d records: %s",
                author_name, len(all_works), len(ol_keys),
                ", ".join(ol_keys),
            )
        else:
            logger.info(
                "  OpenLibrary: author %s '%s' → %d works",
                primary_key, author_name, len(all_works),
            )
        return self._build_result(author_name, primary_key, all_works)

    async def get_author_books(
        self, author_id: str, **_kw,
    ) -> Optional[AuthorResult]:
        """No-op — `search_author` already returns books in one shot.

        Same pattern as HardcoverSource; lookup.py's two-phase flow
        collapses to phase 1. Returning None here degrades cleanly
        on the unreachable slow path.
        """
        return None

    # ── Phase 1 — author lookup ───────────────────────────────────

    async def _resolve_author_keys(
        self, author_name: str
    ) -> list[str]:
        """Find OL author key(s) matching `author_name`.

        Returns a list (possibly empty). One key per matched OL author
        record. Most authors resolve to exactly one key; multiple keys
        signal that OL has split the same person across records (common
        for translated authors who exist as both a romanized record and
        a native-script record).

        Resolution chain (v2.11.0):

        1. **Variant queries** — try the input name verbatim; if OL
           returns 0 hits, retry with whitespace-stripped initials
           ("K.D." ↔ "K. D."). OL's full-text search is whitespace-
           sensitive on initials so this single query change recovers
           authors like K.D. Robertson that would otherwise FAIL.

        2. **Strict-name aggregation** — among the returned docs,
           include every record whose name strictly normalizes to the
           target (lowercase, periods/spaces stripped). Case variants
           like "Isuna Hasekura" / "ISUNA HASEKURA" / "Isuna HASEKURA"
           all aggregate together.

        3. **Cross-script aggregation** — also include any non-Latin
           script record (CJK / hiragana / katakana / hangul) whose
           work_count exceeds the strict-match maximum. OL routinely
           returns the canonical Japanese record for a romanized
           query, but the strict-name gate would otherwise discard it
           because "支倉凍砂" doesn't normalize to "isunahasekura".
           Work-count dominance is the signal that this is the same
           prolific author, not an unrelated entry.

        4. **Top-hit fallback** — if no record passes either gate,
           return OL's #1-ranked result wrapped in a single-element
           list. Preserves the v2.10.6 behavior for the long tail of
           noisy / partial matches.
        """
        docs = await self._search_authors(author_name)

        # Variant retry — OL's search is whitespace-sensitive on
        # initials. If the verbatim query missed, try alternate forms
        # before giving up.
        if not docs:
            for variant in _query_variants(author_name)[1:]:
                docs = await self._search_authors(variant)
                if docs:
                    logger.info(
                        "  OpenLibrary: '%s' returned 0 hits; "
                        "recovered via variant query '%s'",
                        author_name, variant,
                    )
                    break

        if not docs:
            return []

        target = _norm_for_match(author_name)
        strict: list[tuple[int, str, str]] = []      # (work_count, name, key)
        substring: list[tuple[int, str, str]] = []   # 50-score fallback pool
        others: list[tuple[int, str, str]] = []      # everything else, for cross-script

        for d in docs:
            key = d.get("key")
            name = d.get("name") or ""
            work_count = int(d.get("work_count") or 0)
            if not key or not name:
                continue
            normalized = _norm_for_match(name)
            if normalized == target:
                strict.append((work_count, name, key))
            elif target and (target in normalized or normalized in target):
                substring.append((work_count, name, key))
            else:
                others.append((work_count, name, key))

        if not strict and not substring:
            # No name-match at all — trust OL's ranker (single top hit).
            top = docs[0]
            top_key = top.get("key")
            if top_key:
                logger.info(
                    "  OpenLibrary: no strict name match for '%s', "
                    "falling back to top hit '%s' (key=%s)",
                    author_name, top.get("name"), top_key,
                )
                return [top_key]
            return []

        # Strict-name aggregation: include every record that strictly
        # matches. Order by work_count desc so the primary record (the
        # one whose bibliography is most likely complete) comes first.
        strict_sorted = sorted(strict, key=lambda t: t[0], reverse=True)
        keys: list[str] = [k for _, _, k in strict_sorted]

        # If no strict hits but we had substring matches, fall back to
        # the most prolific substring match (preserves v2.10.6 behavior).
        if not keys and substring:
            substring.sort(key=lambda t: t[0], reverse=True)
            keys = [substring[0][2]]

        # Cross-script aggregation. When the OL search has returned a
        # CJK-script record alongside the romanized strict matches and
        # the CJK record dominates work count, it's the canonical native-
        # script entry — almost always the same person. Include it.
        if strict_sorted:
            max_strict_wc = strict_sorted[0][0]
            for wc, name, key in others:
                if _has_cjk(name) and wc > max_strict_wc:
                    keys.append(key)
                    logger.info(
                        "  OpenLibrary: aggregating cross-script record "
                        "%s '%s' (work_count=%d > strict-max=%d) for '%s'",
                        key, name, wc, max_strict_wc, author_name,
                    )

        if len(strict_sorted) > 1:
            others_str = ", ".join(
                f"{n}({c})" for c, n, _ in strict_sorted[1:5]
            )
            logger.info(
                "  OpenLibrary: aggregated %d strict-match records for "
                "'%s'. Primary: %s. Also: %s",
                len(strict_sorted), author_name, strict_sorted[0][1],
                others_str,
            )

        return keys

    async def _search_authors(self, query: str) -> list[dict]:
        """Single call to /search/authors.json. Returns docs list or []."""
        try:
            resp = await self._get(
                f"{BASE}/search/authors.json",
                params={"q": query, "limit": 10},
            )
            data = resp.json()
        except Exception as e:
            logger.debug(
                "  OpenLibrary: author search error for %r: %s",
                query, e,
            )
            return []
        return data.get("docs") or []

    # ── Phase 2 — walk author's works ─────────────────────────────

    async def _fetch_all_author_works(self, author_key: str) -> list[dict]:
        """Page through `/authors/{key}/works.json` until exhausted.

        OL caps per-page at 1000 but defaults to 50. We use 100 to
        match the Hardcover pagination shape and keep response sizes
        sane for very prolific authors. Stops on the partial-page
        signal (last page returned fewer than `limit` items).
        """
        # Author key may come in as `/authors/OL26320A` or `OL26320A`;
        # normalize to the bare id portion for path construction.
        bare = author_key.rsplit("/", 1)[-1]
        PAGE_SIZE = 100
        MAX_PAGES = 30  # 3000-work ceiling — well above any single human
        all_works: list[dict] = []
        offset = 0
        for _ in range(MAX_PAGES):
            try:
                resp = await self._get(
                    f"{BASE}/authors/{bare}/works.json",
                    params={"limit": PAGE_SIZE, "offset": offset},
                )
                data = resp.json()
            except Exception as e:
                logger.debug(
                    "  OpenLibrary: works fetch error at offset=%d: %s",
                    offset, e,
                )
                break
            entries = data.get("entries") or []
            if not entries:
                break
            all_works.extend(entries)
            if len(entries) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return all_works

    # ── Result assembly ────────────────────────────────────────────

    def _build_result(
        self, author_name: str, ol_key: str, works: list[dict],
    ) -> AuthorResult:
        """Convert OL work entries into BookResult/SeriesResult shape."""
        series_map: dict[str, SeriesResult] = {}
        standalone: list[BookResult] = []

        for w in works:
            br = self._work_to_book_result(w)
            if br is None:
                continue
            if br.series_name:
                sm = series_map.setdefault(
                    br.series_name,
                    SeriesResult(name=br.series_name, books=[]),
                )
                sm.books.append(br)
            else:
                standalone.append(br)

        return AuthorResult(
            name=author_name,
            external_id=ol_key,
            books=standalone,
            series=list(series_map.values()),
        )

    def _work_to_book_result(self, work: dict) -> Optional[BookResult]:
        title_raw = (work.get("title") or "").strip()
        if not title_raw:
            return None

        # Series extraction from title — best-effort. Only consume
        # the parenthetical when it looks series-shaped (has a
        # name component); leave format / edition decorations alone.
        series_name, series_idx, cleaned_title = _extract_series_from_title(title_raw)
        if series_name:
            lname = series_name.lower().strip()
            if (
                lname in _EDITION_REJECT_EXACT
                or _EDITION_REJECT_RX.match(lname)
            ):
                series_name, series_idx = None, None
                cleaned_title = title_raw

        # Cover — OL's `covers` field is a list of cover_ids
        cover_url: Optional[str] = None
        covers = work.get("covers")
        if isinstance(covers, list) and covers:
            first = covers[0]
            if isinstance(first, int) and first > 0:
                cover_url = f"{COVER_BASE}/{first}-L.jpg"

        # Description — OL stores it as either a string or a
        # `{type, value}` dict.
        description: Optional[str] = None
        desc = work.get("description")
        if isinstance(desc, dict):
            description = desc.get("value")
        elif isinstance(desc, str):
            description = desc
        if description:
            description = description.strip() or None

        # Pub date — OL exposes `first_publish_date` (string) or
        # falls back to `created.value` (timestamp). Prefer the
        # former; degrade to None.
        pub_date = work.get("first_publish_date")
        if isinstance(pub_date, str):
            pub_date = pub_date.strip()
        elif pub_date is not None:
            pub_date = str(pub_date)

        # Work key for the source URL
        work_key = (work.get("key") or "").rsplit("/", 1)[-1]
        source_url = f"{BASE}/works/{work_key}" if work_key else None

        # v2.11.0: extract work-level language so lookup.py's `_lang_ok`
        # can actually filter non-English entries. Pre-fix, every OL
        # work landed with language=None, which `_lang_ok` treats as
        # "unknown, assume ok" → non-English entries bypassed the
        # user's language filter and polluted the review queue.
        language = _extract_language(work)

        return BookResult(
            title=cleaned_title,
            series_name=series_name,
            series_index=series_idx,
            cover_url=cover_url,
            description=description,
            pub_date=pub_date,
            language=language,
            external_id=work_key or None,
            source=self.name,
            source_url=source_url,
        )
