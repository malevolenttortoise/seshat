"""
Legacy-DB upgrade smoke tests.

Catches the class of bug v2.7.0 shipped with: a CREATE INDEX in the
SCHEMA block referencing a column that legacy databases don't have
yet (because the column gets added by MIGRATIONS, which run AFTER
SCHEMA). The fresh-DB path passes because the SCHEMA's CREATE TABLE
adds the column before the index runs; the legacy-DB path crashes
because CREATE TABLE IF NOT EXISTS no-ops on the existing table and
the index then hits "no such column".

Every test in this file simulates a real legacy database with the
v2.6.x shape of the affected table, then runs `init_db()` to verify
the upgrade completes without raising and lands the expected schema.

When adding a new column to SCHEMA + an ALTER TABLE migration, also
add a new test here that pre-creates the v2.N-1 shape of that table
and asserts init_db() lands without error.
"""
import aiosqlite
import pytest

from app.database import get_db, init_db, SCHEMA


@pytest.fixture
async def legacy_db_path(tmp_path, monkeypatch):
    """Pre-create a SQLite DB at the v2.6.1 schema of book_review_queue
    (no bundle_* columns, no library_slug, no bundle_parent_grab_id).
    Returns the path. The conftest temp_db fixture isn't reused because
    it runs `init_db()` itself — we want to control the pre-init shape.
    """
    from app import config, database

    db_path = tmp_path / "seshat-legacy.db"
    monkeypatch.setattr(config, "APP_DB_PATH", db_path)
    monkeypatch.setattr(database, "APP_DB_PATH", db_path)

    # Seed the DB with v2.6.1's book_review_queue shape. Other tables
    # are irrelevant to the regression but we create the bare minimum
    # (grabs + pipeline_runs) so the FK references in book_review_queue
    # don't trip future tests that exercise inserts.
    async with aiosqlite.connect(str(db_path)) as db:
        await db.executescript("""
            CREATE TABLE announces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seen_at TEXT, raw TEXT, torrent_id TEXT, torrent_name TEXT,
                category TEXT, author_blob TEXT, decision TEXT,
                decision_reason TEXT, matched_author TEXT
            );
            CREATE TABLE grabs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                announce_id INTEGER, mam_torrent_id TEXT NOT NULL,
                torrent_name TEXT NOT NULL, category TEXT, author_blob TEXT,
                torrent_file_path TEXT, qbit_hash TEXT, state TEXT NOT NULL,
                state_updated_at TEXT, grabbed_at TEXT, submitted_at TEXT,
                completed_at TEXT, failed_reason TEXT,
                failed_with_cookie_id INTEGER
            );
            CREATE TABLE pipeline_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                grab_id INTEGER NOT NULL, qbit_hash TEXT, source_path TEXT,
                staged_path TEXT, book_filename TEXT, book_format TEXT,
                metadata_title TEXT, metadata_author TEXT, metadata_series TEXT,
                metadata_language TEXT, sink_name TEXT, sink_result TEXT,
                state TEXT NOT NULL, state_updated_at TEXT, started_at TEXT,
                completed_at TEXT, error TEXT
            );
            -- v2.6.1 shape: no bundle_* columns, no library_slug, no
            -- bundle_parent_grab_id. This is the table that crashed
            -- v2.7.0 startup on Mark's container.
            CREATE TABLE book_review_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                grab_id INTEGER NOT NULL,
                pipeline_run_id INTEGER,
                staged_path TEXT NOT NULL,
                book_filename TEXT NOT NULL,
                book_format TEXT,
                metadata_json TEXT NOT NULL,
                cover_path TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                decided_at TEXT,
                decision_note TEXT
            );
        """)
        # Stamp user_version at the migration count just before the
        # first v2.7.0 bundle migration so every v2.7.0+ migration
        # runs as a fresh upgrade. The pre-v2.7 migration count
        # (12) is stable — appending future v2.N migrations leaves
        # this index unchanged.
        _PRE_V27_MIGRATION_COUNT = 12
        await db.execute(f"PRAGMA user_version = {_PRE_V27_MIGRATION_COUNT}")
        # Insert one pending review row so the backfill UPDATE has
        # something to touch.
        await db.execute(
            "INSERT INTO grabs (mam_torrent_id, torrent_name, state) "
            "VALUES ('100', 'Legacy Book', 'downloaded')"
        )
        await db.execute(
            "INSERT INTO book_review_queue "
            "(grab_id, staged_path, book_filename, metadata_json) "
            "VALUES (1, '/tmp/legacy', 'legacy.epub', '{}')"
        )
        await db.commit()

    return db_path


class TestLegacyDbUpgrade:
    async def test_init_db_survives_v26_to_v27_upgrade(self, legacy_db_path):
        """The v2.7.0 regression: SCHEMA's
        `CREATE INDEX idx_review_queue_bundle_group ON ...(bundle_group_id)`
        ran before the migration that added the bundle_group_id column.
        On a fresh DB the CREATE TABLE in SCHEMA added the column first
        so the index worked; on a legacy DB CREATE TABLE IF NOT EXISTS
        no-op'd and the index crashed with `no such column`.

        Fix: bundle-group index lives in MIGRATIONS only, not SCHEMA.
        """
        # This is the moment-of-truth call — if the regression returns,
        # this raises `sqlite3.OperationalError: no such column`.
        await init_db()

        db = await get_db()
        try:
            # Bundle columns present on the upgraded table.
            cursor = await db.execute("PRAGMA table_info(book_review_queue)")
            cols = {row[1] for row in await cursor.fetchall()}
            assert "bundle_group_id" in cols
            assert "bundle_index" in cols
            assert "bundle_total" in cols
            assert "library_slug" in cols
            assert "bundle_parent_grab_id" in cols

            # v2.8.0 reingest column on grabs.
            cursor = await db.execute("PRAGMA table_info(grabs)")
            grab_cols = {row[1] for row in await cursor.fetchall()}
            assert "is_reingest" in grab_cols

            # Bundle-group index present.
            cursor = await db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name='idx_review_queue_bundle_group'"
            )
            assert (await cursor.fetchone()) is not None

            # Legacy row was backfilled by the UPDATE migration.
            cursor = await db.execute(
                "SELECT bundle_group_id, bundle_total, bundle_index "
                "FROM book_review_queue WHERE book_filename = 'legacy.epub'"
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "grab-1"
            assert row[1] == 1
            assert row[2] == 0
        finally:
            await db.close()

    async def test_schema_indexes_reference_declared_columns(self):
        """Lint guard: every column referenced by a CREATE INDEX
        inside SCHEMA must be declared in the same SCHEMA block's
        CREATE TABLE for that table.

        Note: this is a weaker check than the runtime upgrade test
        above. It catches typo'd column names in SCHEMA indexes
        (`CREATE INDEX … ON books(authr_id)`), but NOT the v2.7.0
        regression itself — where bundle_group_id IS declared in
        SCHEMA's CREATE TABLE but legacy DBs don't have it yet
        because CREATE TABLE IF NOT EXISTS no-ops on existing tables.
        The runtime upgrade test (above) is what guards against that
        regression class. Keep both — they catch different failure
        modes of "schema-level index references a missing column".
        """
        import re

        # Build the set of (table, column) pairs declared in SCHEMA's
        # CREATE TABLE blocks. Permissive parse — accepts the typical
        # `CREATE TABLE [IF NOT EXISTS] name ( ... )` shape used in
        # this file.
        table_columns: dict[str, set[str]] = {}
        for m in re.finditer(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\);",
            SCHEMA, re.DOTALL | re.IGNORECASE,
        ):
            table = m.group(1)
            body = m.group(2)
            cols: set[str] = set()
            for line in body.splitlines():
                line = line.strip().rstrip(",")
                if not line or line.startswith("--"):
                    continue
                # Skip table-level clauses (UNIQUE/FOREIGN/PRIMARY KEY).
                if line.upper().startswith((
                    "UNIQUE", "FOREIGN", "PRIMARY", "CHECK", "CONSTRAINT",
                )):
                    continue
                tokens = line.split(None, 1)
                if not tokens:
                    continue
                cols.add(tokens[0])
            table_columns[table] = cols

        # Walk every CREATE INDEX statement in SCHEMA and assert each
        # referenced column was declared above.
        for m in re.finditer(
            r"CREATE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?\w+\s+ON\s+(\w+)\s*\(([^)]+)\)",
            SCHEMA, re.IGNORECASE,
        ):
            table = m.group(1)
            cols_in_index = [
                c.strip().split()[0] for c in m.group(2).split(",")
            ]
            for col in cols_in_index:
                assert col in table_columns.get(table, set()), (
                    f"SCHEMA CREATE INDEX on {table}({col}) references a "
                    f"column not declared in SCHEMA's CREATE TABLE. Move "
                    f"the index to MIGRATIONS (after the ALTER TABLE that "
                    f"adds the column) to keep legacy-DB upgrades safe."
                )
