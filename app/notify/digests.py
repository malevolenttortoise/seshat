"""
Daily and weekly digest jobs.

Sends summary notifications via ntfy on a schedule. All queries read
from the existing Tier 2 tables populated by the dispatcher and the
pipeline — this module is pure read-side.

Three daily digests (all fire at `daily_digest_hour` local time):

  1. **Accepted books** — grabs that entered the queue or review
     queue in the last 24h
  2. **Tentative captured** — tentative_torrents rows added in the
     last 24h
  3. **Ignored summary** — count of ignored_torrents_seen rows in
     the last 24h, plus the unique author count

One weekly digest (Sundays at 23:30):
  - authors moved to allowed (source=tentative_promote / auto_train /
    coauthor_train / tentative_approve in the last 7 days)
  - authors moved to ignored (source=tentative_auto_ignore / manual)
  - total books added to Calibre in the last 7 days + sample titles
  - also promotes stale authors_tentative_review entries (7+ days
    undecided) to the ignored list, per user decision #9

Every job is idempotent and side-effect-free aside from sending the
notification and (in the weekly job) performing the scheduled
author promotions. APScheduler at_most_one-instance guarding is
handled at the scheduler level — we trust the scheduler not to
fire two copies concurrently.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import aiosqlite

from app.database import get_db
from app.notifications import bus, events

_log = logging.getLogger("seshat.notify.digests")


@dataclass(frozen=True)
class DigestContext:
    """Everything a digest job needs to run.

    Pulled out of DispatcherDeps so the scheduler can build it once
    at startup without dragging the full dispatcher singleton into
    the notify layer.
    """

    ntfy_url: str
    ntfy_topic: str
    weekly_auto_promote_days: int = 7
    # v1.1: Calibre library path — required by the weekly audit job
    # so it can shell out to `calibredb list` against the user's
    # library. Empty string disables the audit.
    calibre_library_path: str = ""


# ─── Daily digest #1: accepted books ────────────────────────────


async def daily_accepted(ctx: DigestContext) -> bool:
    """Send the 24h accepted-books summary."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """
            SELECT torrent_name, author_blob, category
            FROM grabs
            WHERE grabbed_at >= datetime('now', '-24 hours')
              AND state IN ('pending_queue','fetched','submitted',
                            'downloading','downloaded','processing','complete')
            ORDER BY grabbed_at DESC
            LIMIT 20
            """
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    count = len(rows)
    if count == 0:
        return await bus.emit(
            events.DIGEST_DAILY_ACCEPTED,
            title="Daily digest — no new books",
            message="No books were accepted in the last 24 hours.",
        )

    sample = "\n".join(
        f"• {r['torrent_name']} — {r['author_blob']}" for r in rows[:10]
    )
    extra = f"\n… and {count - 10} more" if count > 10 else ""
    return await bus.emit(
        events.DIGEST_DAILY_ACCEPTED,
        title=f"Daily digest — {count} book(s) accepted",
        message=f"{sample}{extra}",
        tags=["books", "white_check_mark"],
    )


# ─── Daily digest #2: tentative torrents ────────────────────────


async def daily_tentative(ctx: DigestContext) -> bool:
    from app.storage import tentative as tentative_storage

    db = await get_db()
    try:
        rows = await tentative_storage.list_tentative_since(db, hours=24)
    finally:
        await db.close()

    count = len(rows)
    if count == 0:
        return True  # no-notification path; nothing interesting

    sample = "\n".join(
        f"• {r.torrent_name} — {r.author_blob}" for r in rows[:10]
    )
    extra = f"\n… and {count - 10} more" if count > 10 else ""
    return await bus.emit(
        events.DIGEST_DAILY_TENTATIVE,
        title=f"Tentative review queue — {count} new",
        message=(
            f"{sample}{extra}\n\n"
            "Review at /tentative to approve or reject."
        ),
    )


# ─── Daily digest #3: ignored summary ───────────────────────────


async def daily_ignored(ctx: DigestContext) -> bool:
    from app.storage import tentative as tentative_storage

    db = await get_db()
    try:
        rows = await tentative_storage.list_ignored_seen_since(db, hours=24)
    finally:
        await db.close()

    count = len(rows)
    if count == 0:
        return True

    # Count unique authors (normalized-ish) and per-author torrent counts.
    per_author: dict[str, int] = {}
    for r in rows:
        per_author[r.author_blob] = per_author.get(r.author_blob, 0) + 1

    unique_count = len(per_author)
    top = sorted(per_author.items(), key=lambda kv: kv[1], reverse=True)[:5]
    top_lines = "\n".join(f"• {name} ({cnt})" for name, cnt in top)

    return await bus.emit(
        events.DIGEST_DAILY_IGNORED,
        title=f"Ignored summary — {count} torrents, {unique_count} authors",
        message=(
            f"Most-frequent ignored authors (last 24h):\n{top_lines}"
        ),
        tags=["no_entry"],
    )


async def run_daily(ctx: DigestContext) -> None:
    """Fire all three daily digests back-to-back."""
    for fn, name in (
        (daily_accepted, "daily_accepted"),
        (daily_tentative, "daily_tentative"),
        (daily_ignored, "daily_ignored"),
    ):
        try:
            await fn(ctx)
        except Exception:
            _log.exception("digest %s failed (non-fatal)", name)


# ─── Weekly digest ──────────────────────────────────────────────


async def _author_moves_since(
    db: aiosqlite.Connection, days: int
) -> tuple[list[str], list[str]]:
    """Return (added_to_allowed, added_to_ignored) in the last N days."""
    cursor = await db.execute(
        """
        SELECT name FROM authors_allowed
        WHERE added_at >= datetime('now', ?)
          AND source IN ('auto_train','coauthor_train',
                         'tentative_promote','tentative_approve')
        ORDER BY added_at DESC
        """,
        (f"-{int(days)} days",),
    )
    allowed = [str(r["name"]) for r in await cursor.fetchall()]

    cursor = await db.execute(
        """
        SELECT name FROM authors_ignored
        WHERE added_at >= datetime('now', ?)
        ORDER BY added_at DESC
        """,
        (f"-{int(days)} days",),
    )
    ignored = [str(r["name"]) for r in await cursor.fetchall()]
    return allowed, ignored


async def _auto_promote_stale_tentative(
    db: aiosqlite.Connection, days: int
) -> int:
    """Promote tentative-review authors past their grace window to ignored.

    Returns the count of authors moved.
    """
    cursor = await db.execute(
        """
        SELECT name FROM authors_tentative_review
        WHERE added_at <= datetime('now', ?)
        """,
        (f"-{int(days)} days",),
    )
    stale_rows = await cursor.fetchall()
    if not stale_rows:
        return 0

    from app.storage import authors as authors_storage
    moved = 0
    for row in stale_rows:
        name = str(row["name"])
        try:
            await authors_storage.promote_tentative_to_ignored(db, name)
            moved += 1
        except Exception:
            _log.exception("weekly: promote %r failed", name)
    # Refresh the dispatcher's filter_config if anything moved so
    # the newly-ignored authors take effect on the next announce.
    if moved > 0:
        try:
            from app import state
            await state.refresh_filter_authors()
        except Exception:
            _log.debug("weekly: filter-config refresh failed (non-fatal)", exc_info=True)
    return moved


async def run_weekly(ctx: DigestContext) -> bool:
    from app.storage import calibre_adds as calibre_adds_storage

    db = await get_db()
    try:
        allowed_new, ignored_new = await _author_moves_since(db, days=7)
        promoted = await _auto_promote_stale_tentative(
            db, days=ctx.weekly_auto_promote_days
        )
        additions = await calibre_adds_storage.list_since(db, hours=7 * 24, limit=500)
    finally:
        await db.close()

    additions_count = len(additions)
    sample_titles = [a.title for a in additions[:10] if a.title]

    lines = [
        f"Books added to Calibre: {additions_count}",
        f"Authors added to allowed: {len(allowed_new)}",
        f"Authors added to ignored: {len(ignored_new)}",
    ]
    if promoted:
        lines.append(f"Auto-promoted (stale tentative → ignored): {promoted}")
    if sample_titles:
        lines.append("")
        lines.append("Recent additions:")
        lines.extend(f"• {t}" for t in sample_titles[:5])

    return await bus.emit(
        events.DIGEST_WEEKLY,
        title="Weekly digest",
        message="\n".join(lines),
    )


# ─── Weekly Calibre audit ───────────────────────────────────────


async def run_calibre_audit(ctx: DigestContext) -> bool:
    """Weekly audit — compare Calibre's library against Seshat's
    `calibre_additions` over the last 7 days, flag anything that
    entered Calibre outside of Seshat's knowledge.

    Uses `calibredb list --for-machine` so we don't have to parse
    metadata.db directly (whose schema can change between Calibre
    releases). The `--for-machine` flag returns JSON; we filter
    client-side to the last 7 days.

    Skipped entirely when `ctx.calibre_library_path` is empty —
    logged once at scheduler startup, not per-fire.
    """
    import asyncio as _asyncio
    import json as _json
    from datetime import datetime, timedelta

    if not ctx.calibre_library_path:
        return True  # disabled; not an error

    try:
        proc = await _asyncio.create_subprocess_exec(
            "calibredb", "list",
            "--library-path", ctx.calibre_library_path,
            "--for-machine",
            "--fields", "id,title,authors,timestamp",
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
        )
        stdout, stderr = await _asyncio.wait_for(proc.communicate(), timeout=60)
    except (FileNotFoundError, _asyncio.TimeoutError) as e:
        _log.warning("calibre_audit: calibredb unavailable or timed out: %s", e)
        return False
    except Exception:
        _log.exception("calibre_audit: calibredb invocation failed")
        return False

    if proc.returncode != 0:
        from app.sinks.calibre import (
            _detect_runtime_lib_failure,
            _format_runtime_lib_diagnostic,
        )
        err = stderr.decode("utf-8", errors="replace")
        if _detect_runtime_lib_failure(err):
            _log.error("%s", _format_runtime_lib_diagnostic(err, action="list"))
        else:
            _log.warning(
                "calibre_audit: calibredb list returned %d: %s",
                proc.returncode, err[:200],
            )
        return False

    try:
        calibre_books = _json.loads(stdout.decode("utf-8", errors="replace"))
    except ValueError:
        _log.warning("calibre_audit: calibredb list output was not valid JSON")
        return False

    # Filter to the last 7 days. calibredb's `timestamp` field is
    # the Calibre add-time (not the file mtime). Iso-8601-ish format.
    since = datetime.now() - timedelta(days=7)
    recent_calibre: list[dict] = []
    for book in calibre_books:
        ts_str = book.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00").split("+")[0])
        except (ValueError, AttributeError):
            continue
        if ts >= since:
            recent_calibre.append(book)

    # What Seshat added in the same window.
    db = await get_db()
    try:
        cursor = await db.execute(
            """
            SELECT title FROM calibre_additions
            WHERE added_at >= datetime('now', '-7 days')
            """
        )
        seshat_titles = {str(r["title"] or "").strip().lower() for r in await cursor.fetchall()}
    finally:
        await db.close()

    # Anything in `recent_calibre` whose title isn't in seshat_titles
    # = added outside Seshat (manual add, other tool, etc.).
    outside_seshat: list[str] = []
    for book in recent_calibre:
        title = str(book.get("title", "") or "").strip()
        if title and title.lower() not in seshat_titles:
            outside_seshat.append(title)

    calibre_count = len(calibre_books)
    recent_total = len(recent_calibre)
    outside_count = len(outside_seshat)

    # No notification if everything was Seshat-originated (the common
    # case for steady-state users). Sends only when there's something
    # to report.
    if outside_count == 0:
        _log.info(
            "calibre_audit: library=%d, last-7d=%d, all via Seshat — no notification",
            calibre_count, recent_total,
        )
        return True

    lines = [
        f"Calibre library: {calibre_count} book(s) total",
        f"Added in the last 7 days: {recent_total}",
        f"Added outside Seshat: {outside_count}",
    ]
    if outside_seshat:
        lines.append("")
        lines.append("Recent non-Seshat additions:")
        lines.extend(f"• {t}" for t in outside_seshat[:10])
        if outside_count > 10:
            lines.append(f"  …and {outside_count - 10} more")

    # Calibre audit shares the weekly cadence — route through the
    # weekly digest event so users see one toggle covering both.
    return await bus.emit(
        events.DIGEST_WEEKLY,
        title=f"Calibre audit — {outside_count} non-Seshat addition(s)",
        message="\n".join(lines),
        tags=["books", "mag"],
    )
