"""v3.10.0 — wrong-author validation runs for EVERY source, every scan.

Regression: `_try_source` used to skip validation entirely whenever the
author already had known books ("Author already confirmed"). That made the
guard a one-shot — the first source to validate disarmed it for all the
others.

Live damage on the reference install: hardcover matched Nick Adams' owned
"Fold" novels on scan one, after which OpenLibrary's name-collapsed author
entity (OL2719653A, 41 works spanning a political commentator, a
children's author and a sci-fi author) merged unchecked. "Trump and
Churchill" and "Kenny the Koala Comes to the USA" became missing books.
"""
from __future__ import annotations

import pytest

from app.discovery.lookup import _validate_author
from app.discovery.sources.base import AuthorResult, BookResult


def _res(*titles):
    return AuthorResult(name="x", external_id="e",
                        books=[BookResult(title=t, source="openlibrary") for t in titles])


# ─── _validate_author semantics ──────────────────────────────


async def test_rejects_a_disjoint_catalogue():
    """The Nick Adams shape: owned sci-fi vs a political catalogue."""
    owned = ["The Triangulum Fold", "The Medusa Fold", "The Halo Fold"]
    ol = _res("Retaking America", "Trump and Churchill",
              "Kenny the Koala Comes to the USA", "Green Card Warrior")
    assert await _validate_author("Nick Adams", owned, ol) is False


async def test_accepts_when_one_title_overlaps():
    owned = ["The Triangulum Fold", "The Medusa Fold"]
    hc = _res("The Loom Fold", "The Venn Fold", "The Medusa Fold")
    assert await _validate_author("Nick Adams", owned, hc) is True


async def test_passes_when_nothing_is_owned():
    """Discovered-only authors (allow-listed, nothing owned yet) have no
    ground truth to validate against — must not be blocked."""
    assert await _validate_author("New Author", [], _res("Some Book")) is True


async def test_rejects_empty_source_catalogue():
    assert await _validate_author("X", ["Owned Book"], _res()) is False


# ─── the call-site regression ────────────────────────────────


async def test_validation_is_not_skipped_when_author_has_known_books(monkeypatch):
    """The actual bug: `existing_titles` being non-empty must NOT disarm
    the guard. Drives `_try_source` with a source whose catalogue is
    disjoint from the owned titles, while pretending the author already
    has plenty of known books."""
    from app.discovery import lookup

    merged = {"called": False}

    async def _fake_merge(*a, **k):
        merged["called"] = True
        return (99, 0)

    monkeypatch.setattr(lookup, "_merge_result", _fake_merge)

    class _Src:
        name = "openlibrary"
        async def search_author(self, name):
            return AuthorResult(name=name, external_id="OL2719653A", books=[])
        async def get_author_books(self, ext, **kw):
            return _res("Retaking America", "Trump and Churchill",
                        "Kenny the Koala Comes to the USA")

    n = await lookup._try_source(
        _Src(), "Nick Adams", 1,
        our_titles=["The Triangulum Fold", "The Medusa Fold"],
        languages=["English"], source_name="openlibrary",
        # author is thoroughly "already confirmed" — the old code path
        # would have short-circuited validation on exactly this
        existing_titles={"The Triangulum Fold", "The Medusa Fold",
                         "The Halo Fold", "Consortium"},
    )
    assert n == 0, "disjoint catalogue must be rejected"
    assert merged["called"] is False, "_merge_result must never be reached"


async def test_matching_source_still_merges_when_already_confirmed(monkeypatch):
    """The guard must not become over-strict: a source that DOES overlap
    still merges even though the author has known books."""
    from app.discovery import lookup

    merged = {"called": False}

    async def _fake_merge(*a, **k):
        merged["called"] = True
        return (3, 0)

    monkeypatch.setattr(lookup, "_merge_result", _fake_merge)

    class _Src:
        name = "hardcover"
        async def search_author(self, name):
            return AuthorResult(name=name, external_id="hc1", books=[])
        async def get_author_books(self, ext, **kw):
            return _res("The Loom Fold", "The Venn Fold", "The Medusa Fold")

    n = await lookup._try_source(
        _Src(), "Nick Adams", 1,
        our_titles=["The Triangulum Fold", "The Medusa Fold"],
        languages=["English"], source_name="hardcover",
        existing_titles={"The Triangulum Fold", "The Medusa Fold"},
    )
    assert merged["called"] is True
    assert n == 3
