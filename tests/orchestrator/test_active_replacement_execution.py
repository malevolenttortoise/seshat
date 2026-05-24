"""
Tests for Phase 5b's `enact_opportunity` + `restore_enactment` orchestrators.

The orchestrators wire together: opportunity-row lookup, gate re-check,
on-disk path resolution, soft-delete move, sink call, audit row, and
status flip. We stub `_resolve_owned_book_dir` + `_select_sink_for_library`
in most tests so we exercise the orchestrator's control flow without
needing real Calibre / ABS / CWA installations — the sink tests + the
dev-stack UAT cover those integration paths separately.

Path-overlap + opt-in gates are validated independently in
`test_active_replacement_safety.py`; here we just confirm the
orchestrator hits the gate at re-enact time and short-circuits with
status='blocked' when it fails.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import database
from app.orchestrator import active_replacement as ar
from app.quality import enactments, opportunities
from app.sinks.base import SinkResult


# ─── Fixtures ────────────────────────────────────────────────


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "APP_DB_PATH", tmp_path / "test.db")
    await database.init_db()
    conn = await database.get_db()
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def calibre_library(tmp_path):
    """A library dict + matching on-disk root with a book directory.

    Returns (library_dict, library_root_path, book_dir_path).
    """
    root = tmp_path / "lib"
    book_dir = root / "Author" / "Title (123)"
    book_dir.mkdir(parents=True)
    (book_dir / "Title - Author.epub").write_bytes(b"x" * 100)
    lib = {
        "slug": "calibre-lib",
        "name": "Test Calibre",
        "app_type": "calibre",
        "content_type": "ebook",
        "library_path": str(root),
    }
    return lib, str(root), str(book_dir)


@pytest.fixture
def settings_allowing(calibre_library):
    """Settings with the master gate + safety set up for the test
    library, so the orchestrator's re-check at step 2 passes."""
    lib, root, _ = calibre_library
    return {
        "active_replacement_enabled_by_slug": {lib["slug"]: True},
        "active_replacement_auto_enact_by_slug": {},
        # qBit prefix nowhere near the library → safety classifies SAFE.
        "local_path_prefix": "/some/unrelated/downloads",
    }


async def _seed_opp(db, **overrides) -> int:
    defaults = dict(
        candidate_grab_id=101,
        candidate_mam_torrent_id="9001",
        candidate_format="epub",
        candidate_score=(0, 0, 0),
        owned_library_slug="calibre-lib",
        owned_book_id=42,
        owned_mam_torrent_id="5000",
        owned_format="epub",
        owned_score=(0, 2, 0),
        media_type="ebook",
    )
    defaults.update(overrides)
    await opportunities.record_opportunity(db, **defaults)
    await db.commit()
    cur = await db.execute(
        "SELECT id FROM replacement_opportunities ORDER BY id DESC LIMIT 1",
    )
    row = await cur.fetchone()
    return int(row[0])


def _stub_resolve(monkeypatch, book_dir, owned_row=None):
    """Patch the orchestrator's path resolver to return a known path."""
    async def fake(library, owned_book_id, settings):
        return book_dir, owned_row or {
            "id": owned_book_id, "calibre_id": 123,
            "audiobookshelf_id": None, "mam_torrent_id": "5000",
            "formats": "epub", "title": "Title",
        }, None
    monkeypatch.setattr(ar, "_resolve_owned_book_dir", fake)


def _stub_sink(monkeypatch, *, kind="calibre", remove_result=None, deliver_result=None):
    """Patch `_select_sink_for_library` to return a stub sink with
    canned remove/deliver behavior. Returns the call log so tests
    can assert against it."""
    calls: list[tuple] = []

    class StubSink:
        name = kind
        async def remove(self, **kw):
            calls.append(("remove", kw))
            return remove_result or SinkResult(
                success=True, sink_name=kind, detail="stub-removed",
            )
        async def deliver(self, path, metadata):
            calls.append(("deliver", path))
            return deliver_result or SinkResult(
                success=True, sink_name=kind, detail="stub-delivered",
            )

    def fake_select(library, settings):
        return kind, StubSink(), None

    monkeypatch.setattr(ar, "_select_sink_for_library", fake_select)
    return calls


# ─── enact_opportunity ───────────────────────────────────────


class TestEnactHappyPath:
    async def test_calibre_enact_moves_book_dir_and_flips_status(
        self, db, monkeypatch, calibre_library, settings_allowing,
    ):
        lib, root, book_dir = calibre_library
        _stub_resolve(monkeypatch, book_dir)
        sink_calls = _stub_sink(monkeypatch, kind="calibre")

        opp_id = await _seed_opp(db, owned_book_id=42)
        result = await ar.enact_opportunity(
            db, opp_id, acted_by="user",
            settings=settings_allowing, libraries=[lib],
        )
        await db.commit()

        assert result.status == "enacted", result
        assert result.enactment_id is not None
        # Original book dir is gone; .seshat-replaced/<ts>/Title (123)
        # holds the data.
        assert not Path(book_dir).exists()
        moved = list(Path(root, ".seshat-replaced").glob("*/Title (123)"))
        assert len(moved) == 1
        # Sink got called with calibre_book_id=123.
        assert any(
            c[0] == "remove" and c[1].get("calibre_book_id") == 123
            for c in sink_calls
        ), sink_calls

        # Opportunity status flipped.
        opp = await opportunities.get_opportunity(db, opp_id)
        assert opp["status"] == "enacted"
        assert opp["acted_by"] == "user"

        # Audit row points at both paths + has size.
        row = await enactments.get_enactment(db, result.enactment_id)
        assert row["owned_path_before"] == book_dir
        assert row["owned_path_after"].startswith(
            str(Path(root) / ".seshat-replaced"),
        )
        assert row["owned_size_bytes"] is not None
        assert row["failed_at"] is None

    async def test_abs_enact_uses_path_call(
        self, db, monkeypatch, tmp_path,
    ):
        """ABSSink.remove takes the original path; the file is already
        moved at that point, so the sink's idempotent 'no path'
        branch + scan trigger does the reconciliation."""
        root = tmp_path / "abs-lib"
        book_dir = root / "Author" / "Title"
        book_dir.mkdir(parents=True)
        (book_dir / "a.m4b").write_bytes(b"x")
        lib = {
            "slug": "abs-lib", "name": "ABS",
            "app_type": "audiobookshelf", "content_type": "audiobook",
            "library_path": str(root),
        }
        settings = {
            "active_replacement_enabled_by_slug": {lib["slug"]: True},
            "local_path_prefix": "/elsewhere",
        }
        _stub_resolve(monkeypatch, str(book_dir), owned_row={
            "id": 7, "calibre_id": None, "audiobookshelf_id": "abs-uuid",
            "mam_torrent_id": None, "formats": "m4b", "title": "T",
        })
        sink_calls = _stub_sink(monkeypatch, kind="abs")

        opp_id = await _seed_opp(
            db, owned_library_slug="abs-lib", owned_book_id=7,
            media_type="audiobook", candidate_format="m4b",
        )
        result = await ar.enact_opportunity(
            db, opp_id, acted_by="auto",
            settings=settings, libraries=[lib],
        )
        await db.commit()

        assert result.status == "enacted"
        # ABS sink gets the original path as its anchor (sink uses
        # the path-doesn't-exist branch since we already moved it).
        assert any(
            c[0] == "remove" and c[1].get("path") == str(book_dir)
            for c in sink_calls
        ), sink_calls


class TestEnactBlocked:
    async def test_blocks_when_opportunity_not_detected(
        self, db, monkeypatch, calibre_library, settings_allowing,
    ):
        lib, _, book_dir = calibre_library
        _stub_resolve(monkeypatch, book_dir)
        _stub_sink(monkeypatch)

        opp_id = await _seed_opp(db)
        # Flip to dismissed first.
        await opportunities.update_status(
            db, opp_id, status="dismissed", acted_by="user",
        )
        await db.commit()

        result = await ar.enact_opportunity(
            db, opp_id, settings=settings_allowing, libraries=[lib],
        )
        assert result.status == "blocked"
        assert "dismissed" in result.detail

    async def test_blocks_when_master_gate_off(
        self, db, monkeypatch, calibre_library,
    ):
        lib, _, book_dir = calibre_library
        _stub_resolve(monkeypatch, book_dir)
        _stub_sink(monkeypatch)
        # Master gate explicitly false.
        settings = {
            "active_replacement_enabled_by_slug": {lib["slug"]: False},
            "local_path_prefix": "/elsewhere",
        }
        opp_id = await _seed_opp(db)
        result = await ar.enact_opportunity(
            db, opp_id, settings=settings, libraries=[lib],
        )
        assert result.status == "blocked"
        assert "gate" in result.detail.lower()

    async def test_blocks_when_library_overlaps_qbit_path(
        self, db, monkeypatch, calibre_library,
    ):
        lib, root, book_dir = calibre_library
        _stub_resolve(monkeypatch, book_dir)
        _stub_sink(monkeypatch)
        # qBit prefix = library_path → OVERLAP → master gate False.
        settings = {
            "active_replacement_enabled_by_slug": {lib["slug"]: True},
            "local_path_prefix": root,
        }
        opp_id = await _seed_opp(db)
        result = await ar.enact_opportunity(
            db, opp_id, settings=settings, libraries=[lib],
        )
        assert result.status == "blocked"

    async def test_not_found_when_opportunity_missing(
        self, db, monkeypatch, calibre_library, settings_allowing,
    ):
        lib, _, book_dir = calibre_library
        _stub_resolve(monkeypatch, book_dir)
        _stub_sink(monkeypatch)
        result = await ar.enact_opportunity(
            db, 9999, settings=settings_allowing, libraries=[lib],
        )
        assert result.status == "not_found"

    async def test_no_sink_when_select_returns_error(
        self, db, monkeypatch, calibre_library, settings_allowing,
    ):
        lib, _, book_dir = calibre_library
        _stub_resolve(monkeypatch, book_dir)

        def fake_select(library, settings):
            return "", None, "no calibredb, no CWA"
        monkeypatch.setattr(ar, "_select_sink_for_library", fake_select)

        opp_id = await _seed_opp(db)
        result = await ar.enact_opportunity(
            db, opp_id, settings=settings_allowing, libraries=[lib],
        )
        assert result.status == "no_sink"
        # No soft-delete happened (sink check is before the move).
        assert Path(book_dir).exists()
        # No audit row written.
        rows = await enactments.list_enactments(db, opportunity_id=opp_id)
        assert rows == []

    async def test_not_found_when_owned_dir_missing(
        self, db, monkeypatch, calibre_library, settings_allowing,
    ):
        lib, _, _ = calibre_library
        _stub_resolve(monkeypatch, "/nope/does/not/exist")
        _stub_sink(monkeypatch)
        opp_id = await _seed_opp(db)
        result = await ar.enact_opportunity(
            db, opp_id, settings=settings_allowing, libraries=[lib],
        )
        assert result.status == "not_found"
        # Opportunity NOT flipped — operator can re-detect / dismiss.
        opp = await opportunities.get_opportunity(db, opp_id)
        assert opp["status"] == "detected"


class TestEnactRollback:
    async def test_sink_failure_rolls_back_soft_delete(
        self, db, monkeypatch, calibre_library, settings_allowing,
    ):
        lib, root, book_dir = calibre_library
        _stub_resolve(monkeypatch, book_dir)
        sink_calls = _stub_sink(
            monkeypatch, kind="calibre",
            remove_result=SinkResult(
                success=False, sink_name="calibre",
                error="exit 2: Calibre library locked",
            ),
        )

        opp_id = await _seed_opp(db)
        result = await ar.enact_opportunity(
            db, opp_id, acted_by="user",
            settings=settings_allowing, libraries=[lib],
        )
        await db.commit()

        assert result.status == "failed", result
        # File is back where it started.
        assert Path(book_dir).exists()
        # Soft-delete folder is empty (file moved back out).
        soft = Path(root) / ".seshat-replaced"
        leftover = [p for p in soft.rglob("*") if p.is_file()]
        assert leftover == []

        # Opportunity stays 'detected' for retry.
        opp = await opportunities.get_opportunity(db, opp_id)
        assert opp["status"] == "detected"

        # Audit row stamped with failed_at + failed_reason.
        row = await enactments.get_enactment(db, result.enactment_id)
        assert row["failed_at"] is not None
        assert "exit 2" in (row["failed_reason"] or "")

    async def test_cwa_path_with_missing_calibre_id_fails_cleanly(
        self, db, monkeypatch, calibre_library, settings_allowing,
    ):
        """The CWA admin API needs calibre_book_id. If the owned row
        doesn't carry one (pre-Seshat books that never got the
        calibre_sync backfill), the enact should fail BEFORE
        attempting any HTTP call and roll back the soft-delete."""
        lib, _, book_dir = calibre_library
        _stub_resolve(monkeypatch, book_dir, owned_row={
            "id": 42, "calibre_id": None,
            "audiobookshelf_id": None, "mam_torrent_id": "5000",
            "formats": "epub", "title": "T",
        })
        # Force CWA selection.
        sink_calls = _stub_sink(monkeypatch, kind="cwa")

        opp_id = await _seed_opp(db)
        result = await ar.enact_opportunity(
            db, opp_id, acted_by="user",
            settings=settings_allowing, libraries=[lib],
        )
        await db.commit()
        assert result.status == "failed", result
        # File moved back.
        assert Path(book_dir).exists()
        # Sink was NEVER called (we short-circuit on missing calibre_id).
        assert not any(c[0] == "remove" for c in sink_calls), sink_calls


# ─── restore_enactment ───────────────────────────────────────


class TestRestore:
    async def test_restores_dir_and_flips_opportunity_back(
        self, db, monkeypatch, calibre_library, settings_allowing,
    ):
        lib, root, book_dir = calibre_library
        _stub_resolve(monkeypatch, book_dir)
        _stub_sink(monkeypatch, kind="calibre")

        # Run an enact first.
        opp_id = await _seed_opp(db)
        enact_result = await ar.enact_opportunity(
            db, opp_id, acted_by="user",
            settings=settings_allowing, libraries=[lib],
        )
        await db.commit()
        assert enact_result.status == "enacted"
        assert not Path(book_dir).exists()

        # Now restore.
        restore_result = await ar.restore_enactment(
            db, enact_result.enactment_id, restored_by="user",
            settings=settings_allowing, libraries=[lib],
        )
        await db.commit()
        assert restore_result.status == "restored", restore_result

        # File back at original.
        assert Path(book_dir).exists()
        # Opportunity flipped back to 'detected'.
        opp = await opportunities.get_opportunity(db, opp_id)
        assert opp["status"] == "detected"
        # Audit row stamped with restored_at.
        row = await enactments.get_enactment(db, enact_result.enactment_id)
        assert row["restored_at"] is not None
        assert row["restored_by"] == "user"

    async def test_blocks_restore_when_enactment_already_restored(
        self, db, monkeypatch, calibre_library, settings_allowing,
    ):
        lib, _, book_dir = calibre_library
        _stub_resolve(monkeypatch, book_dir)
        _stub_sink(monkeypatch, kind="calibre")

        opp_id = await _seed_opp(db)
        e = await ar.enact_opportunity(
            db, opp_id, settings=settings_allowing, libraries=[lib],
        )
        await db.commit()
        await ar.restore_enactment(
            db, e.enactment_id, settings=settings_allowing, libraries=[lib],
        )
        await db.commit()

        # Second restore should block.
        result = await ar.restore_enactment(
            db, e.enactment_id, settings=settings_allowing, libraries=[lib],
        )
        assert result.status == "blocked"
        assert "already restored" in result.detail

    async def test_blocks_restore_of_failed_enactment(
        self, db, monkeypatch, calibre_library, settings_allowing,
    ):
        lib, _, book_dir = calibre_library
        _stub_resolve(monkeypatch, book_dir)
        # First enact fails — soft-delete rolled back.
        _stub_sink(
            monkeypatch, kind="calibre",
            remove_result=SinkResult(
                success=False, sink_name="calibre", error="boom",
            ),
        )
        opp_id = await _seed_opp(db)
        e = await ar.enact_opportunity(
            db, opp_id, settings=settings_allowing, libraries=[lib],
        )
        await db.commit()
        assert e.status == "failed"

        # Restore of a failed enactment should refuse (file is already
        # at original location; nothing to restore).
        result = await ar.restore_enactment(
            db, e.enactment_id, settings=settings_allowing, libraries=[lib],
        )
        assert result.status == "blocked"
        assert "failed" in result.detail.lower()

    async def test_not_found_when_soft_delete_purged(
        self, db, monkeypatch, calibre_library, settings_allowing, tmp_path,
    ):
        lib, _, book_dir = calibre_library
        _stub_resolve(monkeypatch, book_dir)
        _stub_sink(monkeypatch, kind="calibre")

        opp_id = await _seed_opp(db)
        e = await ar.enact_opportunity(
            db, opp_id, settings=settings_allowing, libraries=[lib],
        )
        await db.commit()
        # Simulate retention sweeper having purged the soft-delete file.
        import shutil
        row = await enactments.get_enactment(db, e.enactment_id)
        shutil.rmtree(row["owned_path_after"], ignore_errors=True)

        result = await ar.restore_enactment(
            db, e.enactment_id, settings=settings_allowing, libraries=[lib],
        )
        assert result.status == "not_found"
        assert "purged" in result.detail or "gone" in result.detail

    async def test_blocks_when_original_path_now_occupied(
        self, db, monkeypatch, calibre_library, settings_allowing,
    ):
        """If someone manually re-added a book at the original path
        between enact and restore, restoring would overwrite it.
        We refuse + tell the operator to reconcile manually."""
        lib, _, book_dir = calibre_library
        _stub_resolve(monkeypatch, book_dir)
        _stub_sink(monkeypatch, kind="calibre")

        opp_id = await _seed_opp(db)
        e = await ar.enact_opportunity(
            db, opp_id, settings=settings_allowing, libraries=[lib],
        )
        await db.commit()
        # Recreate book_dir while soft-delete is still in place.
        Path(book_dir).mkdir(parents=True, exist_ok=True)
        (Path(book_dir) / "manual.epub").write_bytes(b"new")

        result = await ar.restore_enactment(
            db, e.enactment_id, settings=settings_allowing, libraries=[lib],
        )
        assert result.status == "blocked"
        assert "occupied" in result.detail


# ─── Phase 5b Phase 6 — retention sweeper ────────────────────


import time as _time


def _make_soft_delete(library_root: Path, age_days: float, label: str = "book") -> Path:
    """Create <library>/.seshat-replaced/<ts>/<label>/file with `ts`
    set to (now - age_days). Returns the timestamp subdir path."""
    soft_root = library_root / ".seshat-replaced"
    soft_root.mkdir(parents=True, exist_ok=True)
    when = _time.localtime(_time.time() - age_days * 86400)
    ts = _time.strftime("%Y%m%d-%H%M%S", when)
    sub = soft_root / ts
    # If the test creates two with the same second, suffix uniquely.
    counter = 0
    while sub.exists():
        counter += 1
        sub = soft_root / f"{ts}-{counter:02d}"
    sub.mkdir(parents=True)
    inner = sub / label
    inner.mkdir()
    (inner / "data.txt").write_bytes(b"x" * 50)
    return sub


class TestPurgeExpiredSoftDeletes:
    """v2.27.0 Phase 6 — filesystem-only sweeper that purges
    <library>/.seshat-replaced/<ts>/ subdirs older than the configured
    retention window. Idempotent + handles malformed names + survives
    missing .seshat-replaced/ folders."""

    def test_purges_old_keeps_new(self, tmp_path):
        root = tmp_path / "lib"
        lib = {
            "slug": "lib-1", "name": "Lib 1",
            "app_type": "calibre", "library_path": str(root),
        }
        old = _make_soft_delete(root, age_days=40, label="old_book")
        new = _make_soft_delete(root, age_days=5, label="new_book")

        result = ar.purge_expired_soft_deletes(
            settings={"active_replacement_soft_delete_retention_days": 30},
            libraries=[lib],
        )
        assert result["purged"] == 1
        assert result["kept"] == 1
        assert not old.exists()
        assert new.exists()
        # Per-library stats present.
        per_lib = result["per_library"]
        assert len(per_lib) == 1
        assert per_lib[0]["slug"] == "lib-1"
        assert per_lib[0]["purged"] == 1
        assert per_lib[0]["kept"] == 1

    def test_idempotent_second_run_is_no_op(self, tmp_path):
        """Re-running after steady state should purge nothing."""
        root = tmp_path / "lib"
        lib = {"slug": "lib", "library_path": str(root), "app_type": "calibre"}
        _make_soft_delete(root, age_days=5, label="fresh")
        # First sweep — purges nothing (only fresh entries).
        first = ar.purge_expired_soft_deletes(
            settings={"active_replacement_soft_delete_retention_days": 30},
            libraries=[lib],
        )
        # Second sweep — same state.
        second = ar.purge_expired_soft_deletes(
            settings={"active_replacement_soft_delete_retention_days": 30},
            libraries=[lib],
        )
        assert first["purged"] == 0
        assert first["kept"] == 1
        assert second["purged"] == 0
        assert second["kept"] == 1

    def test_malformed_dir_names_flagged_not_purged(self, tmp_path):
        """A folder that doesn't parse as YYYYMMDD-HHMMSS must NOT
        be purged — we conservatively skip anything we don't
        understand. Operator-created sibling dirs survive sweeps."""
        root = tmp_path / "lib"
        lib = {"slug": "lib", "library_path": str(root), "app_type": "calibre"}
        soft_root = root / ".seshat-replaced"
        soft_root.mkdir(parents=True)
        rando = soft_root / "operator-manual-folder"
        rando.mkdir()
        (rando / "important.txt").write_bytes(b"keep me")
        _make_soft_delete(root, age_days=40, label="will_be_purged")

        result = ar.purge_expired_soft_deletes(
            settings={"active_replacement_soft_delete_retention_days": 30},
            libraries=[lib],
        )
        assert result["malformed"] == 1
        assert result["purged"] == 1
        assert rando.exists()
        assert (rando / "important.txt").exists()

    def test_missing_seshat_replaced_dir_is_no_op(self, tmp_path):
        """Library with no `.seshat-replaced/` folder should not error
        — common pre-Phase-5b case + after a clean purge."""
        root = tmp_path / "lib-no-folder"
        root.mkdir()
        lib = {"slug": "lib", "library_path": str(root), "app_type": "calibre"}
        result = ar.purge_expired_soft_deletes(
            settings={"active_replacement_soft_delete_retention_days": 30},
            libraries=[lib],
        )
        assert result["purged"] == 0
        assert result["kept"] == 0

    def test_skips_stray_files_in_soft_root(self, tmp_path):
        """A stray FILE (not dir) inside .seshat-replaced/ shouldn't
        get rmtree'd or counted — we only target timestamp subdirs."""
        root = tmp_path / "lib"
        lib = {"slug": "lib", "library_path": str(root), "app_type": "calibre"}
        soft_root = root / ".seshat-replaced"
        soft_root.mkdir(parents=True)
        stray = soft_root / "operator-notes.txt"
        stray.write_bytes(b"don't touch")

        result = ar.purge_expired_soft_deletes(
            settings={"active_replacement_soft_delete_retention_days": 30},
            libraries=[lib],
        )
        assert result["purged"] == 0
        assert result["kept"] == 0
        assert result["malformed"] == 0
        assert stray.exists()

    def test_multi_library_aggregation(self, tmp_path):
        """Stats aggregate across all libraries; per-library breakdown
        is preserved so the operator can see which library had the
        most expired entries."""
        a_root = tmp_path / "lib-a"
        b_root = tmp_path / "lib-b"
        libs = [
            {"slug": "a", "library_path": str(a_root), "app_type": "calibre"},
            {"slug": "b", "library_path": str(b_root), "app_type": "audiobookshelf"},
        ]
        # Vary ages by a few minutes so each call lands in its own
        # second and gets a unique parseable timestamp dir name.
        _make_soft_delete(a_root, age_days=40)
        _make_soft_delete(a_root, age_days=41)
        _make_soft_delete(a_root, age_days=5)
        _make_soft_delete(b_root, age_days=100)

        result = ar.purge_expired_soft_deletes(
            settings={"active_replacement_soft_delete_retention_days": 30},
            libraries=libs,
        )
        assert result["purged"] == 3
        assert result["kept"] == 1
        per_lib = {p["slug"]: p for p in result["per_library"]}
        assert per_lib["a"]["purged"] == 2
        assert per_lib["a"]["kept"] == 1
        assert per_lib["b"]["purged"] == 1
        assert per_lib["b"]["kept"] == 0

    def test_zero_retention_falls_back_to_default(self, tmp_path):
        """Defensive: settings.json hand-edited to 0 days shouldn't
        purge everything immediately. The function clamps to a
        sensible default."""
        root = tmp_path / "lib"
        lib = {"slug": "lib", "library_path": str(root), "app_type": "calibre"}
        _make_soft_delete(root, age_days=5, label="fresh")

        result = ar.purge_expired_soft_deletes(
            settings={"active_replacement_soft_delete_retention_days": 0},
            libraries=[lib],
        )
        # 5-day-old entry should NOT be purged (defaults to 30).
        assert result["purged"] == 0
        assert result["kept"] == 1

    def test_libraries_without_library_path_skipped(self, tmp_path):
        """Library entry without `library_path` (mis-configured discovery)
        shouldn't crash the sweep — it just gets skipped."""
        libs = [
            {"slug": "no-path", "library_path": "", "app_type": "calibre"},
        ]
        result = ar.purge_expired_soft_deletes(
            settings={"active_replacement_soft_delete_retention_days": 30},
            libraries=libs,
        )
        assert result["purged"] == 0
        # The empty-path library doesn't even produce a per-library
        # entry (the function continues before building one).
        assert result["per_library"] == []


class TestParseSoftDeleteTimestamp:
    def test_valid_timestamp_parses(self):
        ts = ar._parse_soft_delete_timestamp("20260524-180000")
        assert ts is not None
        # Should be close to noon-ish on the actual date (local TZ),
        # well into the 2026 range.
        assert ts > _time.mktime(_time.strptime("2026-01-01", "%Y-%m-%d"))

    def test_invalid_format_returns_none(self):
        assert ar._parse_soft_delete_timestamp("not-a-timestamp") is None
        assert ar._parse_soft_delete_timestamp("2026-05-24") is None
        assert ar._parse_soft_delete_timestamp("20260524") is None
        assert ar._parse_soft_delete_timestamp("") is None
