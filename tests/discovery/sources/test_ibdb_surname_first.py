"""IBDB gives some bylines surname first ("Dean, Travis"), which never
matched an author stored given name first (2026-10 audit wave 4 S9, G65;
live check 10-08)."""
import httpx
import pytest

from app.discovery.sources import ibdb as disco_ibdb
from app.metadata.author_names import flip_surname_first
from app.metadata.sources import ibdb as meta_ibdb


@pytest.mark.parametrize("name,flipped", [
    ("Dean, Travis", "Travis Dean"),
    ("Le Guin, Ursula K.", "Ursula K. Le Guin"),
    ("Travis Dean", "Travis Dean"),
    ("Smith, Jr.", "Smith, Jr."),
    ("Smith, III", "Smith, III"),
    ("A, B, C", "A, B, C"),
    ("Dean,", "Dean,"),
    ("", ""),
])
def test_flip_surname_first(name, flipped):
    assert flip_surname_first(name) == flipped


def test_both_ibdb_sources_read_bylines_given_name_first():
    item = {"authors": [{"name": "Dean, Travis (author)"}, "Smith, Jo"]}
    assert disco_ibdb._extract_authors(item) == ["Travis Dean", "Jo Smith"]
    assert meta_ibdb._extract_authors({"authors": ["Dean, Travis"]}) == ["Travis Dean"]


async def test_an_ibdb_scan_keeps_a_surname_first_book():
    def handler(req):
        return httpx.Response(200, json={"books": [
            {"title": "Banished", "authors": ["Dean, Travis"], "id": "u1"},
            {"title": "Someone Else's", "authors": ["Jones, Alex"], "id": "u2"},
        ]})

    src = disco_ibdb.IbdbSource(rate_limit=0)
    src._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await src.get_author_books("Travis Dean")
    titles = {b.title for b in result.books} | {b.title for s in result.series for b in s.books}
    assert titles == {"Banished"}
