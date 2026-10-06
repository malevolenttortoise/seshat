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
    import json

    from app import auth_db, auth_secret, config, database, runtime, secrets
    from app.discovery import author_identity
    from app.discovery import database as disco_database
    from app.discovery import metadata_cache as disco_metadata_cache
    from app.metadata import id_cache

    data_dir = tmp_path_factory.mktemp("seshat-data")
    db_path = data_dir / "seshat.db"

    mp = pytest.MonkeyPatch()
    mp.setattr(config, "DATA_DIR", data_dir)
    mp.setattr(config, "APP_DB_PATH", db_path)
    mp.setattr(database, "APP_DB_PATH", db_path)

    # The AUTH db (which is where the `secrets` table lives) resolves its
    # path through `runtime.get_data_dir()` at CALL time, not through
    # `config.DATA_DIR` -- and `get_data_dir()` does not consult the
    # DATA_DIR env var either, so neither the patches above nor
    # `DATA_DIR=... pytest` redirect it. Each consumer did
    # `from app.runtime import get_data_dir`, so it holds its own
    # reference and has to be patched by name.
    mp.setattr(runtime, "get_data_dir", lambda: data_dir)
    mp.setattr(auth_db, "get_data_dir", lambda: data_dir)
    mp.setattr(auth_secret, "get_data_dir", lambda: data_dir)

    mp.setattr(config, "SETTINGS_PATH", data_dir / "settings.json")
    mp.setattr(config, "AUTH_SECRET_PATH", data_dir / "auth_secret")

    # Four modules do `from app.config import DATA_DIR`, binding their own
    # copy at import time -- patching `config.DATA_DIR` alone leaves them
    # pointed at the real directory. These are what wrote per-library
    # `seshat_<slug>.db` files and `metadata_cache_amazon.db` into the
    # developer's data dir on every run.
    for _mod in (disco_database, disco_metadata_cache, author_identity, id_cache):
        mp.setattr(_mod, "DATA_DIR", data_dir)

    # Disable the qBit add stagger suite-wide. `_stagger_qbit_add()`
    # sleeps `qbit_add_stagger_s` (DEFAULT_SETTINGS: 2.0) +/- jitter
    # before EVERY qBit add. That is deliberate tracker-announce spacing
    # in production and pure dead time here -- it cost ~2.2s in every
    # test that reaches the submit path (36.8s in
    # tests/orchestrator/test_dispatch.py alone).
    #
    # tests/orchestrator/test_dispatch_stagger.py is the one place that
    # actually exercises the stagger, and it patches `load_settings`
    # itself, so it is unaffected by this default.
    (data_dir / "settings.json").write_text(
        json.dumps({
            "qbit_add_stagger_s": 0,
            "qbit_add_stagger_jitter_s": 0,
        })
    )
    # `load_settings` is mtime-cached and may already hold an entry read
    # from the REAL settings path during collection; drop it so the
    # first call inside the suite re-reads from the isolated dir.
    config._settings_cache["data"] = None
    config._settings_cache["mtime"] = None

    async def _init():
        await database.init_db()
        await auth_db.init_auth_db()
        await secrets.init_secrets_table()

    asyncio.run(_init())
    try:
        yield data_dir
    finally:
        config._settings_cache["data"] = None
        config._settings_cache["mtime"] = None
        mp.undo()


@pytest.fixture(autouse=True)
def _no_real_mam(monkeypatch):
    """The suite never talks to the real MAM.

    Until 2026-10-06 it did: any test that drove the dispatcher with a
    non-empty `mam_token` but no `fake_mam` fixture reached
    `get_torrent_info` / the cover fetch through the real module-level
    httpx clients — 44 search-API POSTs and 6 CDN GETs per run, all
    with a junk `mam_id`, twice per push in CI (one per Python
    version). The tests passed only because they fail open on errors.

    Both MAM clients (`app.mam.cookie` and the discovery source's own)
    start every test as a client whose transport refuses with a
    ConnectError — the same "network down" the fail-open paths already
    handle. The getters fall back to it after an app-lifespan shutdown
    nulls the client, instead of lazily building a real one. A test
    that installs its own client (`fake_mam`, or setting `_client`
    directly) still gets it: the patched getters return `_client`
    whenever it is set.
    """
    from app.discovery.sources import mam as disco_mam
    from app.mam import cookie, torrent_info

    # The torrent-info cache is module-level with a 120s TTL, so a test
    # could see torrent IDs (and their authors) cached by an earlier one
    # — which is how a CI-only deadlock hid behind test order.
    torrent_info.invalidate_cache()
    # Same for the live session token: `DispatcherDeps.live_mam_token()`
    # prefers it over the deps' own token, so a cookie rotated by one
    # test (`test_user_status`) leaked into every later dispatch — the
    # lifespan smoke test failed whenever it ran after that file.
    monkeypatch.setattr(cookie, "_current_token", None)

    def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"test suite tried to contact real MAM: {request.url}",
            request=request,
        )

    guard = httpx.AsyncClient(transport=httpx.MockTransport(_refuse))
    for mod, getter in ((cookie, "get_client"), (disco_mam, "_get_client")):
        monkeypatch.setattr(mod, "_client", guard)
        monkeypatch.setattr(mod, getter, lambda mod=mod: mod._client or guard)
    yield guard


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
