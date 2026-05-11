"""Tests for the Dismiss endpoint + cross-list cleanup in approve/reject.

Verifies:
- `POST /api/v1/tentative/{id}/dismiss` marks the row dismissed without
  touching any author table.
- `POST /api/v1/tentative/bulk/dismiss` applies dismiss to every
  pending row.
- Cross-list cleanup #1: `approve` removes each author from
  `authors_tentative_review` so they don't dual-state into both
  "allowed" and "needs review" at once.
- Cross-list cleanup #2: `reject` skips `authors_tentative_review` for
  authors that are already on `authors_allowed`.
- Dismiss declines non-pending rows (idempotency guard).
"""
from __future__ import annotations

from app.database import get_db
from app.storage import authors as authors_storage
from app.storage import tentative as tentative_storage


async def _insert_pending(
    *, mam_id: str, author_blob: str = "Test Author",
) -> int:
    db = await get_db()
    try:
        return await tentative_storage.upsert_tentative(
            db,
            mam_torrent_id=mam_id,
            torrent_name=f"Test {mam_id}",
            author_blob=author_blob,
        )
    finally:
        await db.close()


async def _status(tentative_id: int) -> str:
    db = await get_db()
    try:
        row = await tentative_storage.get_tentative(db, tentative_id)
        return row.status if row else ""
    finally:
        await db.close()


class TestDismiss:
    async def test_dismiss_marks_row_dismissed(self, temp_db):
        from app.routers.tentative import dismiss

        tid = await _insert_pending(mam_id="5001", author_blob="Some Author")

        result = await dismiss(tid)

        assert result.ok is True
        assert result.status == tentative_storage.TENTATIVE_DISMISSED
        assert await _status(tid) == tentative_storage.TENTATIVE_DISMISSED

    async def test_dismiss_does_not_train_or_review_author(self, temp_db):
        from app.routers.tentative import dismiss

        tid = await _insert_pending(
            mam_id="5002", author_blob="Untouched Author"
        )

        await dismiss(tid)

        # Author should be in NEITHER list — dismissal is silent.
        db = await get_db()
        try:
            assert await authors_storage.is_allowed(
                db, "Untouched Author"
            ) is False
            assert await authors_storage.is_tentative_review(
                db, "Untouched Author"
            ) is False
        finally:
            await db.close()

    async def test_dismiss_rejects_non_pending(self, temp_db):
        from app.routers.tentative import dismiss

        tid = await _insert_pending(mam_id="5003")
        # Pre-mark it as rejected.
        db = await get_db()
        try:
            await tentative_storage.set_tentative_status(
                db, tid, tentative_storage.TENTATIVE_REJECTED
            )
        finally:
            await db.close()

        result = await dismiss(tid)

        assert result.ok is False
        assert "rejected" in (result.error or "")
        # Status should NOT have flipped to dismissed.
        assert await _status(tid) == tentative_storage.TENTATIVE_REJECTED

    async def test_bulk_dismiss_dismisses_all_pending(self, temp_db):
        from app.routers.tentative import bulk_dismiss

        ids = [
            await _insert_pending(mam_id="5010"),
            await _insert_pending(mam_id="5011"),
            await _insert_pending(mam_id="5012"),
        ]
        result = await bulk_dismiss(None)

        assert result.processed == 3
        assert result.failed == 0
        for tid in ids:
            assert await _status(tid) == tentative_storage.TENTATIVE_DISMISSED

    async def test_bulk_dismiss_subset(self, temp_db):
        from app.routers.tentative import bulk_dismiss
        from app.routers.tentative import BulkRequest

        ids = [
            await _insert_pending(mam_id="5020"),
            await _insert_pending(mam_id="5021"),
            await _insert_pending(mam_id="5022"),
        ]
        result = await bulk_dismiss(BulkRequest(ids=[ids[0], ids[2]]))

        assert result.processed == 2
        assert await _status(ids[0]) == tentative_storage.TENTATIVE_DISMISSED
        assert await _status(ids[1]) == tentative_storage.TENTATIVE_PENDING
        assert await _status(ids[2]) == tentative_storage.TENTATIVE_DISMISSED


class TestCrossListCleanup:
    async def test_reject_skips_review_if_author_already_allowed(self, temp_db):
        """Author on the allow list shouldn't get dragged into the
        weekly review queue by a one-off reject."""
        from app.routers.tentative import reject

        db = await get_db()
        try:
            await authors_storage.add_allowed(
                db, "Already Allowed", source="test"
            )
        finally:
            await db.close()

        tid = await _insert_pending(
            mam_id="6001", author_blob="Already Allowed",
        )

        result = await reject(tid)

        assert result.ok is True
        db = await get_db()
        try:
            # Still allowed.
            assert await authors_storage.is_allowed(
                db, "Already Allowed"
            ) is True
            # NOT added to tentative_review — the guard fired.
            assert await authors_storage.is_tentative_review(
                db, "Already Allowed"
            ) is False
        finally:
            await db.close()

    async def test_reject_normal_path_still_adds_to_review(self, temp_db):
        """Sanity check: a reject of a NOT-already-allowed author still
        lands on the weekly review queue (existing behavior preserved)."""
        from app.routers.tentative import reject

        tid = await _insert_pending(
            mam_id="6002", author_blob="Brand New Author",
        )

        await reject(tid)

        db = await get_db()
        try:
            assert await authors_storage.is_tentative_review(
                db, "Brand New Author"
            ) is True
            assert await authors_storage.is_allowed(
                db, "Brand New Author"
            ) is False
        finally:
            await db.close()

    async def test_approve_cleans_up_tentative_review(self, temp_db, monkeypatch):
        """Author sitting on the review queue from a prior reject
        should be removed by a subsequent approve. Without this they'd
        appear in BOTH lists simultaneously."""
        from app import state
        from app.routers.tentative import approve

        # Pre-seed: author on the tentative_review list.
        db = await get_db()
        try:
            await authors_storage.add_tentative_review(
                db, "Recovering Author", source="prior_reject"
            )
            assert await authors_storage.is_tentative_review(
                db, "Recovering Author"
            ) is True
        finally:
            await db.close()

        # Stub the dispatcher so approve doesn't try to hit MAM.
        class _NullDispatcher:
            pass
        monkeypatch.setattr(state, "dispatcher", _NullDispatcher())

        # Stub inject_grab so approve's grab-fetch is a no-op success.
        from app.routers import tentative as tentative_router
        from app.orchestrator.dispatch import DispatchResult

        async def _fake_inject(*args, **kwargs):
            return DispatchResult(
                action="submit",
                reason="injected_test",
                announce_id=0,
                grab_id=42,
                error=None,
            )
        monkeypatch.setattr(tentative_router, "inject_grab", _fake_inject)

        tid = await _insert_pending(
            mam_id="6010", author_blob="Recovering Author",
        )
        await approve(tid)

        db = await get_db()
        try:
            # Now ALLOWED and REMOVED from review.
            assert await authors_storage.is_allowed(
                db, "Recovering Author"
            ) is True
            assert await authors_storage.is_tentative_review(
                db, "Recovering Author"
            ) is False
        finally:
            await db.close()
