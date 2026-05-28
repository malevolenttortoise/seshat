"""
End-to-end tests for the cross-library matcher.

Builds two stub discovery DBs (one "ebook" slug, one "audiobook" slug),
seeds them with books that share or don't share a normalized
(author, title) pair, and runs `rebuild_matches`. Asserts on the
resulting `work_links` rows.

Fixtures monkeypatch `app.config.DATA_DIR` to tmp_path so the per-
library DB files land in isolation, then fully init schemas via
`app.discovery.database.init_db` for each library slug. The pipeline
DB (seshat.db) is handled by the `temp_db` fixture.
"""
from __future__ import annotations

import pytest

from app import state
from app.discovery import database as disco_db
from app.works import matcher, storage


async def _seed_book(
    slug: str, book_id: int, title: str, author: str,
) -> None:
    """Insert a book + author row into a library's discovery DB."""
    db = await disco_db.get_db(slug)
    try:
        # Insert (or no-op if) author.
        existing = await (await db.execute(
            "SELECT id FROM authors WHERE name = ?", (author,),
        )).fetchone()
        if existing:
            author_id = existing["id"]
        else:
            cur = await db.execute(
                "INSERT INTO authors (name, sort_name) VALUES (?, ?)",
                (author, author),
            )
            author_id = cur.lastrowid
        await db.execute(
            "INSERT INTO books (id, title, source, owned) "
            "VALUES (?, ?, 'calibre', 1)",
            (book_id, title),
        )
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
            "VALUES (?, ?, 0)", (book_id, author_id),
        )
        await db.commit()
    finally:
        await db.close()


@pytest.fixture
async def two_libraries(tmp_path, monkeypatch, temp_db):
    """Create a Calibre ebook library + an ABS audiobook library.

    Returns the `libraries` list shape that `state._discovered_libraries`
    expects. Tests mutate it then call `matcher.rebuild_matches(libs)`.
    """
    from app import config as app_config
    # Point per-library discovery DBs at tmp_path too.
    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)

    await disco_db.init_db("calibre-main")
    await disco_db.init_db("abs-audio")

    libs = [
        {"slug": "calibre-main", "content_type": "ebook", "app_type": "calibre"},
        {"slug": "abs-audio", "content_type": "audiobook", "app_type": "audiobookshelf"},
    ]
    monkeypatch.setattr(state, "_discovered_libraries", libs)
    yield libs


async def test_matches_identical_pairs(two_libraries):
    await _seed_book("calibre-main", 1, "The Way of Kings", "Brandon Sanderson")
    await _seed_book("abs-audio", 10, "The Way of Kings", "Brandon Sanderson")

    result = await matcher.rebuild_matches()
    assert result.works_created == 1
    assert result.links_added == 2

    # Both books belong to the same work.
    ebook_link = await storage.get_link("calibre-main", 1)
    audio_link = await storage.get_link("abs-audio", 10)
    assert ebook_link.work_id == audio_link.work_id
    assert ebook_link.content_type == "ebook"
    assert audio_link.content_type == "audiobook"


async def test_matches_despite_format_decoration(two_libraries):
    """Calibre's 'The X' and Audible's 'X (Unabridged)' should collapse."""
    await _seed_book("calibre-main", 1, "The Final Empire", "Brandon Sanderson")
    await _seed_book("abs-audio", 10, "Final Empire (Unabridged)", "Brandon Sanderson")

    result = await matcher.rebuild_matches()
    assert result.links_added == 2
    assert (await storage.get_link("calibre-main", 1)).work_id == \
           (await storage.get_link("abs-audio", 10)).work_id


async def test_singletons_no_links(two_libraries):
    """A book with no cross-library twin should not get a link row."""
    await _seed_book("calibre-main", 1, "Alone in Ebook", "Author A")
    await _seed_book("abs-audio", 10, "Alone in Audio", "Author A")

    result = await matcher.rebuild_matches()
    assert result.links_added == 0
    assert await storage.get_link("calibre-main", 1) is None
    assert await storage.get_link("abs-audio", 10) is None


async def test_rerun_is_idempotent(two_libraries):
    await _seed_book("calibre-main", 1, "The Way of Kings", "Brandon Sanderson")
    await _seed_book("abs-audio", 10, "The Way of Kings", "Brandon Sanderson")

    first = await matcher.rebuild_matches()
    assert first.links_added == 2

    second = await matcher.rebuild_matches()
    assert second.links_added == 0  # already linked
    assert second.works_created == 0


async def test_manual_links_never_overwritten(two_libraries):
    """A manual link survives a matcher rerun even against a different auto group."""
    # Seed Calibre ebook + ABS audiobook that auto-match on (author, title).
    await _seed_book("calibre-main", 1, "The Way of Kings", "Brandon Sanderson")
    await _seed_book("abs-audio", 10, "The Way of Kings", "Brandon Sanderson")

    # Manually link book 1 to a completely different work_id first.
    manual_work_id = storage.generate_work_id()
    await storage.create_link(
        work_id=manual_work_id, library_slug="calibre-main", book_id=1,
        content_type="ebook",
    )
    # Promote to manual (simulating the router POST /link).
    from app.database import get_db
    db = await get_db()
    try:
        await db.execute(
            "UPDATE work_links SET link_source = 'manual' "
            "WHERE library_slug = ? AND book_id = ?",
            ("calibre-main", 1),
        )
        await db.commit()
    finally:
        await db.close()

    # Matcher should respect the manual link — the bucket picks the
    # manual work_id as canonical and the ABS book joins that work_id.
    result = await matcher.rebuild_matches()
    assert result.links_skipped_manual == 0  # nothing stomped
    assert (await storage.get_link("calibre-main", 1)).work_id == manual_work_id
    assert (await storage.get_link("abs-audio", 10)).work_id == manual_work_id


async def test_orphan_prune_on_resync(two_libraries):
    """A book removed from its source library loses its link row.

    Each library needs ≥1 surviving book after the delete — the
    reconcile pass's "empty live list → skip" safety net treats a
    zero-book read as a transient error, not a deliberate wipe.
    """
    await _seed_book("calibre-main", 1, "Shared Work", "A. Author")
    await _seed_book("calibre-main", 2, "Keep Me", "B. Other")
    await _seed_book("abs-audio", 10, "Shared Work", "A. Author")
    await _seed_book("abs-audio", 11, "Solo Audio", "C. Third")
    await matcher.rebuild_matches()
    assert await storage.get_link("calibre-main", 1) is not None

    # Simulate Calibre sync pruning book 1 (book 2 survives).
    db = await disco_db.get_db("calibre-main")
    try:
        await db.execute("DELETE FROM books WHERE id = 1")
        await db.commit()
    finally:
        await db.close()

    result = await matcher.rebuild_matches()
    assert result.orphans_pruned >= 1
    assert await storage.get_link("calibre-main", 1) is None
    # The audiobook book's row survives with its original work_id — we
    # deliberately don't garbage-collect stranded singletons because the
    # ebook may be re-added later (e.g., user was swapping Calibre
    # libraries). `work_id` becomes a 1-member work; UI can filter
    # singletons at display time.
    surviving = await storage.get_link("abs-audio", 10)
    assert surviving is not None
    assert surviving.content_type == "audiobook"


async def test_single_library_skips(two_libraries, monkeypatch):
    """Fewer than 2 libraries → matcher is a no-op."""
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "calibre-main", "content_type": "ebook"},
    ])
    result = await matcher.rebuild_matches()
    assert result.works_created == 0
    assert result.links_added == 0


async def test_stale_auto_cleanup_on_rename(two_libraries):
    """Renaming one side of an auto-linked pair drops the stale link.

    The new bucket (post-rename) is a singleton on each side, so no
    new link is created — and the two old auto rows are cleaned up.
    """
    await _seed_book("calibre-main", 1, "The Way of Kings", "Brandon Sanderson")
    await _seed_book("abs-audio", 10, "The Way of Kings", "Brandon Sanderson")
    await matcher.rebuild_matches()

    # Simulate ABS's matcher renaming the audiobook to a different title.
    db = await disco_db.get_db("abs-audio")
    try:
        await db.execute(
            "UPDATE books SET title = 'Words of Radiance' WHERE id = 10"
        )
        await db.commit()
    finally:
        await db.close()

    result = await matcher.rebuild_matches()
    # Both auto rows should be cleaned up (2 members, 2 different keys,
    # no plurality → all autos dropped).
    assert result.stale_auto_removed == 2
    assert await storage.get_link("calibre-main", 1) is None
    assert await storage.get_link("abs-audio", 10) is None


async def test_stale_cleanup_preserves_manual(two_libraries):
    """Manual members set the canonical key — auto rows that drift get dropped."""
    await _seed_book("calibre-main", 1, "Dune", "Frank Herbert")
    await _seed_book("abs-audio", 10, "Dune", "Frank Herbert")
    await matcher.rebuild_matches()

    # Promote the Calibre link to manual — the user "locked in" this pair.
    from app.database import get_db as pipeline_get_db
    db = await pipeline_get_db()
    try:
        await db.execute(
            "UPDATE work_links SET link_source = 'manual' "
            "WHERE library_slug = ? AND book_id = ?",
            ("calibre-main", 1),
        )
        await db.commit()
    finally:
        await db.close()

    # Drift the ABS side — its auto link is now stale relative to the
    # manual side. Manual member is preserved; auto member is dropped.
    db = await disco_db.get_db("abs-audio")
    try:
        await db.execute(
            "UPDATE books SET title = 'Dune Messiah' WHERE id = 10"
        )
        await db.commit()
    finally:
        await db.close()

    result = await matcher.rebuild_matches()
    assert result.stale_auto_removed == 1
    assert (await storage.get_link("calibre-main", 1)).link_source == "manual"
    assert await storage.get_link("abs-audio", 10) is None


async def test_matches_via_loose_subtitle_variant(two_libraries):
    """Halo: Evolutions shape — Calibre has the publisher subtitle,
    Audible dropped it. The strict keys differ; the loose variant on
    the Calibre side collides with the ABS strict key and the pair
    should link via connected components."""
    await _seed_book(
        "calibre-main", 1,
        "Halo: Evolutions - Essential Tales of the Halo Universe",
        "Various",
    )
    await _seed_book("abs-audio", 10, "Halo: Evolutions", "Various")
    result = await matcher.rebuild_matches()
    assert result.links_added == 2
    ebook_link = await storage.get_link("calibre-main", 1)
    audio_link = await storage.get_link("abs-audio", 10)
    assert ebook_link is not None
    assert audio_link is not None
    assert ebook_link.work_id == audio_link.work_id


async def test_no_false_link_via_dash_separator_alone(two_libraries):
    """Two different books that happen to share a ' - Subtitle' prefix
    pattern must NOT collapse if their prefixes actually differ."""
    await _seed_book("calibre-main", 1, "Project Alpha - Part 1", "Same Author")
    await _seed_book("abs-audio", 10, "Project Beta - Part 1", "Same Author")
    result = await matcher.rebuild_matches()
    assert result.links_added == 0


async def test_stale_cleanup_plurality_wins(two_libraries, tmp_path, monkeypatch):
    """When 2 members still agree and 1 drifted, the drifted one gets dropped."""
    from app import config as app_config
    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    # Add a 3rd library so the work can have 3 members.
    await disco_db.init_db("calibre-second")
    libs = [
        {"slug": "calibre-main", "content_type": "ebook", "app_type": "calibre"},
        {"slug": "abs-audio", "content_type": "audiobook", "app_type": "audiobookshelf"},
        {"slug": "calibre-second", "content_type": "ebook", "app_type": "calibre"},
    ]
    monkeypatch.setattr(state, "_discovered_libraries", libs)

    await _seed_book("calibre-main", 1, "Dune", "Frank Herbert")
    await _seed_book("abs-audio", 10, "Dune", "Frank Herbert")
    await _seed_book("calibre-second", 5, "Dune", "Frank Herbert")
    await matcher.rebuild_matches()

    # Drift ONE member — the majority (2) still agree, so the drifted
    # one (1) is the minority and gets dropped.
    db = await disco_db.get_db("calibre-second")
    try:
        await db.execute(
            "UPDATE books SET title = 'Heretics of Dune' WHERE id = 5"
        )
        await db.commit()
    finally:
        await db.close()

    result = await matcher.rebuild_matches()
    assert result.stale_auto_removed == 1
    # The surviving pair remains linked.
    main_link = await storage.get_link("calibre-main", 1)
    abs_link = await storage.get_link("abs-audio", 10)
    assert main_link is not None
    assert abs_link is not None
    assert main_link.work_id == abs_link.work_id
    assert await storage.get_link("calibre-second", 5) is None
