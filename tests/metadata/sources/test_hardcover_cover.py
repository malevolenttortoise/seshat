"""Hardcover enrichment's cover (2026-10 audit wave 4b, G100): `image` is
Hardcover's `cached_image` object; the record carries its URL, not the
object (review 1996 stored `{"id": 4896320, "url": …, "color": …}`)."""
from app.metadata.sources.hardcover import _book_to_record

URL = "https://assets.hardcover.app/editions/30616074/8936133e-0255-4e5e-a0a2-5c56b280bdb2.jpg"


def test_the_cover_is_the_images_url():
    rec = _book_to_record({"editions": [{"image": {
        "id": 4896320, "url": URL, "color": "#7f7467", "width": 1000, "height": 1499,
        "color_name": "Gray",
    }}]})
    assert rec.cover_url == URL


def test_a_bare_url_still_works():
    assert _book_to_record({"editions": [{"image": URL}]}).cover_url == URL


def test_no_image_no_cover():
    assert _book_to_record({"editions": [{"image": None}]}).cover_url is None
    assert _book_to_record({}).cover_url is None
