"""Cover image perceptual hashing tests.

Three layers of coverage:

  1. Pure helpers (synthetic images via Pillow) — no fixture files in
     the repo so we don't have to worry about MAM image licensing.
     The synthetic gradients here hash the same on Pillow 12.3 and 13.0,
     so they give stable expected outputs. Encoded covers can move a bit
     or two between Pillow versions (3 of 120 random JPEG / PNG / WebP
     images moved 2 bits, 12.3 → 13.0), well inside the match band.

  2. Cache helpers (round-trip via `temp_db` fixture) — store, read,
     TTL expiration via time monkeypatching.

  3. Top-level fetch+hash+cache — `_do_get` is mocked so no live MAM
     traffic. Verifies cache-hit shortcut, cache-miss fetch path, and
     graceful None on HTTP/network/decode failure.
"""
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from app.mam import cover_hash


def _make_image_bytes(
    *, width: int = 200, height: int = 300,
    color_seed: int = 0,
) -> bytes:
    """Build a deterministic JPEG of given dimensions for hashing tests.

    Draws geometric shapes (rectangles + ellipses) over a colored
    background. Different `color_seed` values yield visually distinct
    images (positions + colors shift). Same seed yields the same image.

    Why geometry instead of a gradient: pHash latches onto edges /
    structure (DCT low-frequency components from spatial features). A
    smooth gradient has nothing for the hash to anchor on, so tiny
    pixel-pattern shifts at different resolutions blow up the distance.
    Real book covers have title text + author + cover art with edges,
    which is what pHash is designed for.
    """
    bg = (
        (color_seed * 41) % 256,
        (color_seed * 73) % 256,
        (color_seed * 109) % 256,
    )
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)
    # Rectangles + ellipses positioned by seed — same seed = same shapes.
    for i in range(5):
        x0 = (color_seed * 17 + i * 31) % width
        y0 = (color_seed * 23 + i * 37) % height
        x1 = x0 + width // 4
        y1 = y0 + height // 4
        color = (
            (color_seed * 53 + i * 41) % 256,
            (color_seed * 31 + i * 67) % 256,
            (color_seed * 89 + i * 19) % 256,
        )
        if i % 2 == 0:
            draw.rectangle((x0, y0, x1, y1), fill=color)
        else:
            draw.ellipse((x0, y0, x1, y1), fill=color)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


# ─── Pure helpers ────────────────────────────────────────────────


class TestHashImageBytes:
    def test_returns_16_char_hex_string(self):
        h = cover_hash.hash_image_bytes(_make_image_bytes())
        assert isinstance(h, str)
        assert len(h) == 16
        int(h, 16)  # parses as hex

    def test_deterministic_same_input(self):
        data = _make_image_bytes(color_seed=42)
        assert cover_hash.hash_image_bytes(data) == cover_hash.hash_image_bytes(data)

    def test_distinct_images_distinct_hashes(self):
        h1 = cover_hash.hash_image_bytes(_make_image_bytes(color_seed=1))
        h2 = cover_hash.hash_image_bytes(_make_image_bytes(color_seed=99))
        assert h1 != h2

    def test_resolution_invariance(self):
        # Take ONE image and re-encode at half resolution — the way real
        # covers experience size differences (publisher master sized
        # down for MAM CDN). pHash should hash near-identically. This
        # is the property that makes pHash usable for cover matching at
        # all — without it we'd need byte-level equality.
        original = _make_image_bytes(width=400, height=600, color_seed=7)
        with Image.open(BytesIO(original)) as img:
            resized = img.resize((200, 300))
            buf = BytesIO()
            resized.save(buf, format="JPEG", quality=85)
            small = buf.getvalue()
        h_orig = cover_hash.hash_image_bytes(original)
        h_small = cover_hash.hash_image_bytes(small)
        assert cover_hash.hamming_distance(h_orig, h_small) <= 6

    def test_returns_none_on_empty(self):
        assert cover_hash.hash_image_bytes(b"") is None
        assert cover_hash.hash_image_bytes(None) is None  # type: ignore

    def test_returns_none_on_too_small(self):
        # Below _MIN_IMAGE_BYTES (200) — placeholder pixels, not a real cover.
        assert cover_hash.hash_image_bytes(b"x" * 50) is None

    def test_returns_none_on_garbage(self):
        assert cover_hash.hash_image_bytes(b"x" * 1000) is None

    def test_returns_none_on_too_large(self):
        # Above _MAX_IMAGE_BYTES (5MB) — likely scan/dump, not a cover.
        # Use random-ish bytes so PIL doesn't try to decode them anyway.
        assert cover_hash.hash_image_bytes(b"\x00" * (6 * 1024 * 1024)) is None


class TestHashImageFile:
    def test_round_trip_matches_bytes_hash(self, tmp_path):
        data = _make_image_bytes(color_seed=33)
        f = tmp_path / "cover.jpg"
        f.write_bytes(data)
        h_file = cover_hash.hash_image_file(f)
        h_bytes = cover_hash.hash_image_bytes(data)
        assert h_file == h_bytes

    def test_returns_none_on_missing_file(self, tmp_path):
        assert cover_hash.hash_image_file(tmp_path / "does-not-exist.jpg") is None

    def test_returns_none_on_directory(self, tmp_path):
        assert cover_hash.hash_image_file(tmp_path) is None

    def test_returns_none_on_none(self):
        assert cover_hash.hash_image_file(None) is None

    def test_returns_none_on_non_image(self, tmp_path):
        f = tmp_path / "garbage.jpg"
        f.write_bytes(b"definitely not an image" * 200)
        assert cover_hash.hash_image_file(f) is None


class TestHammingDistance:
    def test_identical_hashes_zero(self):
        assert cover_hash.hamming_distance("0" * 16, "0" * 16) == 0

    def test_one_bit_difference_one(self):
        # Single bit differs — Hamming distance 1.
        assert cover_hash.hamming_distance("0000000000000000", "0000000000000001") == 1

    def test_all_bits_difference_max(self):
        # All 64 bits differ.
        assert cover_hash.hamming_distance("0" * 16, "f" * 16) == 64

    def test_none_inputs_return_max(self):
        assert cover_hash.hamming_distance(None, "abcd" * 4) == 64
        assert cover_hash.hamming_distance("abcd" * 4, None) == 64
        assert cover_hash.hamming_distance(None, None) == 64

    def test_empty_inputs_return_max(self):
        assert cover_hash.hamming_distance("", "abcd" * 4) == 64

    def test_malformed_inputs_return_max(self):
        # Non-hex characters, wrong length — biases to no-match.
        assert cover_hash.hamming_distance("zzzz" * 4, "abcd" * 4) == 64
        assert cover_hash.hamming_distance("ab", "abcd" * 4) == 64

    def test_real_cover_distance_shape(self):
        # Two visually distinct synthetic images should land in the
        # "wrong-match" band per our 16-pair validation experiment.
        h1 = cover_hash.hash_image_bytes(_make_image_bytes(color_seed=1))
        h2 = cover_hash.hash_image_bytes(_make_image_bytes(color_seed=99))
        d = cover_hash.hamming_distance(h1, h2)
        # Looser bound than real-world wrong-matches (which were 28+)
        # because synthetic gradients aren't as visually distinct as
        # real cover art. We just need the signal to fire above 0.
        assert d > 5

    def test_returns_native_python_int_not_numpy(self):
        # Regression: imagehash.__sub__ returns numpy.int64, which is
        # comparable to int but NOT JSON-serializable by Pydantic. The
        # production debug-match endpoint failed with
        # PydanticSerializationError when cover_check.distance carried
        # a numpy type. Coercion in `hamming_distance` must use type(int).
        import json

        h1 = cover_hash.hash_image_bytes(_make_image_bytes(color_seed=3))
        h2 = cover_hash.hash_image_bytes(_make_image_bytes(color_seed=4))
        d = cover_hash.hamming_distance(h1, h2)
        # exact type check (isinstance would pass for numpy.int64 too
        # because np.int64 is a subclass of int on some platforms).
        assert type(d) is int
        # Must round-trip through JSON cleanly — this is what Pydantic
        # ultimately attempts.
        json.dumps({"distance": d})


# ─── Cache helpers (persistent, global DB) ───────────────────────


class TestCachePersistence:
    @pytest.mark.asyncio
    async def test_round_trip(self, temp_db, monkeypatch):
        monkeypatch.setattr(cover_hash, "APP_DB_PATH", temp_db)
        await cover_hash.store_cover_hash(
            "12345", "abcdef0123456789",
            width=666, height=1000, bytes_count=12345,
        )
        h = await cover_hash.get_cached_cover_hash("12345")
        assert h == "abcdef0123456789"

    @pytest.mark.asyncio
    async def test_miss_returns_none(self, temp_db, monkeypatch):
        monkeypatch.setattr(cover_hash, "APP_DB_PATH", temp_db)
        assert await cover_hash.get_cached_cover_hash("does-not-exist") is None

    @pytest.mark.asyncio
    async def test_replace_on_re_store(self, temp_db, monkeypatch):
        monkeypatch.setattr(cover_hash, "APP_DB_PATH", temp_db)
        await cover_hash.store_cover_hash("99", "0000000000000000")
        await cover_hash.store_cover_hash("99", "ffffffffffffffff")
        assert await cover_hash.get_cached_cover_hash("99") == "ffffffffffffffff"

    @pytest.mark.asyncio
    async def test_empty_inputs_silent_noop(self, temp_db, monkeypatch):
        monkeypatch.setattr(cover_hash, "APP_DB_PATH", temp_db)
        # Neither stores nor raises.
        await cover_hash.store_cover_hash("", "abcd")
        await cover_hash.store_cover_hash("99", "")
        assert await cover_hash.get_cached_cover_hash("99") is None

    @pytest.mark.asyncio
    async def test_get_with_empty_torrent_id(self, temp_db, monkeypatch):
        monkeypatch.setattr(cover_hash, "APP_DB_PATH", temp_db)
        assert await cover_hash.get_cached_cover_hash("") is None
        assert await cover_hash.get_cached_cover_hash(None) is None  # type: ignore


class TestCacheTTL:
    @pytest.mark.asyncio
    async def test_fresh_returned(self, temp_db, monkeypatch):
        monkeypatch.setattr(cover_hash, "APP_DB_PATH", temp_db)
        await cover_hash.store_cover_hash("99", "abcd" * 4)
        assert await cover_hash.get_cached_cover_hash("99") == "abcd" * 4

    @pytest.mark.asyncio
    async def test_stale_returns_none(self, temp_db, monkeypatch):
        monkeypatch.setattr(cover_hash, "APP_DB_PATH", temp_db)
        await cover_hash.store_cover_hash("99", "abcd" * 4)
        # Advance the clock past TTL — get_cached should return None
        # so the caller re-fetches.
        real_time = cover_hash.time.time
        future = real_time() + cover_hash._CACHE_TTL_SEC + 1
        monkeypatch.setattr(cover_hash.time, "time", lambda: future)
        assert await cover_hash.get_cached_cover_hash("99") is None


# ─── Fetch + hash + cache (top-level) ────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes):
        self.status_code = status_code
        self.content = content


class TestFetchAndHashMamCover:
    @pytest.mark.asyncio
    async def test_cache_hit_short_circuits_fetch(
        self, temp_db, monkeypatch
    ):
        monkeypatch.setattr(cover_hash, "APP_DB_PATH", temp_db)
        await cover_hash.store_cover_hash("777", "feed" * 4)

        called = {"n": 0}

        async def _fake_get(url, token=None, timeout=15):
            called["n"] += 1
            return _FakeResponse(200, b"")

        monkeypatch.setattr(cover_hash, "_do_get", _fake_get)
        h = await cover_hash.fetch_and_hash_mam_cover("777", "tok")
        assert h == "feed" * 4
        assert called["n"] == 0  # cache hit, no HTTP

    @pytest.mark.asyncio
    async def test_cache_miss_fetches_hashes_persists(
        self, temp_db, monkeypatch
    ):
        monkeypatch.setattr(cover_hash, "APP_DB_PATH", temp_db)
        data = _make_image_bytes(color_seed=11)
        expected_hash = cover_hash.hash_image_bytes(data)

        async def _fake_get(url, token=None, timeout=15):
            return _FakeResponse(200, data)

        monkeypatch.setattr(cover_hash, "_do_get", _fake_get)
        h = await cover_hash.fetch_and_hash_mam_cover("888", "tok")
        assert h == expected_hash
        # Persisted: subsequent call returns from cache.
        cached = await cover_hash.get_cached_cover_hash("888")
        assert cached == expected_hash

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_inputs(self, monkeypatch):
        called = {"n": 0}

        async def _fake_get(url, token=None, timeout=15):
            called["n"] += 1
            return _FakeResponse(200, b"")

        monkeypatch.setattr(cover_hash, "_do_get", _fake_get)
        assert await cover_hash.fetch_and_hash_mam_cover("", "tok") is None
        assert await cover_hash.fetch_and_hash_mam_cover("777", "") is None
        assert called["n"] == 0

    @pytest.mark.asyncio
    async def test_returns_none_on_http_error(self, temp_db, monkeypatch):
        monkeypatch.setattr(cover_hash, "APP_DB_PATH", temp_db)

        async def _fake_get(url, token=None, timeout=15):
            return _FakeResponse(403, b"")

        monkeypatch.setattr(cover_hash, "_do_get", _fake_get)
        h = await cover_hash.fetch_and_hash_mam_cover("999", "tok")
        assert h is None
        # Must NOT persist on failure — next attempt should still try.
        assert await cover_hash.get_cached_cover_hash("999") is None

    @pytest.mark.asyncio
    async def test_returns_none_on_network_failure(self, temp_db, monkeypatch):
        monkeypatch.setattr(cover_hash, "APP_DB_PATH", temp_db)

        async def _fake_get(url, token=None, timeout=15):
            raise ConnectionError("network down")

        monkeypatch.setattr(cover_hash, "_do_get", _fake_get)
        h = await cover_hash.fetch_and_hash_mam_cover("999", "tok")
        assert h is None

    @pytest.mark.asyncio
    async def test_returns_none_on_undecodable_bytes(self, temp_db, monkeypatch):
        monkeypatch.setattr(cover_hash, "APP_DB_PATH", temp_db)

        async def _fake_get(url, token=None, timeout=15):
            return _FakeResponse(200, b"not an image" * 100)

        monkeypatch.setattr(cover_hash, "_do_get", _fake_get)
        h = await cover_hash.fetch_and_hash_mam_cover("999", "tok")
        assert h is None
