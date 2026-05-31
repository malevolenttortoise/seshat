"""
Regression tests for the env-var → settings.json seeding layer.

This file exists because of a real production bug found during the
first deploy: `MAM_IRC_NICK`, `MAM_IRC_ACCOUNT`, and `MAM_IRC_PASSWORD`
were defined in `DEFAULT_SETTINGS` but never wired up in
`_apply_env_overrides`. The lifespan reads `settings.json` to decide
whether to start the IRC listener, so an unwired env var meant the
listener silently stayed off no matter what the user put in their
docker-compose.yml. The bug was invisible — there was no error, no
warning, no test failure, just a missing log line.

The fix is the dumbest-possible change (six lines in `_apply_env_overrides`)
but the discipline this file enforces is more valuable than the fix:
**every env var declared as a first-run seed at the top of config.py
MUST have a matching wire-up in `_apply_env_overrides`, and a test
here pinning down the relationship.** That way the next time someone
adds a new ENV_FOO without wiring it up, this test fails immediately.
"""
import importlib
import json

import pytest


@pytest.fixture
def fresh_config(monkeypatch, tmp_path):
    """Reload `app.config` with a clean DATA_DIR and known env vars.

    Each test sets `monkeypatch.setenv(...)` for the env vars it
    cares about BEFORE the fixture body runs (using indirect
    parametrization isn't worth the complexity here — direct
    monkeypatching in the test body is simpler).

    Returns the freshly-imported `app.config` module so the test
    can call `load_settings()` against the temp DATA_DIR.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Force a fresh import of app.config so the module-level ENV_*
    # constants pick up our monkeypatched env vars instead of
    # whatever was set when the test session started.
    import app.config
    importlib.reload(app.config)
    return app.config


class TestEnvSeeding:
    """Every documented first-run env var must seed its settings key."""

    def test_mam_session_id_seeds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MAM_SESSION_ID", "test_cookie_value")
        import app.config
        importlib.reload(app.config)
        settings = app.config.load_settings()
        assert settings["mam_session_id"] == "test_cookie_value"

    def test_mam_irc_nick_seeds(self, monkeypatch, tmp_path):
        # The original production bug.
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MAM_IRC_NICK", "test_bot")
        import app.config
        importlib.reload(app.config)
        settings = app.config.load_settings()
        assert settings["mam_irc_nick"] == "test_bot"

    def test_mam_irc_account_seeds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MAM_IRC_ACCOUNT", "test_account")
        import app.config
        importlib.reload(app.config)
        settings = app.config.load_settings()
        assert settings["mam_irc_account"] == "test_account"

    def test_mam_irc_password_seeds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MAM_IRC_PASSWORD", "secret_pw")
        import app.config
        importlib.reload(app.config)
        settings = app.config.load_settings()
        assert settings["mam_irc_password"] == "secret_pw"

    def test_qbit_url_seeds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("QBIT_URL", "http://qbit.local:8080")
        import app.config
        importlib.reload(app.config)
        settings = app.config.load_settings()
        assert settings["qbit_url"] == "http://qbit.local:8080"

    def test_qbit_username_seeds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("QBIT_USERNAME", "admin")
        import app.config
        importlib.reload(app.config)
        settings = app.config.load_settings()
        assert settings["qbit_username"] == "admin"

    def test_qbit_password_seeds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("QBIT_PASSWORD", "adminadmin")
        import app.config
        importlib.reload(app.config)
        settings = app.config.load_settings()
        assert settings["qbit_password"] == "adminadmin"

    def test_qbit_watch_category_seeds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("QBIT_WATCH_CATEGORY", "custom-category")
        import app.config
        importlib.reload(app.config)
        settings = app.config.load_settings()
        assert settings["qbit_watch_category"] == "custom-category"

    def test_calibre_library_path_seeds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("CALIBRE_LIBRARY_PATH", "/my/calibre")
        import app.config
        importlib.reload(app.config)
        settings = app.config.load_settings()
        assert settings["calibre_library_path"] == "/my/calibre"

    def test_staging_path_seeds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("STAGING_PATH", "/my/staging")
        import app.config
        importlib.reload(app.config)
        settings = app.config.load_settings()
        assert settings["staging_path"] == "/my/staging"

    def test_ntfy_url_seeds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("NTFY_URL", "https://ntfy.sh/mytopic")
        import app.config
        importlib.reload(app.config)
        settings = app.config.load_settings()
        assert settings["ntfy_url"] == "https://ntfy.sh/mytopic"

    def test_verbose_logging_seeds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("VERBOSE_LOGGING", "true")
        import app.config
        importlib.reload(app.config)
        settings = app.config.load_settings()
        assert settings["verbose_logging"] is True

    def test_dry_run_seeds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SESHAT_DRY_RUN", "true")
        import app.config
        importlib.reload(app.config)
        settings = app.config.load_settings()
        assert settings["dry_run"] is True


class TestEnvOverridePrecedence:
    """Env vars only seed FIRST-RUN settings — they never override
    a value already saved in settings.json. This is the rule
    documented in `config.py`'s module docstring; pin it down so
    a future refactor doesn't accidentally make env vars sticky."""

    def test_env_var_does_not_override_saved_settings(
        self, monkeypatch, tmp_path
    ):
        # First run: env var seeds the value into settings.json
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MAM_IRC_NICK", "first_value")
        import app.config
        importlib.reload(app.config)
        first_settings = app.config.load_settings()
        assert first_settings["mam_irc_nick"] == "first_value"

        # Now simulate a restart with a different env var value.
        # The settings.json from the first run should win.
        monkeypatch.setenv("MAM_IRC_NICK", "second_value")
        importlib.reload(app.config)
        second_settings = app.config.load_settings()
        assert second_settings["mam_irc_nick"] == "first_value"


class TestLegacySettingsMigration:
    """v2.9.0 migration: `accept_audiobook_announces` boolean →
    "audiobooks" membership in `allowed_formats`. Documented in
    `_apply_legacy_settings_migrations` in app/config.py."""

    def _seed_settings_file(self, tmp_path, payload: dict) -> None:
        """Write a settings.json with the given shape, monkeypatch-
        friendly: caller is expected to have already pointed DATA_DIR
        at tmp_path via env var."""
        import json
        (tmp_path / "settings.json").write_text(json.dumps(payload))

    def test_legacy_on_with_nonempty_formats_adds_audiobooks(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import app.config
        importlib.reload(app.config)
        self._seed_settings_file(tmp_path, {
            "accept_audiobook_announces": True,
            "allowed_formats": ["ebooks"],
        })

        settings = app.config.load_settings()
        # Legacy flag is gone.
        assert "accept_audiobook_announces" not in settings
        # "audiobooks" added to allowed_formats.
        assert "audiobooks" in settings["allowed_formats"]
        assert "ebooks" in settings["allowed_formats"]

        # Persisted to disk in the migrated shape (no legacy key).
        import json
        saved = json.loads((tmp_path / "settings.json").read_text())
        assert "accept_audiobook_announces" not in saved
        assert "audiobooks" in saved["allowed_formats"]

    def test_legacy_on_with_empty_formats_keeps_empty(
        self, monkeypatch, tmp_path,
    ):
        """Empty allowed_formats means 'accept all formats' — already
        includes audiobooks. Migration should NOT pollute it with an
        explicit audiobooks entry (which would flip semantics to
        'only audiobooks')."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import app.config
        importlib.reload(app.config)
        self._seed_settings_file(tmp_path, {
            "accept_audiobook_announces": True,
            "allowed_formats": [],
        })

        settings = app.config.load_settings()
        assert "accept_audiobook_announces" not in settings
        assert settings["allowed_formats"] == []

    def test_legacy_off_just_drops_the_key(self, monkeypatch, tmp_path):
        """User had accept_audiobook_announces=False (default ebook-only).
        Migration should remove the key without adding 'audiobooks' to
        allowed_formats — preserves ebook-only behavior."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import app.config
        importlib.reload(app.config)
        self._seed_settings_file(tmp_path, {
            "accept_audiobook_announces": False,
            "allowed_formats": ["ebooks"],
        })

        settings = app.config.load_settings()
        assert "accept_audiobook_announces" not in settings
        assert settings["allowed_formats"] == ["ebooks"]
        assert "audiobooks" not in settings["allowed_formats"]

    def test_legacy_on_with_audiobooks_already_listed_no_duplicate(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import app.config
        importlib.reload(app.config)
        self._seed_settings_file(tmp_path, {
            "accept_audiobook_announces": True,
            "allowed_formats": ["ebooks", "audiobooks"],
        })

        settings = app.config.load_settings()
        assert "accept_audiobook_announces" not in settings
        assert settings["allowed_formats"].count("audiobooks") == 1

    def test_no_legacy_key_is_noop(self, monkeypatch, tmp_path):
        """Already-migrated settings (no legacy key) should round-trip
        cleanly — no spurious writes, no shape changes."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import app.config
        importlib.reload(app.config)
        self._seed_settings_file(tmp_path, {
            "allowed_formats": ["ebooks", "audiobooks"],
        })

        settings = app.config.load_settings()
        assert "accept_audiobook_announces" not in settings
        assert settings["allowed_formats"] == ["ebooks", "audiobooks"]

    def test_migration_is_idempotent(self, monkeypatch, tmp_path):
        """Running the helper twice on the same dict must not flip
        the result. Defends against future code paths that load
        twice without reloading from disk."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import app.config
        importlib.reload(app.config)

        settings = {
            "accept_audiobook_announces": True,
            "allowed_formats": ["ebooks"],
        }
        first = app.config._apply_legacy_settings_migrations(settings)
        assert first is True
        snapshot = dict(settings)

        second = app.config._apply_legacy_settings_migrations(settings)
        assert second is False
        assert settings == snapshot


class TestMetadataCacheNestedSettingsSeed:
    """v3.4.0 slice 01 — DEFAULT_SETTINGS now seeds nested
    `metadata_cache.{amazon, goodreads}.{enabled, mode, schedule.*}`.
    Existing installs get missing sub-keys back-filled via
    `_apply_legacy_settings_migrations` deep-seed (shallow-merge would
    otherwise mask the new GR sub-tree under any saved amazon dict).
    See ADR-0018 §3."""

    def _seed_settings_file(self, tmp_path, payload: dict) -> None:
        import json
        (tmp_path / "settings.json").write_text(json.dumps(payload))

    def test_default_settings_seeds_both_sources_symmetrically(self):
        import app.config
        mc = app.config.DEFAULT_SETTINGS["metadata_cache"]
        assert set(mc.keys()) == {"amazon", "goodreads"}
        for src, sub in mc.items():
            assert sub["enabled"] is False, f"{src} default should be off"
            assert sub["mode"] == "disabled", (
                f"{src} default mode should be disabled "
                "(no live behavior change on upgrade)"
            )
            assert sub["schedule"]["active_hours"] == "10:00-22:00"
            assert sub["schedule"]["timezone"] == ""

    def test_fresh_install_persists_nested_shape(
        self, monkeypatch, tmp_path,
    ):
        """First-ever load with no settings.json writes the nested
        sub-tree to disk so an operator hand-editing the file finds
        the keys present rather than absent."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import app.config
        importlib.reload(app.config)

        settings = app.config.load_settings()
        mc = settings.get("metadata_cache")
        assert isinstance(mc, dict)
        assert "amazon" in mc and "goodreads" in mc
        assert mc["goodreads"]["mode"] == "disabled"

        import json
        saved = json.loads((tmp_path / "settings.json").read_text())
        assert "metadata_cache" in saved
        assert "goodreads" in saved["metadata_cache"]
        assert saved["metadata_cache"]["goodreads"]["enabled"] is False

    def test_existing_install_with_amazon_only_gets_goodreads_seeded(
        self, monkeypatch, tmp_path,
    ):
        """The shallow-merge gotcha: a saved
        `metadata_cache: {amazon: {…}}` would otherwise mask the new
        goodreads sub-tree from DEFAULT_SETTINGS. The deep-seed in
        `_apply_legacy_settings_migrations` must fill it in."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import app.config
        importlib.reload(app.config)
        self._seed_settings_file(tmp_path, {
            "metadata_cache": {
                "amazon": {"enabled": True, "mode": "continuous"},
            },
        })

        settings = app.config.load_settings()
        mc = settings["metadata_cache"]
        # User's amazon settings preserved exactly.
        assert mc["amazon"]["enabled"] is True
        assert mc["amazon"]["mode"] == "continuous"
        # Missing amazon sub-keys back-filled from defaults.
        assert mc["amazon"]["schedule"]["active_hours"] == "10:00-22:00"
        # Brand-new goodreads sub-tree appears.
        assert mc["goodreads"]["enabled"] is False
        assert mc["goodreads"]["mode"] == "disabled"
        assert mc["goodreads"]["schedule"]["timezone"] == ""

    def test_existing_install_user_overrides_never_clobbered(
        self, monkeypatch, tmp_path,
    ):
        """A user who has explicitly opted into goodreads (somehow —
        e.g. hand-edit) must keep their values across re-load."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import app.config
        importlib.reload(app.config)
        self._seed_settings_file(tmp_path, {
            "metadata_cache": {
                "goodreads": {
                    "enabled": True,
                    "mode": "scheduled",
                    "schedule": {
                        "active_hours": "01:00-05:00",
                        "timezone": "America/New_York",
                    },
                },
            },
        })

        settings = app.config.load_settings()
        gr = settings["metadata_cache"]["goodreads"]
        assert gr["enabled"] is True
        assert gr["mode"] == "scheduled"
        assert gr["schedule"]["active_hours"] == "01:00-05:00"
        assert gr["schedule"]["timezone"] == "America/New_York"
        # Amazon sub-tree appears with defaults (was missing on disk).
        amz = settings["metadata_cache"]["amazon"]
        assert amz["enabled"] is False
        assert amz["mode"] == "disabled"

    def test_existing_install_partial_schedule_subkeys_get_backfilled(
        self, monkeypatch, tmp_path,
    ):
        """User wrote only `schedule.active_hours` — the missing
        `timezone` sub-key gets back-filled to the default empty
        string so callers don't NPE on a missing key."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import app.config
        importlib.reload(app.config)
        self._seed_settings_file(tmp_path, {
            "metadata_cache": {
                "amazon": {
                    "schedule": {"active_hours": "06:00-22:00"},
                },
            },
        })

        settings = app.config.load_settings()
        amz_sched = settings["metadata_cache"]["amazon"]["schedule"]
        assert amz_sched["active_hours"] == "06:00-22:00"  # user value
        assert amz_sched["timezone"] == ""                  # back-filled

    def test_seeder_is_idempotent(self, monkeypatch, tmp_path):
        """Running the deep-seed twice on an already-seeded dict
        is a no-op — protects against double-write loops."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import app.config
        importlib.reload(app.config)

        settings = {
            "metadata_cache": {
                "amazon": {"enabled": True, "mode": "continuous"},
            },
        }
        first = app.config._apply_legacy_settings_migrations(settings)
        assert first is True
        snapshot = json.loads(json.dumps(settings))

        second = app.config._apply_legacy_settings_migrations(settings)
        assert second is False
        assert json.loads(json.dumps(settings)) == snapshot


class TestNoSilentlyMissingSeeds:
    """The discipline test: every ENV_* constant defined as a
    first-run seed at the top of config.py must be referenced in
    `_apply_env_overrides`. Catches the exact bug that took down
    the IRC listener in production.

    This test scrapes both surfaces and asserts they line up. If
    you add a new ENV_FOO without a matching `if ENV_FOO and ...`
    line in _apply_env_overrides, this fails immediately.
    """

    def test_every_env_constant_is_wired_up(self):
        import inspect

        import app.config

        source = inspect.getsource(app.config._apply_env_overrides)

        # Find every ENV_* constant defined at module level. Skip
        # the ones that are explicitly NOT settings seeds (e.g.
        # WEBUI_HOST, WEBUI_PORT, AUTH_SECRET — those are read
        # directly by main.py / auth scaffolding, never written to
        # settings.json).
        not_seeds = {
            "ENV_WEBUI_HOST",
            "ENV_WEBUI_PORT",
            "ENV_AUTH_SECRET",
        }
        env_constants = [
            name
            for name in dir(app.config)
            if name.startswith("ENV_") and name not in not_seeds
        ]

        unwired = [
            name for name in env_constants if name not in source
        ]
        assert not unwired, (
            f"These ENV_* constants exist in app.config but are NOT "
            f"referenced in _apply_env_overrides — settings.json will "
            f"never get seeded from them: {unwired}"
        )
