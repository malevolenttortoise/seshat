"""
Unit tests for the pending grabs queue.

Coverage targets:
  - enqueue: insert + idempotent re-insert with priority change
  - size: empty + non-empty
  - peek_next + take: the budget watcher's drain (peek, check, take)
  - Pop ordering: priority desc first, then queued_at asc (FIFO
    among same-priority entries)
  - remove: explicit cancel
  - list_all: dashboard view
"""
import asyncio

from app.database import get_db
from app.rate_limit import queue
from tests.rate_limit._helpers import insert_dummy_grab


async def _drain_one(db):
    """What the budget watcher does per pop: peek, then take that grab."""
    head = await queue.peek_next(db)
    if head is not None:
        assert await queue.take(db, head.grab_id)
    return head


class TestEnqueue:
    async def test_basic_enqueue(self, temp_db):
        db = await get_db()
        try:
            grab_id = await insert_dummy_grab(db)
            await queue.enqueue(db, grab_id)

            assert await queue.size(db) == 1
            queued = await queue.peek_next(db)
            assert queued is not None
            assert queued.grab_id == grab_id
            assert queued.priority == 0
        finally:
            await db.close()

    async def test_enqueue_with_priority(self, temp_db):
        db = await get_db()
        try:
            grab_id = await insert_dummy_grab(db)
            await queue.enqueue(db, grab_id, priority=5)

            queued = await queue.peek_next(db)
            assert queued is not None
            assert queued.priority == 5
        finally:
            await db.close()

    async def test_re_enqueue_updates_priority(self, temp_db):
        db = await get_db()
        try:
            grab_id = await insert_dummy_grab(db)
            await queue.enqueue(db, grab_id, priority=0)
            await queue.enqueue(db, grab_id, priority=10)

            assert await queue.size(db) == 1  # not duplicated
            queued = await queue.peek_next(db)
            assert queued is not None
            assert queued.priority == 10
        finally:
            await db.close()


class TestSize:
    async def test_empty(self, temp_db):
        db = await get_db()
        try:
            assert await queue.size(db) == 0
        finally:
            await db.close()

    async def test_grows_with_each_enqueue(self, temp_db):
        db = await get_db()
        try:
            for i in range(4):
                grab_id = await insert_dummy_grab(db, torrent_id=str(i))
                await queue.enqueue(db, grab_id)
            assert await queue.size(db) == 4
        finally:
            await db.close()


class TestPopOrdering:
    async def test_fifo_within_same_priority(self, temp_db):
        db = await get_db()
        try:
            ids = []
            for i in range(3):
                grab_id = await insert_dummy_grab(db, torrent_id=str(i))
                await queue.enqueue(db, grab_id)
                ids.append(grab_id)
                # Force a measurable timestamp gap so the ORDER BY
                # queued_at ASC has a stable order to work with —
                # SQLite's datetime('now') is second-precision.
                await asyncio.sleep(1.05)

            popped = []
            for _ in range(3):
                q = await _drain_one(db)
                assert q is not None
                popped.append(q.grab_id)

            assert popped == ids  # oldest first
        finally:
            await db.close()

    async def test_priority_beats_age(self, temp_db):
        db = await get_db()
        try:
            old_id = await insert_dummy_grab(db, torrent_id="old")
            await queue.enqueue(db, old_id, priority=0)
            await asyncio.sleep(1.05)

            new_high_id = await insert_dummy_grab(db, torrent_id="new")
            await queue.enqueue(db, new_high_id, priority=10)

            # The newer high-priority entry should pop first.
            first = await _drain_one(db)
            assert first is not None
            assert first.grab_id == new_high_id

            second = await _drain_one(db)
            assert second is not None
            assert second.grab_id == old_id
        finally:
            await db.close()

    async def test_pop_empty_returns_none(self, temp_db):
        db = await get_db()
        try:
            assert await _drain_one(db) is None
        finally:
            await db.close()

    async def test_peek_doesnt_remove(self, temp_db):
        db = await get_db()
        try:
            grab_id = await insert_dummy_grab(db)
            await queue.enqueue(db, grab_id)

            await queue.peek_next(db)
            await queue.peek_next(db)
            assert await queue.size(db) == 1
        finally:
            await db.close()

    async def test_take_removes(self, temp_db):
        db = await get_db()
        try:
            grab_id = await insert_dummy_grab(db)
            await queue.enqueue(db, grab_id)

            assert await queue.take(db, grab_id) is True
            assert await queue.take(db, grab_id) is False  # already gone
            assert await queue.size(db) == 0
            assert await queue.peek_next(db) is None
        finally:
            await db.close()


class TestRemove:
    async def test_remove_specific_grab(self, temp_db):
        db = await get_db()
        try:
            ids = []
            for i in range(3):
                grab_id = await insert_dummy_grab(db, torrent_id=str(i))
                await queue.enqueue(db, grab_id)
                ids.append(grab_id)

            await queue.remove(db, ids[1])
            assert await queue.size(db) == 2

            remaining = [q.grab_id for q in await queue.list_all(db)]
            assert ids[1] not in remaining
        finally:
            await db.close()

    async def test_remove_unknown_silent_noop(self, temp_db):
        db = await get_db()
        try:
            await queue.remove(db, 99999)
        finally:
            await db.close()


class TestListAll:
    async def test_returns_in_pop_order(self, temp_db):
        db = await get_db()
        try:
            low_id = await insert_dummy_grab(db, torrent_id="low")
            await queue.enqueue(db, low_id, priority=0)
            await asyncio.sleep(1.05)
            high_id = await insert_dummy_grab(db, torrent_id="high")
            await queue.enqueue(db, high_id, priority=5)

            all_queued = await queue.list_all(db)
            assert [q.grab_id for q in all_queued] == [high_id, low_id]
        finally:
            await db.close()
