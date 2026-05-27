"""v3.0.0 Phase 3.4 — Amazon Author-Store byLine contributor parsing.

`_parse_byline_contributor` reads one byLine contributor node from the
product-grid widget into a `ContributorInfo` (name + role + Amazon author
ASIN + image). `AmazonSource._product_to_book` maps those onto
`BookResult.contributors`.

The node shape is pinned to a live allbooks payload (Sanderson fixture):

    {"name": "Brandon Sanderson",
     "roles": [{"displayString": "Author", "type": "author"}],
     "contributor": {"author": "/marketplaces/…/authors/B001IGFHW6"},
     "links": […, {"url": "…/amzn-author-media-prod/o1ehb….jpg"}]}

Role rule (locked allowlist): role None when every role is type "author"
(or none present), else the first non-author displayString — so the
downstream `contributor_is_author` filter keeps only authors. The widget
also surfaces Narrator / Publisher / Reader / Illustrator roles, all of
which must drop.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.discovery.sources.amazon import AmazonSource
from app.discovery.sources.amazon_widget_parser import (
    ContributorInfo,
    Product,
    _parse_byline_contributor,
    parse_allbooks_html,
)
from app.discovery.sources.base import contributor_is_author

FIXTURE = Path(__file__).parent.parent.parent / "fixtures" / "amazon" / "sanderson_allbooks_page1.html"


def _node(name, *, roles=None, author_ref=None, links=None):
    n = {"name": name}
    if roles is not None:
        n["roles"] = roles
    if author_ref is not None:
        n["contributor"] = {"author": author_ref}
    if links is not None:
        n["links"] = links
    return n


AUTHOR_ROLE = [{"displayString": "Author", "type": "author"}]


class TestParseBylineContributor:
    def test_author_full_node(self):
        ci = _parse_byline_contributor(_node(
            "Brandon Sanderson",
            roles=AUTHOR_ROLE,
            author_ref="/marketplaces/ATVPDKIKX0DER/contributors/authors/B001IGFHW6",
            links=[
                {"url": "/Brandon-Sanderson/e/B001IGFHW6"},
                {"url": "https://images-na.ssl-images-amazon.com/images/S/amzn-author-media-prod/o1ehb.jpg"},
            ],
        ))
        assert ci == ContributorInfo(
            name="Brandon Sanderson",
            role=None,
            author_id="B001IGFHW6",
            image_url="https://images-na.ssl-images-amazon.com/images/S/amzn-author-media-prod/o1ehb.jpg",
        )

    def test_illustrator_role_kept_as_label(self):
        ci = _parse_byline_contributor(_node(
            "Sam Kieth", roles=[{"displayString": "Illustrator", "type": "illustrator"}]))
        assert ci.role == "Illustrator"
        assert not contributor_is_author(ci.role)  # dropped downstream

    def test_narrator_publisher_reader_all_drop(self):
        for label, typ in (("Narrator", "narrator"), ("Publisher", "publisher"), ("Reader", "reader")):
            ci = _parse_byline_contributor(_node("X", roles=[{"displayString": label, "type": typ}]))
            assert ci.role == label and not contributor_is_author(ci.role)

    def test_multi_role_with_nonauthor_drops(self):
        """Author + Illustrator (graphic-novel creator) → drop on ambiguity."""
        ci = _parse_byline_contributor(_node("Mixed", roles=[
            {"displayString": "Author", "type": "author"},
            {"displayString": "Illustrator", "type": "illustrator"},
        ]))
        assert ci.role == "Illustrator"
        assert not contributor_is_author(ci.role)

    def test_no_roles_treated_as_author(self):
        ci = _parse_byline_contributor(_node("No Roles", author_ref="/x/y/AAA"))
        assert ci.role is None and contributor_is_author(ci.role)
        assert ci.author_id == "AAA"

    def test_missing_name_returns_none(self):
        assert _parse_byline_contributor({"roles": AUTHOR_ROLE}) is None
        assert _parse_byline_contributor({"name": "  "}) is None
        assert _parse_byline_contributor("not a dict") is None

    def test_no_contributor_ref_yields_none_id(self):
        ci = _parse_byline_contributor(_node("Anon", roles=AUTHOR_ROLE))
        assert ci.author_id is None and ci.image_url is None

    def test_image_only_from_image_like_link(self):
        ci = _parse_byline_contributor(_node(
            "P", roles=AUTHOR_ROLE,
            links=[{"url": "/some/store/page"}, {"url": "https://m.media-amazon.com/x.png?foo=1"}]))
        assert ci.image_url == "https://m.media-amazon.com/x.png?foo=1"


class TestFixtureExtraction:
    @pytest.fixture(scope="class")
    def data(self):
        return parse_allbooks_html(FIXTURE.read_text())

    def test_primary_product_author(self, data):
        ci = data.products[0].contributor_details[0]
        assert ci.name == "Brandon Sanderson"
        assert ci.role is None
        assert ci.author_id == "B001IGFHW6"
        assert ci.image_url and "author-media" in ci.image_url

    def test_legacy_names_view_preserved(self, data):
        # `contributors` (names-only) still mirrors the detail names.
        p = data.products[0]
        assert p.contributors == tuple(ci.name for ci in p.contributor_details)

    def test_only_author_roles_survive_filter(self, data):
        """Across the page, every contributor the filter keeps is an
        author; the widget's Narrator/Publisher/Reader/Illustrator roles
        all drop."""
        kept_roles = {
            ci.role
            for p in data.products
            for ci in p.contributor_details
            if contributor_is_author(ci.role)
        }
        assert kept_roles == {None}


class TestProductToBook:
    def test_contributor_details_map_onto_bookresult(self):
        p = Product(
            asin="B000",
            title="Co-Authored Book",
            contributors=("Jason Anspach", "Nick Cole"),
            binding_symbol="kindle_edition",
            binding_display="Kindle Edition",
            series_title=None, series_position=None, series_total=None,
            detail_page_link="/dp/B000", cover_url=None, media_matrix=(), genres=(),
            contributor_details=(
                ContributorInfo("Jason Anspach", None, "B01AAA", "http://img/a.jpg"),
                ContributorInfo("Nick Cole", None, "B01BBB", None),
                ContributorInfo("Some Illustrator", "Illustrator", "B01CCC", None),
            ),
        )
        bk = AmazonSource()._product_to_book(p)
        assert [(c.name, c.role, c.source_author_id) for c in bk.contributors] == [
            ("Jason Anspach", None, "B01AAA"),
            ("Nick Cole", None, "B01BBB"),
            ("Some Illustrator", "Illustrator", "B01CCC"),
        ]
        # role-filter keeps only the two authors
        kept = [c.name for c in bk.contributors if contributor_is_author(c.role)]
        assert kept == ["Jason Anspach", "Nick Cole"]
        assert bk.contributors[0].image_url == "http://img/a.jpg"

    def test_empty_contributor_details_yields_empty_list(self):
        p = Product(
            asin="B1", title="Solo", contributors=(), binding_symbol="kindle_edition",
            binding_display="Kindle Edition", series_title=None, series_position=None,
            series_total=None, detail_page_link="/dp/B1", cover_url=None,
            media_matrix=(), genres=(),
        )
        assert AmazonSource()._product_to_book(p).contributors == []
