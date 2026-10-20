"""G182: a source skipped while backing off is retried for the pending
review once the backoff ends, merged as if it had never been skipped.

The trial (2026-10-12 → 16): ~22 of 72 grabs skipped Amazon inside a
cooldown. Nothing here reaches a source: the sources are fakes.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import state
from app.database import get_db
from app.metadata.enricher import EnrichmentConfig, MetadataEnricher
from app.metadata.record import MetaRecord
from app.metadata.sources.base import MetaSource
from app.orchestrator import enrich_retry
from app.storage import grabs as grabs_storage
from app.storage import review_queue as review_storage

PRIORITY = ("mam", "goodreads", "amazon", "hardcover", "kobo", "ibdb")


class _Fake(MetaSource):
    def __init__(self, name: str, record: MetaRecord | None, *, backing_off: bool = False):
        super().__init__(rate_limit=0)
        self.name = name
        self._record = record
        self._backing_off = backing_off
        self.calls = 0

    def backing_off(self) -> bool:
        return self._backing_off

    async def search_book(self, title, author, **_):
        self.calls += 1
        return self._record


def _amazon_record(**kw) -> MetaRecord:
    return MetaRecord(**{
        "title": "The Tale", "authors": ["Author"], "source": "amazon",
        "series": "Tales", "series_index": 2.0, "publisher": "Big House",
        "page_count": 412, **kw,
    })


def _enriched(**kw) -> dict:
    """What the review stored after Amazon was skipped: Hardcover and ibdb
    filled the gaps."""
    return {
        "title": "The Tale", "authors": ["Author"], "series": None, "series_index": None,
        "publisher": "Small Press", "page_count": None, "description": "Short.",
        "field_sources": {"title": "mam", "authors": "mam", "publisher": "ibdb",
                          "description": "hardcover"},
        "source_log": [
            {"source": "mam", "confidence": 1.0, "status": "matched"},
            {"source": "amazon", "confidence": None, "status": "soft_blocked"},
            {"source": "hardcover", "confidence": 0.9, "status": "matched"},
        ],
        **kw,
    }


async def _review(metadata: dict, *, status: str = "pending") -> int:
    db = await get_db()
    try:
        gid = await grabs_storage.create_grab(
            db, announce_id=None, mam_torrent_id="1277999", torrent_name="The Tale",
            category="Ebooks - Fantasy", author_blob="Author",
            state=grabs_storage.STATE_DOWNLOADED,
        )
        rid = await review_storage.create_entry(
            db, grab_id=gid, pipeline_run_id=None, staged_path="", book_filename="tale.epub",
            book_format="epub", metadata=metadata, library_slug="calibre-library",
        )
        if status != "pending":
            await review_storage.set_status(db, rid, status)
        return rid
    finally:
        await db.close()


async def _metadata(rid: int) -> dict:
    db = await get_db()
    try:
        return (await review_storage.get_entry(db, rid)).metadata
    finally:
        await db.close()


@pytest.fixture
def sources(monkeypatch):
    def install(*fakes: _Fake):
        enricher = MetadataEnricher(
            EnrichmentConfig(enabled=True, priority=PRIORITY, accept_confidence=0.8),
            sources=list(fakes), audiobook_sources=list(fakes),
        )
        monkeypatch.setattr(state, "dispatcher", SimpleNamespace(metadata_enricher=enricher))
        monkeypatch.setattr(enrich_retry, "_still_backing_off", lambda source: False)
    return install


def _base_metadata(**top) -> dict:
    return {"title": "The Tale", "author": "Author", "publisher": "Small Press",
            "description": "Short.", "series": None, "enriched": _enriched(), **top}


class TestRetry:
    async def test_the_skipped_source_merges_as_if_it_had_run(self, temp_db, sources):
        amazon = _Fake("amazon", _amazon_record())
        sources(amazon)
        rid = await _review(_base_metadata())

        assert await enrich_retry.retry_skipped_sources() == 1

        md = await _metadata(rid)
        e = md["enriched"]
        # Amazon outranks ibdb: its publisher replaces ibdb's; it fills the gaps.
        assert e["publisher"] == "Big House" and e["field_sources"]["publisher"] == "amazon"
        assert (e["series"], e["series_index"], e["page_count"]) == ("Tales", 2.0, 412)
        # MAM's title stays (exact-ID outranks every source).
        assert e["field_sources"]["title"] == "mam"
        # Top-level fields that came from enrichment follow it.
        assert md["publisher"] == "Big House" and md["series"] == "Tales"
        entry = next(x for x in e["source_log"] if x["source"] == "amazon")
        assert entry["status"] == "matched" and entry["retried"] is True

    async def test_once_only(self, temp_db, sources):
        amazon = _Fake("amazon", _amazon_record())
        sources(amazon)
        await _review(_base_metadata())
        await enrich_retry.retry_skipped_sources()
        assert await enrich_retry.retry_skipped_sources() == 0
        assert amazon.calls == 1

    async def test_a_field_you_edited_stays(self, temp_db, sources):
        sources(_Fake("amazon", _amazon_record()))
        rid = await _review(_base_metadata(publisher="My Own Edit"))
        await enrich_retry.retry_skipped_sources()
        md = await _metadata(rid)
        assert md["publisher"] == "My Own Edit"
        assert md["enriched"]["publisher"] == "Big House"

    async def test_a_lower_ranked_source_never_replaces(self, temp_db, sources):
        """Kobo ranks under Hardcover: it fills only what is empty."""
        kobo = _Fake("kobo", MetaRecord(title="The Tale", authors=["Author"], source="kobo",
                                         description="x" * 3, page_count=300,
                                         publisher="Kobo Imprint"))
        sources(kobo)
        enriched = _enriched()
        enriched["source_log"][1]["source"] = "kobo"
        enriched["field_sources"]["publisher"] = "hardcover"
        rid = await _review(_base_metadata(enriched=enriched))
        await enrich_retry.retry_skipped_sources()
        e = (await _metadata(rid))["enriched"]
        assert e["description"] == "Short." and e["page_count"] == 300
        assert e["publisher"] == "Small Press"         # Hardcover's, kept

    async def test_still_backing_off_waits(self, temp_db, sources, monkeypatch):
        amazon = _Fake("amazon", _amazon_record(), backing_off=True)
        sources(amazon)
        rid = await _review(_base_metadata())
        assert await enrich_retry.retry_skipped_sources() == 0
        assert amazon.calls == 0
        entry = next(x for x in (await _metadata(rid))["enriched"]["source_log"]
                     if x["source"] == "amazon")
        assert entry["status"] == "soft_blocked" and "retried" not in entry

    async def test_nothing_reruns_after_a_decision(self, temp_db, sources):
        amazon = _Fake("amazon", _amazon_record())
        sources(amazon)
        await _review(_base_metadata(), status="approved")
        assert await enrich_retry.retry_skipped_sources() == 0
        assert amazon.calls == 0

    async def test_no_match_is_recorded_and_not_retried_again(self, temp_db, sources):
        amazon = _Fake("amazon", None)
        sources(amazon)
        rid = await _review(_base_metadata())
        assert await enrich_retry.retry_skipped_sources() == 1
        md = await _metadata(rid)
        entry = next(x for x in md["enriched"]["source_log"] if x["source"] == "amazon")
        assert entry["status"] == "no_result" and entry["retried"] is True
        assert md["publisher"] == "Small Press"
        assert await enrich_retry.retry_skipped_sources() == 0

    async def test_another_volume_never_merges_on_retry(self, temp_db, sources):
        sources(_Fake("amazon", _amazon_record(title="The Tale: Volume 3")))
        md = _base_metadata(title="The Tale Vol 2")
        rid = await _review(md)
        await enrich_retry.retry_skipped_sources()
        after = await _metadata(rid)
        entry = next(x for x in after["enriched"]["source_log"] if x["source"] == "amazon")
        assert entry["status"] == "other_volume"
        assert after["enriched"]["publisher"] == "Small Press"


    async def test_amazon_still_cooling_down_sends_nothing(self, temp_db, sources, monkeypatch):
        """Amazon has no pre-call hook (its cooldown is per page kind): the
        retry asks the cooldown itself before building the search."""
        amazon = _Fake("amazon", _amazon_record())
        sources(amazon)
        monkeypatch.setattr(enrich_retry, "_still_backing_off", lambda source: source == "amazon")
        await _review(_base_metadata())
        assert await enrich_retry.retry_skipped_sources() == 0
        assert amazon.calls == 0

    async def test_a_review_decided_meanwhile_is_left_alone(self, temp_db, sources):
        amazon = _Fake("amazon", _amazon_record())
        sources(amazon)
        rid = await _review(_base_metadata(), status="approved")
        assert await enrich_retry._retry_one(state.dispatcher.metadata_enricher, rid, "amazon") is False
        assert amazon.calls == 0


class TestRemerge:
    def test_mams_exact_match_outranks_every_source(self):
        """Even when MAM isn't in the configured list (it's put first at
        run time when the grab has a torrent ID)."""
        out, changed = enrich_retry.remerge(
            {"title": "MAM's Title", "field_sources": {"title": "mam"}},
            {"title": "Another Title"}, "goodreads", ["goodreads", "amazon"],
        )
        assert out["title"] == "MAM's Title" and changed == []

    def test_description_keeps_the_longer_text(self):
        out, changed = enrich_retry.remerge(
            {"description": "Long enough text.", "field_sources": {"description": "mam"}},
            {"description": "Short"}, "amazon", list(PRIORITY),
        )
        assert out["description"] == "Long enough text." and changed == []

    def test_an_unranked_field_source_is_outranked(self):
        out, changed = enrich_retry.remerge(
            {"publisher": "X", "field_sources": {}}, {"publisher": "Y"}, "ibdb", list(PRIORITY),
        )
        assert out["publisher"] == "Y" and changed == ["publisher"]


class TestEnricherMarksBackoffSkips:
    async def test_a_source_backing_off_is_skipped_as_soft_blocked(self):
        kobo = _Fake("kobo", MetaRecord(title="T", authors=["A"], source="kobo"), backing_off=True)
        enricher = MetadataEnricher(EnrichmentConfig(enabled=True), sources=[kobo])
        assert await enricher.enrich(title="T", author="A") is None
        assert kobo.calls == 0
        assert enricher.last_source_log == [
            {"source": "kobo", "confidence": None, "status": "soft_blocked"},
        ]

    async def test_a_refusal_inside_the_call_is_soft_blocked_not_no_result(self):
        class _Refused(_Fake):
            def skipped_for_backoff(self) -> bool:
                return True

        amazon = _Refused("amazon", None)
        enricher = MetadataEnricher(EnrichmentConfig(enabled=True), sources=[amazon])
        await enricher.enrich(title="T", author="A")
        assert enricher.last_source_log[0]["status"] == "soft_blocked"
