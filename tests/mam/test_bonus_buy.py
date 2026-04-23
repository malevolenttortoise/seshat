"""
Unit tests for the MAM bonusBuy.php wrappers.

Covers URL construction (spendtype / amount / torrentid / cache-buster
shape), response parsing (success + failure), input validation, and the
user_status cache-warming side effect. Every request goes through the
FakeMAM fixture — no real MAM traffic.
"""
from __future__ import annotations

import time

import pytest

from app.mam import bonus_buy, cookie as cookie_module
from app.mam.bonus_buy import (
    BP_PER_PERSONAL_FL,
    BP_PER_UPLOAD_GB,
    BP_PER_VIP_WEEK,
    BuyResult,
    buy_personal_freeleech,
    buy_upload_credit,
    buy_vip,
)
from app.mam.user_status import (
    UserStatus,
    _cache as _user_status_cache,
    invalidate_cache,
)


@pytest.fixture(autouse=True)
def _clear_user_status_cache():
    invalidate_cache()
    yield
    invalidate_cache()


def _bonus_requests(fake_mam):
    return [r for r in fake_mam.requests if "bonusBuy.php" in str(r.url)]


# ─── Happy paths ────────────────────────────────────────────


class TestBuyVipHappyPath:
    async def test_returns_successful_buy_result(self, fake_mam):
        result = await buy_vip(4, token="tok")
        assert isinstance(result, BuyResult)
        assert result.success is True
        assert result.message == "ok"

    async def test_parses_fresh_user_state(self, fake_mam):
        result = await buy_vip(4, token="tok")
        assert result.new_seedbonus == pytest.approx(26512.091)
        assert result.new_uploaded_bytes == 9094496082151
        assert result.new_downloaded_bytes == 96557899
        assert result.new_ratio == pytest.approx(94186.97)

    async def test_echoes_amount_from_response(self, fake_mam):
        # Default fake response body echoes amount=50 (upload-sized); that's
        # fine — the test just confirms we surface whatever MAM sends back,
        # not that we reconcile it against the request.
        result = await buy_vip(4, token="tok")
        assert result.amount_echo == 50

    async def test_url_has_spendtype_vip_and_duration(self, fake_mam):
        # MAM's VIP endpoint uses `duration=`, NOT `amount=`. Passing
        # `amount=` silently 404s. Guard that regression.
        await buy_vip(8, token="tok")
        urls = [str(r.url) for r in _bonus_requests(fake_mam)]
        assert len(urls) == 1
        assert "spendtype=VIP" in urls[0]
        assert "duration=8" in urls[0]
        assert "amount=" not in urls[0]

    async def test_max_weeks_accepted(self, fake_mam):
        await buy_vip("max", token="tok")
        urls = [str(r.url) for r in _bonus_requests(fake_mam)]
        assert "duration=max" in urls[0]

    async def test_request_method_is_get(self, fake_mam):
        await buy_vip(4, token="tok")
        assert _bonus_requests(fake_mam)[0].method == "GET"

    async def test_cookie_header_attached(self, fake_mam):
        await buy_vip(4, token="my_session")
        assert "my_session" in fake_mam.cookies_seen()

    async def test_includes_cache_buster(self, fake_mam):
        await buy_vip(4, token="tok")
        url = str(_bonus_requests(fake_mam)[0].url)
        assert "_=" in url  # jQuery-style millisecond cache-buster


class TestBuyUploadHappyPath:
    async def test_success_with_int_gb(self, fake_mam):
        result = await buy_upload_credit(50, token="tok")
        assert result.success is True
        urls = [str(r.url) for r in _bonus_requests(fake_mam)]
        assert "spendtype=upload" in urls[0]
        assert "amount=50" in urls[0]

    async def test_success_with_float_gb(self, fake_mam):
        # Fractional amounts above the 50 GB floor must round-trip
        # through urlencode without being quantized. Sub-50 GB is
        # MAM-rejected, so we use 50.5 here instead of 2.5.
        result = await buy_upload_credit(50.5, token="tok")
        assert result.success is True
        urls = [str(r.url) for r in _bonus_requests(fake_mam)]
        assert "amount=50.5" in urls[0]

    async def test_rejects_below_minimum_floor(self, fake_mam):
        from app.mam.bonus_buy import MIN_UPLOAD_GB
        # Live MAM capture 2026-04-24: `"Automated spenders are limited
        # to buying at least 50 GB of upload at a time, due to log spam"`.
        with pytest.raises(ValueError, match="at least"):
            await buy_upload_credit(1, token="tok")
        with pytest.raises(ValueError, match="at least"):
            await buy_upload_credit(MIN_UPLOAD_GB - 0.01, token="tok")
        assert _bonus_requests(fake_mam) == []


class TestBuyPersonalFreeleechHappyPath:
    async def test_success_with_torrent_id(self, fake_mam):
        result = await buy_personal_freeleech("965093", token="tok")
        assert result.success is True

    async def test_url_has_spendtype_personalfl_and_torrentid(self, fake_mam):
        await buy_personal_freeleech("965093", token="tok")
        urls = [str(r.url) for r in _bonus_requests(fake_mam)]
        assert "spendtype=personalFL" in urls[0]
        assert "torrentid=965093" in urls[0]

    async def test_url_embeds_timestamp_in_path(self, fake_mam):
        # Personal-FL is the one spendtype where MAM wants the epoch-ms
        # cache-buster duplicated as a trailing path segment. The `timestamp`
        # query param carries the same value.
        before_ms = int(time.time() * 1000) - 5
        await buy_personal_freeleech("965093", token="tok")
        url = str(_bonus_requests(fake_mam)[0].url)
        # Path is /json/bonusBuy.php/<ts_ms>. Strip the query and check
        # the last segment parses as an epoch-ms near "now".
        path_only = url.split("?", 1)[0]
        last_segment = path_only.rstrip("/").rsplit("/", 1)[-1]
        assert last_segment.isdigit()
        assert int(last_segment) >= before_ms
        # AND the same value appears as the `timestamp` query param.
        query = url.split("?", 1)[1]
        assert f"timestamp={last_segment}" in query

    async def test_personalfl_cost_constant_is_50k(self):
        # Sanity check on the hardcoded pricing — if MAM ever changes
        # this, the scheduler / router need to be updated too, so a
        # regression here is a prompt for conscious review.
        assert BP_PER_PERSONAL_FL == 50000


# ─── Input validation ──────────────────────────────────────


class TestInputValidation:
    async def test_buy_vip_rejects_odd_weeks(self, fake_mam):
        with pytest.raises(ValueError, match="4, 8, 12"):
            await buy_vip(7, token="tok")
        assert _bonus_requests(fake_mam) == []

    async def test_buy_vip_accepts_4_8_12(self, fake_mam):
        for weeks in (4, 8, 12):
            await buy_vip(weeks, token="tok")
        assert len(_bonus_requests(fake_mam)) == 3

    async def test_buy_upload_rejects_zero(self, fake_mam):
        with pytest.raises(ValueError, match="positive"):
            await buy_upload_credit(0, token="tok")

    async def test_buy_upload_rejects_negative(self, fake_mam):
        with pytest.raises(ValueError, match="positive"):
            await buy_upload_credit(-5, token="tok")

    async def test_buy_personal_fl_rejects_empty_torrent_id(self, fake_mam):
        with pytest.raises(ValueError, match="torrent_id"):
            await buy_personal_freeleech("", token="tok")

    async def test_buy_personal_fl_rejects_whitespace_only(self, fake_mam):
        with pytest.raises(ValueError, match="torrent_id"):
            await buy_personal_freeleech("   ", token="tok")


# ─── MAM-side failures never raise ────────────────────────


class TestFailureHandling:
    async def test_mam_success_false_returns_failure_result(self, fake_mam):
        fake_mam.bonus_buy.body = b'{"success":false,"error":"Not enough bonus, s1"}'
        result = await buy_upload_credit(50, token="tok")
        assert result.success is False
        assert "Not enough bonus" in result.message
        assert result.new_seedbonus is None
        # The raw payload is preserved so audit rows can store the error code.
        assert result.raw == {"success": False, "error": "Not enough bonus, s1"}

    async def test_http_403_returns_failure_result(self, fake_mam):
        fake_mam.bonus_buy.status = 403
        fake_mam.bonus_buy.body = b"forbidden"
        result = await buy_vip(4, token="tok")
        assert result.success is False
        assert "HTTP 403" in result.message

    async def test_http_500_returns_failure_result(self, fake_mam):
        fake_mam.bonus_buy.status = 500
        fake_mam.bonus_buy.body = b"internal"
        result = await buy_upload_credit(50, token="tok")
        assert result.success is False

    async def test_invalid_json_returns_failure_result(self, fake_mam):
        fake_mam.bonus_buy.body = b"not json at all"
        result = await buy_vip(4, token="tok")
        assert result.success is False
        assert "invalid JSON" in result.message

    async def test_non_dict_response_returns_failure_result(self, fake_mam):
        fake_mam.bonus_buy.body = b'"a bare string"'
        result = await buy_vip(4, token="tok")
        assert result.success is False
        assert "unexpected response shape" in result.message


# ─── User-status cache warming ────────────────────────────


class TestCacheWarming:
    def _seed_cache(self, token: str) -> UserStatus:
        """Populate the cache with a plausible baseline."""
        baseline = UserStatus(
            ratio=2.0,
            wedges=5,
            seedbonus=100.0,
            classname="Power User",
            username="testuser",
            uid=999,
            uploaded_bytes=1_000_000_000,
            downloaded_bytes=500_000_000,
        )
        # Match user_status._cache_key behaviour — key is the first 16 chars.
        from app.mam.user_status import _cache_key
        _user_status_cache[_cache_key(token)] = (
            __import__("time").monotonic(),
            baseline,
        )
        return baseline

    async def test_successful_buy_warms_cached_status(self, fake_mam):
        baseline = self._seed_cache("tok")
        await buy_upload_credit(50, token="tok")

        # Read through get_user_status: it should return the warmed
        # values without hitting the MAM user_status endpoint.
        from app.mam.user_status import get_user_status
        fresh = await get_user_status(token="tok")
        assert fresh.seedbonus == pytest.approx(26512.091)
        assert fresh.uploaded_bytes == 9094496082151
        assert fresh.downloaded_bytes == 96557899
        assert fresh.ratio == pytest.approx(94186.97)
        # Fields MAM doesn't echo back are preserved from the baseline.
        assert fresh.wedges == baseline.wedges
        assert fresh.classname == baseline.classname
        assert fresh.username == baseline.username
        assert fresh.uid == baseline.uid
        # Cache was warmed, so jsonLoad.php wasn't touched.
        assert not any("jsonLoad.php" in str(r.url) for r in fake_mam.requests)

    async def test_buy_without_prior_cache_is_noop(self, fake_mam):
        # No baseline seeded — warming should silently skip (we can't
        # synthesize a full UserStatus from a partial buy response).
        await buy_upload_credit(50, token="tok")
        from app.mam.user_status import _cache_key
        assert _cache_key("tok") not in _user_status_cache

    async def test_failed_buy_does_not_warm_cache(self, fake_mam):
        baseline = self._seed_cache("tok")
        fake_mam.bonus_buy.body = b'{"success":false,"error":"Not enough bonus, s1"}'
        await buy_upload_credit(50, token="tok")
        # Cache unchanged — baseline values still present.
        from app.mam.user_status import get_user_status
        fresh = await get_user_status(token="tok")
        assert fresh.seedbonus == baseline.seedbonus


# ─── Ratio-field parsing ──────────────────────────────────


class TestRatioParsing:
    async def test_parses_dict_shape_from_bonus_buy(self, fake_mam):
        # Default body already has the dict shape; just confirm parsing.
        result = await buy_vip(4, token="tok")
        assert result.new_ratio == pytest.approx(94186.97)

    async def test_parses_bare_float_shape(self, fake_mam):
        fake_mam.bonus_buy.body = (
            b'{"success":true,"type":"upload","amount":1,'
            b'"seedbonus":100.0,"uploaded":1,"downloaded":1,"ratio":3.5}'
        )
        result = await buy_upload_credit(50, token="tok")
        assert result.new_ratio == pytest.approx(3.5)

    async def test_missing_ratio_yields_none(self, fake_mam):
        fake_mam.bonus_buy.body = (
            b'{"success":true,"type":"upload","amount":1,'
            b'"seedbonus":100.0,"uploaded":1,"downloaded":1}'
        )
        result = await buy_upload_credit(50, token="tok")
        assert result.new_ratio is None
        assert result.success is True


# ─── Pricing sanity ───────────────────────────────────────


class TestPricingConstants:
    def test_upload_price_is_500_per_gb(self):
        assert BP_PER_UPLOAD_GB == 500

    def test_vip_price_is_1250_per_week(self):
        assert BP_PER_VIP_WEEK == 1250


# ─── Module export surface ────────────────────────────────


class TestModuleSurface:
    def test_public_api(self):
        # Guard against accidental renames / removals — commit 2 (economy.py)
        # and commit 6 (routers/economy.py) both import from here.
        assert callable(bonus_buy.buy_vip)
        assert callable(bonus_buy.buy_upload_credit)
        assert callable(bonus_buy.buy_personal_freeleech)
        assert bonus_buy.BuyResult is BuyResult

    def test_uses_www_subdomain(self):
        # bonusBuy.php is only served from www — `t.` returns 404 for
        # every spendtype. Guard that choice so a future refactor
        # that "unifies" MAM hosts doesn't silently break the feature.
        assert bonus_buy._BONUS_BUY_URL.startswith(
            "https://www.myanonamouse.net/json/"
        )


# ─── Never raises on network errors ───────────────────────


class TestNeverRaises:
    async def test_network_exception_returns_failure_result(self, monkeypatch):
        # Simulate a network-layer exception before we can even get a
        # response object. Patch cookie._do_get to raise, and confirm
        # buy_vip surfaces that as BuyResult rather than bubbling.
        async def boom(*args, **kwargs):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(cookie_module, "_do_get", boom)
        # bonus_buy binds _do_get at import, so monkeypatch its local ref too.
        monkeypatch.setattr(bonus_buy, "_do_get", boom)

        result = await buy_vip(4, token="tok")
        assert result.success is False
        assert "connection refused" in result.message


# ─── Dry-run mode ──────────────────────────────────────────


@pytest.fixture
def dry_run_enabled(monkeypatch):
    """Flip the dry-run module flag via monkeypatch.

    Simpler than wiring an isolated settings.json for these tests —
    the production path reads `mam_economy_dry_run` through
    `load_settings()`, but the private `_is_dry_run` helper is the
    single choke point, so patching it directly exercises the same
    branches in the buy_* functions without pulling in settings
    infrastructure.
    """
    monkeypatch.setattr(bonus_buy, "_is_dry_run", lambda: True)


class TestDryRun:
    async def test_vip_returns_dry_run_result_no_http(
        self, fake_mam, dry_run_enabled
    ):
        result = await buy_vip(4, token="tok")
        assert result.success is True
        assert result.dry_run is True
        assert "[DRY RUN]" in result.message
        # Dry-run must NOT hit the fake MAM at all — that's the
        # whole point of the flag.
        assert _bonus_requests(fake_mam) == []

    async def test_vip_dry_run_includes_expected_cost(
        self, fake_mam, dry_run_enabled
    ):
        result = await buy_vip(8, token="tok")
        # 8 weeks × 1250 BP = 10,000 BP
        assert f"{8 * BP_PER_VIP_WEEK:,}" in result.message

    async def test_vip_dry_run_max_weeks_has_unknown_cost(
        self, fake_mam, dry_run_enabled
    ):
        # "max" can't be priced client-side, so the cost note reflects that.
        result = await buy_vip("max", token="tok")
        assert result.success is True
        assert "unknown BP" in result.message

    async def test_upload_returns_dry_run_result(
        self, fake_mam, dry_run_enabled
    ):
        result = await buy_upload_credit(50, token="tok")
        assert result.success is True
        assert result.dry_run is True
        # 50 GB × 500 BP = 25,000 BP
        assert f"{50 * BP_PER_UPLOAD_GB:,}" in result.message
        assert _bonus_requests(fake_mam) == []

    async def test_personal_fl_returns_dry_run_result(
        self, fake_mam, dry_run_enabled
    ):
        result = await buy_personal_freeleech("123", token="tok")
        assert result.success is True
        assert result.dry_run is True
        assert f"{BP_PER_PERSONAL_FL:,}" in result.message
        assert _bonus_requests(fake_mam) == []

    async def test_dry_run_still_validates_inputs(
        self, fake_mam, dry_run_enabled
    ):
        # Input validation runs BEFORE the dry-run check — a bad
        # caller argument is still a programming error regardless
        # of whether we'd have hit MAM.
        with pytest.raises(ValueError):
            await buy_vip(7, token="tok")
        with pytest.raises(ValueError):
            await buy_upload_credit(-1, token="tok")
        with pytest.raises(ValueError):
            await buy_personal_freeleech("", token="tok")

    async def test_dry_run_result_leaves_new_fields_none(
        self, fake_mam, dry_run_enabled
    ):
        # Audit rows rely on new_seedbonus=None to compute cost=None
        # so dry-run rows don't show a bogus cost in the history.
        result = await buy_vip(4, token="tok")
        assert result.new_seedbonus is None
        assert result.new_uploaded_bytes is None
        assert result.new_ratio is None
