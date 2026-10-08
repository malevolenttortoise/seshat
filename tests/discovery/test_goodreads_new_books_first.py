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
