#!/usr/bin/env python3
"""
Validation harness for the metadata-source overhaul (Phase 0 of the
v2.11.0 plan; see project_seshat_metadata_overhaul.md).

Runs each enabled discovery source against a fixed set of 13
benchmark authors and reports books-surfaced, time taken, and
errors. Output is a Markdown table written to
docs/validation/<timestamp>.md plus stdout.

Not part of pytest — this is a developer tool, run manually:

    python scripts/validate_sources.py

Use it to:
  - Capture today's pre-overhaul baseline (with Goodreads in
    Cloudflare-202 state — that's the broken state we're fixing)
  - Re-run after each phase to measure delta
  - Surface coverage gaps the new defaults need to address

Authors picked 2026-05-13 across:
  - LitRPG / progression-fantasy
  - Indie self-pub with massive bibliographies
  - Traditional-pub mainstream
  - Pen-name duo (James S. A. Corey = Abraham + Franck)
  - Non-Latin name (Asato Asato — slug→name edge case)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Import lazily so the script can at least print its help text on a
# bare checkout without the seshat venv activated.
from app.discovery.sources.amazon import AmazonSource  # noqa: E402
from app.discovery.sources.goodreads import GoodreadsSource  # noqa: E402
from app.discovery.sources.google_books import GoogleBooksSource  # noqa: E402
from app.discovery.sources.hardcover import HardcoverSource  # noqa: E402
from app.discovery.sources.ibdb import IbdbSource  # noqa: E402
from app.discovery.sources.kobo import KoboSource  # noqa: E402
from app.discovery.sources.openlibrary import OpenLibrarySource  # noqa: E402


AUTHORS: list[str] = [
    "J. N. Chaney",
    "Marcus Sloss",
    "Sabaa Tahir",
    "James S. A. Corey",
    "Jim Butcher",
    "Brandon Sanderson",
    "William D. Arand",
    "Logan Jacobs",
    "Jon Messenger",
    "Karen Traviss",
    "Robyn Bee",
    "K.D. Robertson",
    "Asato Asato",
    "Isuna Hasekura",
]


@dataclass
class Result:
    source: str
    author: str
    found_id: Optional[str]
    book_count: int
    seconds: float
    error: Optional[str]


async def _run_one(source, author: str) -> Result:
    """Probe one author against one source. Never raises."""
    t0 = time.monotonic()
    try:
        sr = await source.search_author(author)
        if sr is None or not getattr(sr, "external_id", None):
            return Result(
                source=source.name,
                author=author,
                found_id=None,
                book_count=0,
                seconds=time.monotonic() - t0,
                error="search_author returned None",
            )
        # Some sources do all the heavy lifting in search_author and
        # already have books on the result; others need a second call.
        # Count books across BOTH the standalone `books` field AND every
        # `series[*].books` list — Hardcover puts all entries under
        # series when each book belongs to a series, so reading only
        # `books` under-counts (e.g., Jim Butcher: 0 standalone + 10
        # in-series = 10 actual, not 0).
        def _total(result):
            standalone = list(getattr(result, "books", []) or [])
            series_total = sum(
                len(getattr(s, "books", []) or [])
                for s in (getattr(result, "series", []) or [])
            )
            return len(standalone) + series_total

        book_count = _total(sr)
        if book_count == 0 and hasattr(source, "get_author_books"):
            br = await source.get_author_books(sr.external_id)
            if br is not None:
                book_count = _total(br)
        return Result(
            source=source.name,
            author=author,
            found_id=str(sr.external_id),
            book_count=book_count,
            seconds=time.monotonic() - t0,
            error=None if book_count > 0 else "search_author returned None",
        )
    except Exception as e:
        return Result(
            source=source.name,
            author=author,
            found_id=None,
            book_count=0,
            seconds=time.monotonic() - t0,
            error=f"{type(e).__name__}: {str(e)[:200]}",
        )


async def main(hardcover_api_key: str, google_books_api_key: str) -> int:
    sources = [
        GoodreadsSource(rate_limit=2.0),
        HardcoverSource(api_key=hardcover_api_key),
        OpenLibrarySource(rate_limit=1.5),
        KoboSource(rate_limit=1.5),
        AmazonSource(rate_limit=1.5),
        IbdbSource(rate_limit=1.5),
        GoogleBooksSource(rate_limit=1.5, api_key=google_books_api_key or None),
    ]

    # Per-source extra inter-author delay (beyond the source's own
    # rate_limit). Catches the 2026-05-12 Hardcover transient issue
    # where the GraphQL beta API throttled mid-batch after ~6 calls,
    # producing 8/14 false FAILs that didn't reproduce on one-shot
    # probes. Conservative defaults — only adds 1-2s per author.
    PER_SOURCE_EXTRA_DELAY = {
        "hardcover": 2.0,    # GraphQL beta throttling observed
        "google_books": 1.0, # transient 503s observed
    }

    print(f"validating {len(AUTHORS)} authors against {len(sources)} sources...")
    print()

    results: list[Result] = []
    for src in sources:
        print(f"  → {src.name}")
        extra_delay = PER_SOURCE_EXTRA_DELAY.get(src.name, 0.0)
        for author in AUTHORS:
            res = await _run_one(src, author)
            status = (
                f"{res.book_count:>4} books"
                if res.error is None
                else f"FAIL ({res.error[:60]})"
            )
            print(f"    {author:<24} {status:<60} ({res.seconds:.1f}s)")
            results.append(res)
            if extra_delay > 0:
                await asyncio.sleep(extra_delay)
        try:
            await src.close()
        except Exception:
            pass
        print()

    return _write_report(results)


def _write_report(results: list[Result]) -> int:
    out_dir = _REPO_ROOT / "docs" / "validation"
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_file = out_dir / f"sources-{timestamp}.md"

    # Build a pivot: author × source → "N books" or "FAIL"
    sources_in_order: list[str] = []
    for r in results:
        if r.source not in sources_in_order:
            sources_in_order.append(r.source)

    by_author: dict[str, dict[str, Result]] = {}
    for r in results:
        by_author.setdefault(r.author, {})[r.source] = r

    lines: list[str] = []
    lines.append(f"# Source validation — {timestamp}")
    lines.append("")
    lines.append(
        "Per-author book counts surfaced by each discovery source. "
        "Captured manually via `scripts/validate_sources.py`."
    )
    lines.append("")
    lines.append("| Author | " + " | ".join(sources_in_order) + " |")
    lines.append("|---|" + "|".join("---" for _ in sources_in_order) + "|")
    for author in AUTHORS:
        row: list[str] = [author]
        for src in sources_in_order:
            r = by_author.get(author, {}).get(src)
            if r is None:
                row.append("—")
            elif r.error:
                row.append(f"FAIL ({r.seconds:.1f}s)")
            else:
                row.append(f"{r.book_count} ({r.seconds:.1f}s)")
        lines.append("| " + " | ".join(row) + " |")

    lines.append("")
    lines.append("## Failures (full error strings)")
    lines.append("")
    failures = [r for r in results if r.error]
    if not failures:
        lines.append("None.")
    else:
        for r in failures:
            lines.append(f"- **{r.source} / {r.author}**: {r.error}")

    lines.append("")
    lines.append("## Raw results (JSON)")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(
        [r.__dict__ for r in results], indent=2, default=str,
    ))
    lines.append("```")

    out_file.write_text("\n".join(lines))
    print(f"\nreport: {out_file}")
    return 0


async def _load_keys_from_secrets(args) -> tuple[str, str]:
    """When running inside the Seshat container (or any env where the
    auth-secret is reachable), pull keys from the encrypted secrets
    store automatically. CLI args still win if explicitly passed."""
    hk = args.hardcover_key
    gk = args.google_books_key
    if not hk:
        try:
            from app.secrets import get_secret
            hk = (await get_secret("hardcover_api_key")) or ""
        except Exception:
            pass
    if not gk:
        try:
            from app.secrets import get_secret
            gk = (await get_secret("google_books_api_key")) or ""
        except Exception:
            pass
    return hk, gk


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--hardcover-key", default="", help="Hardcover API key (else read from secrets store)")
    p.add_argument("--google-books-key", default="", help="Google Books API key (skipped if empty)")
    args = p.parse_args()

    async def _entry():
        hk, gk = await _load_keys_from_secrets(args)
        return await main(hk, gk)

    sys.exit(asyncio.run(_entry()))
