"""
Tests for `_orphan_series_promotion_pass` and the cross-author
collision fix in `_ensure_series_for_author`.

Regression context: every source returned Warden Locke's "Player
Slayer: Spicy Gamelit Fantasy Episode N" books, his "Manassassin
N (Manassassin #N)" books, and his "Soulless Rising N (Soulless
Rising #N)" books as STANDALONES — none asserted a series row.
`_title_to_series_pass` only links to series that already exist,
so it couldn't help. The new pass detects clusters of standalones
sharing a prefix + per-book numeric markers and bootstraps a series.

Companion fix: `_ensure_series` is now author-scoped (current author
+ pen-name partners) so two unrelated authors with the same series
name (Cressman/Savarovsky "The Last Paladin") get separate series
rows instead of collapsing into one.
"""
from __future__ import annotations

import pytest

from app.discovery.lookup import _extract_series_signal


# ─── _extract_series_signal (pure) ────────────────────────────

class TestExtractSeriesSignal:
    """Direct regex tests for each arm of `_extract_series_signal`."""

    @pytest.mark.parametrize("title,expected", [
        # Arm 1: parenthetical "(SeriesName #N)"
        ("Manassassin 1 (Manassassin #1)", ("Manassassin", 1.0)),
        ("Soulless Rising (Soulless Rising #1)", ("Soulless Rising", 1.0)),
        ("Dungeonteers: A Shifter's Journey 4 (Dungeonteers #4)", ("Dungeonteers", 4.0)),
        ("Kingdom Evolution 2 (Kingdom Evolution #2)", ("Kingdom Evolution", 2.0)),
        # Arm 2a: subtitle volume marker
        ("Player Slayer: Spicy Gamelit Fantasy Episode 4", ("Player Slayer", 4.0)),
        ("Player Slayer: Fun and Spicy Gamelit Episode 1", ("Player Slayer", 1.0)),
        ("Manassassin: LitRPG Harem Adventure Book 1", ("Manassassin", 1.0)),
        ("Uncle Rob Left Me His Fantasy World: Episode 1", ("Uncle Rob Left Me His Fantasy World", 1.0)),
        ("Some Saga: Volume 3", ("Some Saga", 3.0)),
        # Arm 2b: prefix trailing number with subtitle
        ("Dungeon Depot 2: Slice of Life LitRPG Harem", ("Dungeon Depot", 2.0)),
        ("Masked Chaos 2 (Finale): A Spicy Dark Fantasy", ("Masked Chaos", 2.0)),
        # Arm 2c: bare prefix with subtitle, no number
        ("Dungeon Depot: Slice of Life LitRPG Harem", ("Dungeon Depot", None)),
        ("Masked Chaos: A Spicy Halloween Dark Fantasy", ("Masked Chaos", None)),
        # Arm 3: no colon, trailing number
        ("Manassassin 2", ("Manassassin", 2.0)),
        ("Soulless Rising 3", ("Soulless Rising", 3.0)),
        # Whitespace tolerance — extra space before colon
        ("Player Slayer : Spicy Gamelit Fantasy Episode 9", ("Player Slayer", 9.0)),
        # Volume-marker word attached to PREFIX (not subtitle) — must
        # be stripped from series name. Borgy60 canary.
        ("The Last Legend Reborn Book 2: An OP MC Regression LitRPG",
         ("The Last Legend Reborn", 2.0)),
        ("Tower Breaker Book 1: A LitRPG Apocalypse",
         ("Tower Breaker", 1.0)),
        ("Some Saga Vol 3: Aftermath", ("Some Saga", 3.0)),
        ("Some Saga Volume 4: Aftermath", ("Some Saga", 4.0)),
        ("Some Saga Part 2", ("Some Saga", 2.0)),
    ])
    def test_extracts_signal(self, title: str, expected: tuple[str, float | None]) -> None:
        sig = _extract_series_signal(title)
        assert sig is not None, f"expected signal for {title!r}, got None"
        assert sig[0] == expected[0]
        assert sig[1] == expected[1]

    @pytest.mark.parametrize("title", [
        # No structure at all → no signal
        "Above the Bookstore",
        "Spawnmons",
        # Below min prefix length
        "X 1",
        "II 2",
        # Single-token title with no colon, no number
        "Manassassin",
    ])
    def test_no_signal(self, title: str) -> None:
        # Either None or a prefix-only signal (idx=None) — both are
        # acceptable for clustering since singletons won't promote.
        sig = _extract_series_signal(title)
        if sig is not None:
            assert sig[1] is None  # prefix-only is fine; idx must be None


# ─── DB-backed pass tests ─────────────────────────────────────

@pytest.fixture
async def discovery_db(tmp_path, monkeypatch):
    from app import config as app_config
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    yield tmp_path
    disco_db.set_active_library(None)


async def _insert_author(name: str) -> int:
    from app.discovery.database import get_db
    from app.metadata.author_names import normalize_author_name
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name, normalized_name) "
            "VALUES (?, ?, ?)",
            (name, name, normalize_author_name(name)),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _insert_book(author_id: int, title: str, **kwargs) -> int:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        owned = kwargs.get("owned", 0)
        is_omni = kwargs.get("is_omnibus", 0)
        hidden = kwargs.get("hidden", 0)
        cur = await db.execute(
            "INSERT INTO books (title, author_id, source, owned, "
            "is_omnibus, hidden) VALUES (?, ?, 'goodreads', ?, ?, ?)",
            (title, author_id, owned, is_omni, hidden),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _series_for_book(book_id: int):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT b.title, b.series_index, s.name AS series_name "
            "FROM books b LEFT JOIN series s ON b.series_id = s.id "
            "WHERE b.id = ?",
            (book_id,),
        )).fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def _author_books(author_id: int):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT b.id, b.title, b.series_index, s.name AS series_name "
            "FROM books b LEFT JOIN series s ON b.series_id = s.id "
            "WHERE b.author_id = ? ORDER BY b.id",
            (author_id,),
        )).fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


class TestOrphanPromotionParenthetical:
    """Arm 1: explicit (SeriesName #N) annotation."""

    async def test_paren_cluster_promoted(self, discovery_db):
        from app.discovery.lookup import _orphan_series_promotion_pass

        author_id = await _insert_author("Warden Locke")
        b1 = await _insert_book(author_id, "Soulless Rising (Soulless Rising #1)")
        b2 = await _insert_book(author_id, "Soulless Rising 2 (Soulless Rising #2)")
        b3 = await _insert_book(author_id, "Soulless Rising 3 (Soulless Rising #3)")

        promoted = await _orphan_series_promotion_pass(author_id)
        assert promoted == 3

        for bid, expected_idx in [(b1, 1.0), (b2, 2.0), (b3, 3.0)]:
            row = await _series_for_book(bid)
            assert row["series_name"] == "Soulless Rising"
            assert row["series_index"] == expected_idx

    async def test_paren_singleton_not_promoted(self, discovery_db):
        """A single book with parenthetical annotation is not enough.
        Need at least 2 cluster members."""
        from app.discovery.lookup import _orphan_series_promotion_pass

        author_id = await _insert_author("Warden Locke")
        b1 = await _insert_book(author_id, "Soulless Rising (Soulless Rising #1)")

        promoted = await _orphan_series_promotion_pass(author_id)
        assert promoted == 0
        row = await _series_for_book(b1)
        assert row["series_name"] is None


class TestOrphanPromotionSubtitleMarker:
    """Arm 2: prefix + Episode N / Book N / Volume N subtitle."""

    async def test_episode_cluster_promoted(self, discovery_db):
        """Player Slayer: 9 books with Episode N subtitle."""
        from app.discovery.lookup import _orphan_series_promotion_pass

        author_id = await _insert_author("Warden Locke")
        ids = []
        for n in range(1, 10):
            ids.append(await _insert_book(
                author_id,
                f"Player Slayer: Spicy Gamelit Fantasy Episode {n}",
            ))

        promoted = await _orphan_series_promotion_pass(author_id)
        assert promoted == 9

        for n, bid in enumerate(ids, start=1):
            row = await _series_for_book(bid)
            assert row["series_name"] == "Player Slayer"
            assert row["series_index"] == float(n)

    async def test_mixed_paren_and_subtitle(self, discovery_db):
        """Manassassin: 3 paren annotations + 1 'Book 1' subtitle.
        Both arms agree on prefix 'Manassassin'. The duplicate at
        index 1 should be deduped (existing parenthetical-form row
        wins because the subtitle row has 'Book 1' suffix)."""
        from app.discovery.lookup import _orphan_series_promotion_pass

        author_id = await _insert_author("Warden Locke")
        await _insert_book(author_id, "Manassassin 1 (Manassassin #1)")
        await _insert_book(author_id, "Manassassin 2 (Manassassin #2)")
        await _insert_book(author_id, "Manassassin 3 (Manassassin #3)")
        await _insert_book(author_id, "Manassassin: LitRPG Harem Adventure Book 1")

        promoted = await _orphan_series_promotion_pass(author_id)
        # 3 paren-form books promoted at #1/#2/#3; b4 dedup-loses to b1.
        assert promoted >= 3

        # Verify final state: the Book-N-suffix loser is gone,
        # parenthetical winners remain at correct indices.
        books = await _author_books(author_id)
        assert len(books) == 3
        names = {b["title"]: b for b in books}
        assert "Manassassin 1 (Manassassin #1)" in names
        assert "Manassassin 2 (Manassassin #2)" in names
        assert "Manassassin 3 (Manassassin #3)" in names
        assert "Manassassin: LitRPG Harem Adventure Book 1" not in names

    async def test_prefix_with_one_no_number_member(self, discovery_db):
        """Dungeon Depot: bare prefix + 2 numbered → bare defaults
        to index 1, the others keep their explicit numbers."""
        from app.discovery.lookup import _orphan_series_promotion_pass

        author_id = await _insert_author("Warden Locke")
        await _insert_book(author_id, "Dungeon Depot: Slice of Life LitRPG Harem")
        await _insert_book(author_id, "Dungeon Depot 2: Slice of Life LitRPG Harem")
        await _insert_book(author_id, "Dungeon Depot 3: Slice of Life LitRPG Harem")

        promoted = await _orphan_series_promotion_pass(author_id)
        assert promoted == 3

        rows = {r["title"]: r for r in await _author_books(author_id)}
        assert rows["Dungeon Depot: Slice of Life LitRPG Harem"]["series_index"] == 1.0
        assert rows["Dungeon Depot 2: Slice of Life LitRPG Harem"]["series_index"] == 2.0
        assert rows["Dungeon Depot 3: Slice of Life LitRPG Harem"]["series_index"] == 3.0


class TestOrphanPromotionGuards:
    """Skip rules — owned, hidden, omnibus, single-member clusters."""

    async def test_owned_books_skipped(self, discovery_db):
        """User's curated Calibre rows are off-limits."""
        from app.discovery.lookup import _orphan_series_promotion_pass

        author_id = await _insert_author("Warden Locke")
        await _insert_book(author_id, "Manassassin 1 (Manassassin #1)", owned=1)
        await _insert_book(author_id, "Manassassin 2 (Manassassin #2)", owned=1)
        await _insert_book(author_id, "Manassassin 3 (Manassassin #3)", owned=1)

        promoted = await _orphan_series_promotion_pass(author_id)
        assert promoted == 0

    async def test_omnibus_skipped(self, discovery_db):
        """An is_omnibus row in the cluster is filtered before
        promotion — it routes to the omnibus sub-row instead."""
        from app.discovery.lookup import _orphan_series_promotion_pass

        author_id = await _insert_author("Warden Locke")
        b1 = await _insert_book(author_id, "Player Slayer: Spicy Gamelit Fantasy Episode 1")
        b2 = await _insert_book(author_id, "Player Slayer: Spicy Gamelit Fantasy Episode 2")
        omni = await _insert_book(
            author_id,
            "Player Slayer: Spicy Gamelit Adventure Volume Set 2",
            is_omnibus=1,
        )

        promoted = await _orphan_series_promotion_pass(author_id)
        assert promoted == 2

        # Episodes promoted, omnibus untouched
        row1 = await _series_for_book(b1)
        row2 = await _series_for_book(b2)
        omni_row = await _series_for_book(omni)
        assert row1["series_name"] == "Player Slayer"
        assert row2["series_name"] == "Player Slayer"
        assert omni_row["series_name"] is None

    async def test_hidden_skipped(self, discovery_db):
        from app.discovery.lookup import _orphan_series_promotion_pass

        author_id = await _insert_author("Warden Locke")
        await _insert_book(author_id, "Manassassin 1 (Manassassin #1)", hidden=1)
        await _insert_book(author_id, "Manassassin 2 (Manassassin #2)", hidden=1)
        await _insert_book(author_id, "Manassassin 3 (Manassassin #3)", hidden=1)

        promoted = await _orphan_series_promotion_pass(author_id)
        assert promoted == 0

    async def test_no_explicit_indices_no_promotion(self, discovery_db):
        """Cluster needs ≥ 2 members with explicit numeric indices.
        Two bare-prefix books don't cross the threshold."""
        from app.discovery.lookup import _orphan_series_promotion_pass

        author_id = await _insert_author("Warden Locke")
        await _insert_book(author_id, "Same Prefix: First Subtitle")
        await _insert_book(author_id, "Same Prefix: Second Subtitle")

        promoted = await _orphan_series_promotion_pass(author_id)
        assert promoted == 0


# ─── Cross-author collision fix ───────────────────────────────

class TestEnsureSeriesAuthorScoped:
    """The Cressman/Savarovsky "The Last Paladin" case."""

    async def test_unrelated_authors_get_separate_rows(self, discovery_db):
        """Two unrelated authors using the same series name no longer
        collapse into one row. Each gets its own author-scoped row."""
        from app.discovery.lookup import _ensure_series_for_author
        from app.discovery.database import get_db

        cressman = await _insert_author("John Cressman")
        savarovsky = await _insert_author("Roman Savarovsky")

        db = await get_db()
        try:
            sid_a = await _ensure_series_for_author(db, "The Last Paladin", cressman)
            sid_b = await _ensure_series_for_author(db, "The Last Paladin", savarovsky)
            await db.commit()

            assert sid_a != sid_b

            rows = await (await db.execute(
                "SELECT id, name, author_id FROM series ORDER BY id"
            )).fetchall()
            assert len(rows) == 2
            assert {r["author_id"] for r in rows} == {cressman, savarovsky}
        finally:
            await db.close()

    async def test_same_author_returns_same_row(self, discovery_db):
        """Idempotent for the same author."""
        from app.discovery.lookup import _ensure_series_for_author
        from app.discovery.database import get_db

        author_id = await _insert_author("John Cressman")

        db = await get_db()
        try:
            sid_a = await _ensure_series_for_author(db, "The Last Paladin", author_id)
            sid_b = await _ensure_series_for_author(db, "The Last Paladin", author_id)
            assert sid_a == sid_b
        finally:
            await db.close()

    async def test_pen_name_partners_share_row(self, discovery_db):
        """Darren and Arand are pen-name linked — they SHOULD share
        the "Incubus Inc." row. The fix preserves that behavior."""
        from app.discovery.lookup import _ensure_series_for_author
        from app.discovery.database import get_db

        arand = await _insert_author("Arand Welkin")
        darren = await _insert_author("Darren Welkin")

        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO pen_name_links "
                "(canonical_author_id, alias_author_id) VALUES (?, ?)",
                (arand, darren),
            )
            await db.commit()

            sid_a = await _ensure_series_for_author(db, "Incubus Inc.", arand)
            sid_b = await _ensure_series_for_author(db, "Incubus Inc.", darren)
            assert sid_a == sid_b
        finally:
            await db.close()
