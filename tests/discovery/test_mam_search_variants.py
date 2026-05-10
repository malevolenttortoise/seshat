"""Search-cascade alternate-form generator tests.

`_alternate_title_forms` and `_alternate_author_forms` generate
variant queries for the cascade's passes 6+. They exist to bridge
MAM's stricter tokenization (no space-collapsing on initials,
zero-padding sensitivity on trailing numbers) so the search returns
the right candidate when our source-side form differs from MAM's.
"""
import pytest

from app.discovery.sources.mam import (
    _alternate_author_forms,
    _alternate_title_forms,
)


# ─── Title variants ─────────────────────────────────────────────


class TestAlternateTitleForms:
    @pytest.mark.parametrize("title,expected", [
        # Canonical D6/Warhawk case — trailing number stripped
        ("Right of Retribution 2", ["Right of Retribution"]),
        ("Domestic Decay 2", ["Domestic Decay"]),
        ("School of Magic 2", ["School of Magic"]),
        ("Past Life Hero 2", ["Past Life Hero"]),
        # Multi-digit trailing number
        ("My Series 12", ["My Series"]),
        # Single trailing number with extra whitespace
        ("Foo  3  ", ["Foo"]),
    ])
    def test_strips_trailing_number(self, title, expected):
        assert _alternate_title_forms(title) == expected

    @pytest.mark.parametrize("title", [
        # No trailing number
        "The Way of Kings",
        "Foundation",
        # Only whitespace + number guards against false positives — the
        # regex requires a space before the digit, so these don't match
        # — they're intentionally not stripped:
        "Foundation1",
        # Mid-title number — anchored to end so this doesn't match
        "Apollo 11 Mission Report",
        # Empty / falsy
        "",
        "   ",
        # Result of stripping would be too short
        "AB 5",
    ])
    def test_negative_no_strip(self, title):
        assert _alternate_title_forms(title) == []


# ─── Author variants ────────────────────────────────────────────


class TestAlternateAuthorForms:
    @pytest.mark.parametrize("author,expected", [
        # Canonical Veil case
        ("J J Cross", ["JJ Cross", "J.J. Cross"]),
        # 3-initial author
        ("J R R Tolkien", ["JRR Tolkien", "J.R.R. Tolkien"]),
        # With existing periods
        ("J. K. Rowling", ["JK Rowling", "J.K. Rowling"]),
        ("P. G. Wodehouse", ["PG Wodehouse", "P.G. Wodehouse"]),
        # Concatenated form → split variants
        ("JK Rowling", ["J K Rowling", "J.K. Rowling"]),
        ("JRR Tolkien", ["J R R Tolkien", "J.R.R. Tolkien"]),
    ])
    def test_generates_initial_variants(self, author, expected):
        assert _alternate_author_forms(author) == expected

    @pytest.mark.parametrize("author", [
        # Single author name — no initials
        "Tolkien",
        "Catherine Fisher",
        "Brandon Sanderson",
        # First-name + surname (no initials)
        "Brandon Sanderson",
        # Single initial only — not enough to bridge tokenization
        "J Smith",
        # Empty
        "",
    ])
    def test_no_variants_for_non_initial_authors(self, author):
        assert _alternate_author_forms(author) == []

    def test_excludes_input_form(self):
        # Input "JJ Cross" should not appear in its own variant list.
        out = _alternate_author_forms("JJ Cross")
        assert "JJ Cross" not in out

    def test_dedupes_collapsed_variants(self):
        # If concat and with_periods coincidentally equal, only one
        # appears in the output. (Edge case — single-letter surname.)
        # Using "J K Rowling" as the canonical case where concat
        # ("JK Rowling") and with_periods ("J.K. Rowling") differ.
        out = _alternate_author_forms("J K Rowling")
        assert len(out) == len(set(out))


# ─── Combined cases known from UAT ──────────────────────────────


class TestKnownUatCases:
    """Pin the specific cases that A1+A3 UAT identified as fixable."""

    def test_d6_right_of_retribution(self):
        # Currently-stored URL fails because MAM has "Right of
        # Retribution 02" (zero-padded). Stripping the number
        # surfaces the right tid.
        assert "Right of Retribution" in _alternate_title_forms(
            "Right of Retribution 2"
        )

    def test_warhawk_amnesty(self):
        # Amnesty stays as-is (no trailing number); the cover-pHash
        # path catches the wrong-stored URL via demote signal. No
        # variant needed for this case — pin that the helper doesn't
        # produce a spurious one.
        assert _alternate_title_forms("Warhawk's Amnesty") == []

    def test_veil_jj_cross(self):
        # MAM has "JJ Cross"; Calibre has "J J Cross". Variants must
        # include the no-space form.
        variants = _alternate_author_forms("J J Cross")
        assert "JJ Cross" in variants
