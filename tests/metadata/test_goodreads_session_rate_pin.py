"""The shared Goodreads session can't be pinned at no delay (2026-10-08
live check, audit issue 12): the cache reader's inner source passed
rate_limit=0.0, and when a scan was the first Goodreads caller after a
restart every request went out back to back."""
import pytest

from app.metadata import goodreads_session


@pytest.fixture(autouse=True)
def _fresh_session():
    goodreads_session.reset_session_for_tests()
    yield
    goodreads_session.reset_session_for_tests()


@pytest.mark.parametrize("rate", [0.0, -1.0, None])
async def test_no_rate_means_the_default(rate):
    session = await goodreads_session.get_session(rate_limit=rate)
    assert session.rate_limit == goodreads_session._DEFAULT_RATE_LIMIT


async def test_a_configured_rate_pins():
    session = await goodreads_session.get_session(rate_limit=2.0)
    assert session.rate_limit == 2.0


async def test_the_session_waits_the_metadata_sources_rate(monkeypatch):
    """G50: the configured rate applies to every caller, read per request,
    whatever the first caller passed."""
    from app import config as app_config

    settings = {"metadata_sources": {"goodreads": {"rate_limit": 30.0}}}
    monkeypatch.setattr(app_config, "load_settings", lambda: settings)
    session = await goodreads_session.get_session(rate_limit=2.0)
    assert session.current_rate() == 30.0
    settings["metadata_sources"]["goodreads"]["rate_limit"] = 12.0
    assert session.current_rate() == 12.0      # no restart needed


async def test_a_directly_built_session_keeps_its_own_rate(monkeypatch):
    from app import config as app_config

    monkeypatch.setattr(app_config, "load_settings",
                        lambda: {"metadata_sources": {"goodreads": {"rate_limit": 30.0}}})
    assert goodreads_session.GoodreadsSession(rate_limit=0).current_rate() == 0


async def test_the_wait_before_a_request_uses_the_configured_rate(monkeypatch):
    from app import config as app_config

    monkeypatch.setattr(app_config, "load_settings",
                        lambda: {"metadata_sources": {"goodreads": {"rate_limit": 30.0}}})
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(goodreads_session.asyncio, "sleep", fake_sleep)
    session = await goodreads_session.get_session(rate_limit=2.0)
    await session._sleep_with_jitter()
    assert slept and 30.0 <= slept[0] <= 31.0
