"""Goodreads fetches the books discovery doesn't have before the ones it
does (2026-10 audit issue 12; Mark, G47), and a resume keeps the order."""
from app.discovery.sources.goodreads import GoodreadsSource


def _rb(book_id: str, title: str) -> dict:
    return {"book_id": book_id, "title": title}


def test_new_books_come_first_in_list_order():
    src = GoodreadsSource(rate_limit=0.0)
    src._known_titles = {"contracts cats", "keeper kindred"}
    raw = [
        _rb("1", "Contracts & Cats"), _rb("2", "Keeper & Kindred"),
        _rb("7", "Magic's Toll: Fatebound"), _rb("8", "Magic's Toll: Cursebound"),
    ]
    assert [r["book_id"] for r in src._detail_order(raw, "A", 0)] == ["7", "8", "1", "2"]


def test_no_known_titles_keeps_the_list_order():
    src = GoodreadsSource(rate_limit=0.0)
    raw = [_rb("1", "One"), _rb("2", "Two")]
    assert src._detail_order(raw, "A", 0) == raw


def test_a_resume_reuses_the_first_calls_order():
    src = GoodreadsSource(rate_limit=0.0)
    src._partial_state = {"author_id": "A", "index": 1, "order": ["8", "7", "1"]}
    src._known_titles = set()               # whatever changed since
    raw = [_rb("1", "One"), _rb("7", "Seven"), _rb("8", "Eight")]
    assert [r["book_id"] for r in src._detail_order(raw, "A", 1)] == ["8", "7", "1"]


def test_series_suffixes_and_subtitles_still_count_as_known():
    """Live shapes (2026-10-08, Toni Binns): Goodreads' list titles carry
    the series, discovery's carry subtitles; only the two Goodreads-only
    books are new."""
    from app.discovery.sources.goodreads import known_title_keys

    src = GoodreadsSource(rate_limit=0.0)
    src._known_titles = known_title_keys([
        "Contracts & Cats", "Call of the Traveler (Traveler Series Book 2)",
        "Harmony & Home: A Cozy Slice-of-Life Fantasy (Meow: Magical Emporium of Wares Book 4)",
    ])
    raw = [
        _rb("1", "Contracts & Cats (Meow: Magical Emporium of Wares #1)"),
        _rb("5", "Call of the Traveler (Traveler #2)"),
        _rb("7", "Magic's Toll: Fatebound"),
        _rb("9", "Harmony & Home (Meow: Magical Emporium of Wares #4)"),
    ]
    assert [r["book_id"] for r in src._detail_order(raw, "A", 0)] == ["7", "1", "5", "9"]


def test_a_shared_prefix_doesnt_make_a_new_book_known():
    from app.discovery.sources.goodreads import _norm_title, known_title_keys

    keys = known_title_keys(["Magic's Toll: Fatebound"])
    assert _norm_title("Magic's Toll: Cursebound") not in keys
