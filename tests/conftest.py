"""
Pytest fixtures shared across the Seshat test suite.

Includes the `temp_db` SQLite fixture, the `fake_mam` HTTP transport
swap, the `fake_qbit` per-instance fake, and the `fake_irc` in-memory
IRC server. Tests opt into whichever they need by parameter name.
"""
import sys
from pathlib import Path

import httpx
import pytest

# Make `app` importable when running pytest from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(scope="session", autouse=True)
def _isolated_data_dir(tmp_path_factory):
    """Keep the whole suite off the developer's real app database.

    `app.config.DATA_DIR` resolves to a per-user OS location
    (`$XDG_DATA_HOME/seshat` on Linux), and `app.database.get_db()` reads
    the module-level `APP_DB_PATH` bound from it at import time. Any test
    that does NOT take the `temp_db` fixture therefore fell through to
    that real file.

    On a machine that has ever run Seshat -- or just run this suite
    before, since a stray `init_db()` creates the schema there -- the
    tables happen to exist and those tests pass. On a clean checkout they
    fail with `no such table: secrets` / `no such table: book_grab_links`.
    That is exactly what the first CI run caught: 17 failures here, zero
    locally, with no difference but accumulated state on disk.

    Pointing DATA_DIR at a throwaway directory and initializing the
    schema once per session makes the two environments agree, and means
    a test can no longer read or write the developer's real data.

    `temp_db` still overrides this per-test; this is only the default
    for everything that doesn't ask.
    """
    import asyncio

    from app import config, database

    data_dir = tmp_path_factory.mktemp("seshat-data")
    db_path = data_dir / "seshat.db"

    mp = pytest.MonkeyPatch()
    mp.setattr(config, "DATA_DIR", data_dir)
    mp.setattr(config, "APP_DB_PATH", db_path)
    mp.setattr(database, "APP_DB_PATH", db_path)

    asyncio.run(database.init_db())
    try:
        yield data_dir
    finally:
        mp.undo()


@pytest.fixture
async def fake_mam():
    """Install a programmable fake MAM HTTP server for the test.

    Replaces `app.mam.cookie._client` with an `httpx.AsyncClient` whose
    transport intercepts every request and returns canned responses
    from a `FakeMAM` instance. The yielded fake is mutable — tests
    tweak `fake.search.status`, `fake.download.body`, etc. to drive
    different scenarios.

    The cookie module exposes a single process-wide client; this
    fixture mutates it directly and restores the original on teardown.
    Real MAM is never contacted.
    """
    from app.mam import cookie
    from tests.fake_mam import FakeMAM

    fake = FakeMAM()
    original_client = cookie._client
    cookie._client = httpx.AsyncClient(transport=fake.transport())
    try:
        yield fake
    finally:
        await cookie._client.aclose()
        cookie._client = original_client


@pytest.fixture
def fake_qbit():
    """Programmable fake qBittorrent WebUI server.

    Unlike `fake_mam`, this fixture doesn't monkey-patch any module
    state — `QbitClient` is per-instance, so tests construct their
    own client and pass `fake_qbit.transport()` to its `transport=`
    constructor parameter directly. The fake's request log and
    captured-add list let tests assert on what the client did.
    """
    from tests.fake_qbit import FakeQbit

    return FakeQbit()


@pytest.fixture
def fake_irc():
    """Programmable in-memory fake IRC server.

    Returns a `FakeIrc` instance whose `connect_fn` method should be
    passed to `IrcClient(connect_fn=fake_irc.connect_fn)`. Tests
    drive the handshake step-by-step using `wait_for_line` and
    `feed_line` (or use the `drive_sasl_handshake` helper from
    `tests.fake_irc` to skip the boilerplate).
    """
    from tests.fake_irc import FakeIrc

    return FakeIrc()


@pytest.fixture
async def temp_db(tmp_path, monkeypatch):
    """Per-test SQLite database fully initialized with the production schema.

    Each test gets a brand-new file under pytest's `tmp_path`, the
    `app.config.APP_DB_PATH` constant is monkeypatched to point at it,
    and `init_db()` runs to create every table in `database.SCHEMA`.
    Yields the path so tests can pass it to fresh connections; the
    file is automatically removed at the end of the test by pytest's
    tmp_path teardown.

    Tests that need a connection should call `await get_db()` after
    the fixture has yielded — the monkeypatch ensures get_db() opens
    the temp file rather than the real DATA_DIR.
    """
    from app import config, database

    db_path = tmp_path / "seshat-test.db"
    monkeypatch.setattr(config, "APP_DB_PATH", db_path)
    monkeypatch.setattr(database, "APP_DB_PATH", db_path)
    await database.init_db()
    yield db_path
