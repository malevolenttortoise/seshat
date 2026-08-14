"""
Unit tests for the filter gate.

Covers the full decision matrix from the original `ebook_gate.sh`:
  - category gating
  - author detection (clean blob path + scrape-from-text fallback)
  - allow / ignore / unknown classification
  - allow-wins-over-ignore for multi-author releases
  - the weekly-skip side data on the Decision

These tests are deliberately exhaustive on the decision matrix
because this is the load-bearing logic that decides whether to spend
a snatch budget slot. A regression here is silent and expensive.
"""
from app.filter.gate import (
    Announce,
    FilterConfig,
    evaluate_announce,
    extract_author_blob_from_text,
    split_authors,
)
from app.filter.normalize import (
    extract_format,
    normalize_author,
    normalize_category,
)


# ─── Helpers ─────────────────────────────────────────────────


def make_config(
    categories: list[str] | None = None,
    excluded_categories: list[str] | None = None,
    allowed_formats: list[str] | None = None,
    excluded_formats: list[str] | None = None,
    allowed_languages: list[str] | None = None,
    allowed: list[str] | None = None,
    ignored: list[str] | None = None,
) -> FilterConfig:
    """Build a FilterConfig with realistic defaults, normalized."""
    cats = categories if categories is not None else [
        "Ebooks - Fantasy",
        "Ebooks - Science Fiction",
        "AudioBooks - Fantasy",
    ]
    return FilterConfig(
        allowed_categories=frozenset(normalize_category(c) for c in cats),
        excluded_categories=frozenset(
            normalize_category(c) for c in (excluded_categories or [])
        ),
        allowed_formats=frozenset(
            extract_format(f) or normalize_category(f)
            for f in (allowed_formats or [])
        ),
        excluded_formats=frozenset(
            extract_format(f) or normalize_category(f)
            for f in (excluded_formats or [])
        ),
        allowed_languages=frozenset(
            lang.strip().lower() for lang in (allowed_languages or [])
        ),
        allowed_authors=frozenset(normalize_author(a) for a in (allowed or [])),
        ignored_authors=frozenset(normalize_author(a) for a in (ignored or [])),
    )


def make_announce(
    category: str = "Ebooks - Fantasy",
    author_blob: str = "Brandon Sanderson",
    torrent_name: str = "Some Book Title",
    **kwargs,
) -> Announce:
    return Announce(
        torrent_id=kwargs.pop("torrent_id", "12345"),
        torrent_name=torrent_name,
        category=category,
        author_blob=author_blob,
        **kwargs,
    )


# ─── Multi-category gate (MAM post-2026-08-11 announce format) ──


class TestMultiCategoryGate:
    """MAM announces now carry several content tags per torrent.

    Both category gates are any-match over that list. Matching only the
    first tag would make every verdict depend on MAM's tag ordering,
    which is not a property Seshat controls.
    """

    def test_allowed_when_any_category_matches(self):
        # "Horror" is not allowed, "Science Fiction" is, and Horror
        # sorts first — this is a real announce shape (tid 1262725).
        config = make_config(
            allowed=["Jonathan Maberry"],
            categories=["Ebooks - Science Fiction"],
        )
        announce = make_announce(
            author_blob="Jonathan Maberry",
            category="Ebooks - Horror",
            categories=("Horror", "Science Fiction"),
        )
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"

    def test_skipped_when_no_category_matches(self):
        config = make_config(
            allowed=["Brandon Sanderson"],
            categories=["Ebooks - Science Fiction"],
        )
        announce = make_announce(
            category="Ebooks - Horror",
            categories=("Horror", "Romance"),
        )
        decision = evaluate_announce(announce, config)
        assert decision.action == "skip"
        assert decision.reason == "category_not_allowed"

    def test_excluded_when_any_category_matches(self):
        # The exclusion path matters most: MAM demoted
        # "Erotica/Sexual Content" from a torrent flag to a real
        # category, so this gate is the only lever left for it — and a
        # first-tag-only check would miss it whenever it isn't first.
        config = make_config(
            allowed=["Brandon Sanderson"],
            categories=["Ebooks - Fantasy"],
            excluded_categories=["Ebooks - Erotica/Sexual Content"],
        )
        announce = make_announce(
            category="Ebooks - Fantasy",
            categories=("Fantasy", "Erotica/Sexual Content", "Romance"),
        )
        decision = evaluate_announce(announce, config)
        assert decision.action == "skip"
        assert decision.reason == "category_excluded"

    def test_exclusion_beats_inclusion(self):
        config = make_config(
            allowed=["Brandon Sanderson"],
            categories=["Ebooks - Fantasy"],
            excluded_categories=["Ebooks - Romance"],
        )
        announce = make_announce(
            category="Ebooks - Fantasy",
            categories=("Fantasy", "Romance"),
        )
        decision = evaluate_announce(announce, config)
        assert decision.action == "skip"
        assert decision.reason == "category_excluded"

    def test_empty_categories_falls_back_to_single_field(self):
        # Manually-injected announces and replayed historical rows only
        # ever carry the combined `category` string.
        config = make_config(
            allowed=["Brandon Sanderson"],
            categories=["Ebooks - Fantasy"],
        )
        announce = make_announce(category="Ebooks - Fantasy", categories=())
        assert evaluate_announce(announce, config).action == "allow"

        blocked = make_announce(category="Ebooks - Horror", categories=())
        assert evaluate_announce(blocked, config).reason == "category_not_allowed"


# ─── Category gate ───────────────────────────────────────────


class TestCategoryGate:
    def test_allowed_category_passes(self):
        config = make_config(allowed=["Brandon Sanderson"])
        announce = make_announce(category="Ebooks - Fantasy")
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"

    def test_disallowed_category_skipped(self):
        config = make_config(allowed=["Brandon Sanderson"])
        announce = make_announce(category="Ebooks - Romance")
        decision = evaluate_announce(announce, config)
        assert decision.action == "skip"
        assert decision.reason == "category_not_allowed"

    def test_category_normalized_before_check(self):
        # Different capitalization / separators must still match.
        config = make_config(
            categories=["ebooks fantasy"],
            allowed=["Brandon Sanderson"],
        )
        announce = make_announce(category="EBOOKS_-_FANTASY")
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"

    def test_empty_category_skipped(self):
        config = make_config(allowed=["Brandon Sanderson"])
        announce = make_announce(category="")
        decision = evaluate_announce(announce, config)
        assert decision.action == "skip"
        assert decision.reason == "category_not_allowed"


# ─── Format gate ────────────────────────────────────────────


class TestFormatGate:
    def test_allowed_format_passes(self):
        config = make_config(
            allowed_formats=["ebooks"],
            allowed=["Brandon Sanderson"],
        )
        announce = make_announce(category="Ebooks - Fantasy")
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"

    def test_disallowed_format_skipped(self):
        config = make_config(
            allowed_formats=["audiobooks"],
            allowed=["Brandon Sanderson"],
        )
        announce = make_announce(category="Ebooks - Fantasy")
        decision = evaluate_announce(announce, config)
        assert decision.action == "skip"
        assert decision.reason == "format_not_allowed"

    def test_empty_allowed_formats_accepts_all(self):
        # No format restriction = all formats pass.
        config = make_config(allowed=["Brandon Sanderson"])
        announce = make_announce(category="Comics/Graphic novels - Fantasy")
        # Category won't be in allowed_categories, but format gate passes.
        decision = evaluate_announce(announce, config)
        assert decision.reason != "format_not_allowed"

    def test_excluded_format_skipped(self):
        config = make_config(
            excluded_formats=["comics graphic novels"],
            categories=["Comics/Graphic novels - Fantasy"],
            allowed=["Brandon Sanderson"],
        )
        announce = make_announce(category="Comics/Graphic novels - Fantasy")
        decision = evaluate_announce(announce, config)
        assert decision.action == "skip"
        assert decision.reason == "format_excluded"

    def test_excluded_format_overrides_allowed(self):
        # If a format is in both allowed AND excluded, exclusion wins.
        config = make_config(
            allowed_formats=["ebooks", "audiobooks"],
            excluded_formats=["audiobooks"],
            categories=["AudioBooks - Fantasy"],
            allowed=["Brandon Sanderson"],
        )
        announce = make_announce(category="AudioBooks - Fantasy")
        decision = evaluate_announce(announce, config)
        assert decision.action == "skip"
        assert decision.reason == "format_excluded"

    def test_format_normalized_case_insensitive(self):
        config = make_config(
            allowed_formats=["EBOOKS"],
            allowed=["Brandon Sanderson"],
        )
        announce = make_announce(category="Ebooks - Fantasy")
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"

    def test_format_runs_before_category(self):
        # Format gate fires before category gate — a format-level skip
        # should not produce a category_not_allowed reason.
        config = make_config(
            allowed_formats=["audiobooks"],
            categories=["Ebooks - Fantasy"],
            allowed=["Brandon Sanderson"],
        )
        announce = make_announce(category="Ebooks - Fantasy")
        decision = evaluate_announce(announce, config)
        assert decision.action == "skip"
        assert decision.reason == "format_not_allowed"


# ─── Language gate ──────────────────────────────────────────


class TestLanguageGate:
    def test_allowed_language_passes(self):
        config = make_config(
            allowed_languages=["english"],
            allowed=["Brandon Sanderson"],
        )
        announce = make_announce(language="English")
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"

    def test_disallowed_language_skipped(self):
        config = make_config(
            allowed_languages=["english"],
            allowed=["Brandon Sanderson"],
        )
        announce = make_announce(language="Russian")
        decision = evaluate_announce(announce, config)
        assert decision.action == "skip"
        assert decision.reason == "language_not_allowed"

    def test_empty_allowed_languages_accepts_all(self):
        config = make_config(allowed=["Brandon Sanderson"])
        announce = make_announce(language="Spanish")
        decision = evaluate_announce(announce, config)
        # Should not be blocked by language.
        assert decision.reason != "language_not_allowed"

    def test_multiple_languages_allowed(self):
        config = make_config(
            allowed_languages=["english", "spanish"],
            allowed=["Brandon Sanderson"],
        )
        announce = make_announce(language="Spanish")
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"

    def test_language_case_insensitive(self):
        config = make_config(
            allowed_languages=["english"],
            allowed=["Brandon Sanderson"],
        )
        announce = make_announce(language="ENGLISH")
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"

    def test_empty_language_field_skipped_when_gate_active(self):
        config = make_config(
            allowed_languages=["english"],
            allowed=["Brandon Sanderson"],
        )
        announce = make_announce(language="")
        decision = evaluate_announce(announce, config)
        assert decision.action == "skip"
        assert decision.reason == "language_not_allowed"


# ─── Category exclusion ─────────────────────────────────────


class TestCategoryExclusion:
    def test_excluded_category_skipped(self):
        config = make_config(
            excluded_categories=["Ebooks - Fantasy"],
            allowed=["Brandon Sanderson"],
        )
        announce = make_announce(category="Ebooks - Fantasy")
        decision = evaluate_announce(announce, config)
        assert decision.action == "skip"
        assert decision.reason == "category_excluded"

    def test_non_excluded_category_passes(self):
        config = make_config(
            excluded_categories=["Ebooks - Romance"],
            allowed=["Brandon Sanderson"],
        )
        announce = make_announce(category="Ebooks - Fantasy")
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"

    def test_exclusion_overrides_inclusion(self):
        # Category is in allowed_categories but also in excluded_categories.
        config = make_config(
            categories=["Ebooks - Fantasy"],
            excluded_categories=["Ebooks - Fantasy"],
            allowed=["Brandon Sanderson"],
        )
        announce = make_announce(category="Ebooks - Fantasy")
        decision = evaluate_announce(announce, config)
        assert decision.action == "skip"
        assert decision.reason == "category_excluded"

    def test_format_plus_category_exclusion(self):
        # "Include all ebooks EXCEPT romance" pattern.
        config = make_config(
            allowed_formats=["ebooks"],
            categories=[
                "Ebooks - Fantasy",
                "Ebooks - Romance",
                "Ebooks - Science Fiction",
            ],
            excluded_categories=["Ebooks - Romance"],
            allowed=["Brandon Sanderson"],
        )
        fantasy = make_announce(category="Ebooks - Fantasy")
        romance = make_announce(category="Ebooks - Romance")

        assert evaluate_announce(fantasy, config).action == "allow"
        assert evaluate_announce(romance, config).action == "skip"
        assert evaluate_announce(romance, config).reason == "category_excluded"


# ─── Author detection ────────────────────────────────────────


class TestAuthorDetection:
    def test_author_blob_used_directly(self):
        config = make_config(allowed=["Brandon Sanderson"])
        announce = make_announce(author_blob="Brandon Sanderson")
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"

    def test_no_author_anywhere_skipped(self):
        config = make_config(allowed=["Brandon Sanderson"])
        announce = make_announce(
            author_blob="",
            torrent_name="A Random Title With No Author Field",
        )
        decision = evaluate_announce(announce, config)
        assert decision.action == "skip"
        assert decision.reason == "author_not_detected"

    def test_fallback_extracts_from_torrent_name_with_by(self):
        config = make_config(allowed=["Brandon Sanderson"])
        announce = make_announce(
            author_blob="",
            torrent_name="The Way of Kings by Brandon Sanderson [English / epub]",
        )
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"

    def test_fallback_extracts_mam_announce_format(self):
        config = make_config(allowed=["Brandon Sanderson"])
        announce = make_announce(
            author_blob="",
            torrent_name="",
            description=(
                "New Torrent: The Way of Kings By: Brandon Sanderson "
                "Category: ( Ebooks - Fantasy ) Size: ( 5 MB )"
            ),
        )
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"


# ─── Allow / ignore / unknown classification ─────────────────


class TestAuthorClassification:
    def test_allowed_single_author(self):
        config = make_config(allowed=["Brandon Sanderson"])
        announce = make_announce(author_blob="Brandon Sanderson")
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"
        assert decision.reason == "allowed_author"
        assert decision.matched_author == "Brandon Sanderson"

    def test_ignored_single_author(self):
        config = make_config(ignored=["Stephen King"])
        announce = make_announce(author_blob="Stephen King")
        decision = evaluate_announce(announce, config)
        assert decision.action == "skip"
        assert decision.reason == "ignored_author"
        assert decision.primary_log_author == "Stephen King"

    def test_unknown_single_author(self):
        config = make_config()  # empty allow + ignore lists
        announce = make_announce(author_blob="Some New Author")
        decision = evaluate_announce(announce, config)
        assert decision.action == "skip"
        assert decision.reason == "author_not_allowlisted"
        assert decision.unknown_authors == ("Some New Author",)
        assert decision.primary_log_author == "Some New Author"

    def test_normalization_matches_across_case(self):
        config = make_config(allowed=["BRANDON SANDERSON"])
        announce = make_announce(author_blob="brandon sanderson")
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"

    def test_normalization_handles_calibre_sort_form(self):
        # The Calibre weekly audit will write authors as
        # "Lastname, Firstname"; the normalize layer must reconcile.
        config = make_config(allowed=["Sanderson, Brandon"])
        announce = make_announce(author_blob="Brandon Sanderson")
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"


# ─── Multi-author rules ──────────────────────────────────────


class TestMultiAuthor:
    def test_comma_separated_split(self):
        # The shell script splits on commas — this is a faithful port.
        config = make_config(allowed=["Jason Anspach"])
        announce = make_announce(author_blob="J N Chaney, Jason Anspach")
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"
        assert decision.matched_author == "Jason Anspach"

    def test_and_separated_split(self):
        config = make_config(allowed=["Jason Anspach"])
        announce = make_announce(author_blob="J N Chaney and Jason Anspach")
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"

    def test_ampersand_separated_split(self):
        config = make_config(allowed=["Jason Anspach"])
        announce = make_announce(author_blob="J N Chaney & Jason Anspach")
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"

    def test_slash_separated_split(self):
        config = make_config(allowed=["Jason Anspach"])
        announce = make_announce(author_blob="J N Chaney / Jason Anspach")
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"

    def test_semicolon_separated_split(self):
        config = make_config(allowed=["Jason Anspach"])
        announce = make_announce(author_blob="J N Chaney; Jason Anspach")
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"

    def test_allow_wins_over_ignore(self):
        # If one co-author is allowed and another is ignored, the
        # whole release is allowed. Mirrors the shell script.
        config = make_config(
            allowed=["Jason Anspach"],
            ignored=["J N Chaney"],
        )
        announce = make_announce(author_blob="J N Chaney, Jason Anspach")
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"
        assert decision.matched_author == "Jason Anspach"

    def test_unknown_keeps_release_in_review_queue(self):
        # If one co-author is unknown and one is ignored (none allowed),
        # the release is skipped as `author_not_allowlisted` so the
        # unknown author goes into the weekly review.
        config = make_config(ignored=["J N Chaney"])
        announce = make_announce(author_blob="J N Chaney, Jason Anspach")
        decision = evaluate_announce(announce, config)
        assert decision.action == "skip"
        assert decision.reason == "author_not_allowlisted"
        assert "Jason Anspach" in decision.unknown_authors
        assert "J N Chaney" not in decision.unknown_authors

    def test_all_ignored_marks_as_ignored(self):
        config = make_config(ignored=["J N Chaney", "Jason Anspach"])
        announce = make_announce(author_blob="J N Chaney, Jason Anspach")
        decision = evaluate_announce(announce, config)
        assert decision.action == "skip"
        assert decision.reason == "ignored_author"
        # First-encountered ignored author is the log author.
        assert decision.primary_log_author == "J N Chaney"

    def test_unknown_authors_field_lists_only_unknowns(self):
        config = make_config(
            allowed=["Author A"],
            ignored=["Author B"],
        )
        announce = make_announce(
            author_blob="Author A, Author B, Author C, Author D",
        )
        # Allow short-circuits — Author A matches first.
        decision = evaluate_announce(announce, config)
        assert decision.action == "allow"
        # Authors after the allow hit are not walked, so unknown_authors
        # is empty even though C and D would otherwise be unknown.
        assert decision.unknown_authors == ()

    def test_unknown_authors_collected_when_no_allow_hit(self):
        config = make_config(ignored=["Author B"])
        announce = make_announce(
            author_blob="Author A, Author B, Author C",
        )
        decision = evaluate_announce(announce, config)
        assert decision.action == "skip"
        assert decision.unknown_authors == ("Author A", "Author C")


# ─── Helper: extract_author_blob_from_text ───────────────────


class TestExtractAuthorBlobFromText:
    def test_mam_announce_format(self):
        text = (
            "New Torrent: The Way of Kings By: Brandon Sanderson "
            "Category: ( Ebooks - Fantasy ) Size: ( 5 MB )"
        )
        assert extract_author_blob_from_text(text) == "Brandon Sanderson"

    def test_title_by_format(self):
        text = "The Way of Kings by Brandon Sanderson [English / epub]"
        assert extract_author_blob_from_text(text) == "Brandon Sanderson"

    def test_title_by_format_no_brackets(self):
        text = "The Way of Kings by Brandon Sanderson"
        assert extract_author_blob_from_text(text) == "Brandon Sanderson"

    def test_bare_by_format(self):
        text = "Some Random Text By: Brandon Sanderson | other stuff"
        assert extract_author_blob_from_text(text) == "Brandon Sanderson"

    def test_walks_inputs_in_order(self):
        # First non-empty match wins.
        assert (
            extract_author_blob_from_text("", "by Author One", "by Author Two")
            == "Author One"
        )

    def test_returns_empty_when_no_match(self):
        assert extract_author_blob_from_text("just a random title") == ""
        assert extract_author_blob_from_text("") == ""
        assert extract_author_blob_from_text() == ""


# ─── Helper: split_authors ───────────────────────────────────


class TestSplitAuthors:
    def test_empty_string(self):
        assert split_authors("") == []

    def test_single_author(self):
        assert split_authors("Brandon Sanderson") == ["Brandon Sanderson"]

    def test_comma(self):
        assert split_authors("A, B, C") == ["A", "B", "C"]

    def test_and(self):
        assert split_authors("A and B and C") == ["A", "B", "C"]

    def test_ampersand(self):
        assert split_authors("A & B") == ["A", "B"]

    def test_slash(self):
        assert split_authors("A / B") == ["A", "B"]

    def test_semicolon(self):
        assert split_authors("A; B") == ["A", "B"]

    def test_mixed_separators(self):
        assert split_authors("A, B and C & D") == ["A", "B", "C", "D"]

    def test_strips_whitespace(self):
        assert split_authors("  A  ,  B  ") == ["A", "B"]

    def test_drops_empty_pieces(self):
        assert split_authors("A,, B") == ["A", "B"]
