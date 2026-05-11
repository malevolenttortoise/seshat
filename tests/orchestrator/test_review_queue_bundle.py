"""
Tests for the v2.7.0 bundle-aware columns on `book_review_queue`.

Covers:
  - Default-shape inserts get bundle_total=1 / bundle_index=0
    and bundle_group_id auto-defaults to `grab-<id>`
  - Bundle children inserts carry through their bundle fields
  - Query ordering (created_at, bundle_group_id, bundle_index)
    keeps bundle siblings adjacent
  - Legacy-row backfill: the migration's UPDATE step backfills
    bundle_group_id for any row inserted before the column existed

Migration sanity (fresh DB → SCHEMA has the columns) is implicitly
covered by every other test in the file passing.
"""
from app.database import get_db
from app.storage import grabs as grabs_storage
from app.storage import review_queue as review_storage


async def _make_grab(db, mam_torrent_id: str = "100", torrent_name: str = "T") -> int:
    return await grabs_storage.create_grab(
        db, announce_id=None, mam_torrent_id=mam_torrent_id,
        torrent_name=torrent_name, category="ebooks fantasy",
        author_blob="Some Author", state=grabs_storage.STATE_DOWNLOADED,
    )


class TestDefaultShape:
    async def test_create_entry_defaults_to_single_book(self, temp_db):
        db = await get_db()
        try:
            grab_id = await _make_grab(db)
            entry_id = await review_storage.create_entry(
                db,
                grab_id=grab_id,
                pipeline_run_id=None,
                staged_path="/tmp/test",
                book_filename="book.epub",
                book_format="epub",
                metadata={"title": "Book", "author": "Author"},
            )
            row = await review_storage.get_entry(db, entry_id)
            assert row is not None
            assert row.bundle_total == 1
            assert row.bundle_index == 0
            # Default group id is the deterministic per-grab value.
            assert row.bundle_group_id == f"grab-{grab_id}"
            assert row.bundle_parent_grab_id is None
            assert row.library_slug is None
        finally:
            await db.close()


class TestBundleShape:
    async def test_bundle_children_round_trip(self, temp_db):
        db = await get_db()
        try:
            grab_id = await _make_grab(db)
            group_id = f"grab-{grab_id}"
            ids = []
            for i in range(3):
                entry_id = await review_storage.create_entry(
                    db,
                    grab_id=grab_id,
                    pipeline_run_id=None,
                    staged_path=f"/tmp/test/group-{i}",
                    book_filename=f"book-{i}.epub",
                    book_format="epub",
                    metadata={"title": f"Book {i}", "author": "Author"},
                    bundle_group_id=group_id,
                    bundle_index=i,
                    bundle_total=3,
                    library_slug="calibre-library",
                    bundle_parent_grab_id=grab_id,
                )
                ids.append(entry_id)

            # Each child round-trips with its bundle fields.
            for i, entry_id in enumerate(ids):
                row = await review_storage.get_entry(db, entry_id)
                assert row.bundle_total == 3
                assert row.bundle_index == i
                assert row.bundle_group_id == group_id
                assert row.library_slug == "calibre-library"
                assert row.bundle_parent_grab_id == grab_id
        finally:
            await db.close()

    async def test_list_pending_keeps_bundle_siblings_adjacent(self, temp_db):
        """When multiple bundles + single-book grabs coexist in the
        queue, sibling rows from the same bundle must come out
        adjacent and in bundle_index order so the UI can render
        them as one card."""
        db = await get_db()
        try:
            # Bundle A (3 children)
            grab_a = await _make_grab(db, mam_torrent_id="A", torrent_name="A")
            for i in range(3):
                await review_storage.create_entry(
                    db,
                    grab_id=grab_a,
                    pipeline_run_id=None,
                    staged_path=f"/tmp/A/group-{i}",
                    book_filename=f"a-{i}.epub",
                    book_format="epub",
                    metadata={"title": f"A book {i}"},
                    bundle_group_id=f"grab-{grab_a}",
                    bundle_index=i,
                    bundle_total=3,
                )

            # A single-book grab after.
            grab_b = await _make_grab(db, mam_torrent_id="B", torrent_name="B")
            await review_storage.create_entry(
                db,
                grab_id=grab_b,
                pipeline_run_id=None,
                staged_path="/tmp/B",
                book_filename="b.epub",
                book_format="epub",
                metadata={"title": "B"},
            )

            pending = await review_storage.list_pending(db)
            assert len(pending) == 4
            # First three rows are bundle A in bundle_index order.
            assert [r.bundle_group_id for r in pending[:3]] == [f"grab-{grab_a}"] * 3
            assert [r.bundle_index for r in pending[:3]] == [0, 1, 2]
            # Last row is the single B grab.
            assert pending[3].grab_id == grab_b
            assert pending[3].bundle_total == 1
        finally:
            await db.close()


class TestLegacyBackfill:
    async def test_pre_existing_rows_get_default_bundle_group_id(self, temp_db):
        """Simulate a legacy row that was inserted before the bundle
        columns existed. The migration's UPDATE step backfills
        bundle_group_id from grab_id; bundle_total/index already have
        DEFAULT 1/0 from the ALTER TABLE statements."""
        db = await get_db()
        try:
            grab_id = await _make_grab(db)
            # Manually NULL out bundle_group_id to simulate the pre-
            # migration state, then re-run the backfill.
            await db.execute(
                """
                INSERT INTO book_review_queue
                    (grab_id, pipeline_run_id, staged_path,
                     book_filename, book_format, metadata_json,
                     cover_path, status, bundle_group_id)
                VALUES (?, NULL, ?, ?, ?, '{}', NULL, 'pending', NULL)
                """,
                (grab_id, "/tmp/legacy", "legacy.epub", "epub"),
            )
            await db.commit()
            # Run the migration's backfill UPDATE.
            await db.execute(
                "UPDATE book_review_queue "
                "SET bundle_group_id = 'grab-' || grab_id "
                "WHERE bundle_group_id IS NULL"
            )
            await db.commit()

            cursor = await db.execute(
                "SELECT bundle_group_id, bundle_total, bundle_index "
                "FROM book_review_queue WHERE grab_id = ?",
                (grab_id,),
            )
            row = await cursor.fetchone()
            assert row["bundle_group_id"] == f"grab-{grab_id}"
            assert row["bundle_total"] == 1
            assert row["bundle_index"] == 0
        finally:
            await db.close()
