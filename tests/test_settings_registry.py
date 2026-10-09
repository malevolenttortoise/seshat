"""Issue 25 / G127 — every settings key the code uses is registered.

`load_settings` drops top-level keys no code knows (`config.is_known_settings_key`).
A key some code reads or writes but nobody registered would be swept the
next time settings.json changes: live state deleted silently (a one-time
backfill's "done" sentinel re-running it at the next boot, say). This test
scans `app/` for settings reads and writes and fails on any such key.

What it sees: `x.get("k")`, `x["k"]`, `x.setdefault/pop("k")` and
`"k" in x` where `x` is `load_settings()` or a name bound to it (or a
`settings` parameter). Keys built from module constants, local strings,
f-strings (as patterns), ternaries and loops over literal tuples are
resolved. A site it can't resolve must be listed in `_COVERED_ELSEWHERE`
with the test that covers its keys.
"""
from __future__ import annotations

import ast
import fnmatch
import json
import logging
from pathlib import Path

import pytest

from app import config

APP = Path(__file__).resolve().parent.parent / "app"

# Sites whose key comes from data the scan can't follow, and what covers them.
_COVERED_ELSEWHERE = {
    # the bus reads each event's legacy key → test_event_legacy_keys_are_known
    ("notifications/bus.py", "is_enabled"),
    # PATCH writes only keys the loaded settings already hold (it rejects
    # the rest), and never `_RUNTIME_STATE_KEYS` → test_runtime_state_keys_are_known
    ("routers/settings.py", "apply_settings_patch"),
    # the economy buy paths write `timestamp_key` (a literal at each call) →
    # test_economy_keys_are_known
    ("orchestrator/economy_scheduler.py", "_record_buy_outcome"),
    ("routers/economy.py", "_persist_manual_buy_result"),
    # Phase 7's one-shot migration reads the legacy per-source keys
    # (`<source>_enabled`, `rate_<source>`, the two legacy priority lists)
    # that the sweep must DROP afterwards; it runs before any sweep
    # (`_sweep_unknown_keys` waits for `metadata_sources`).
    ("metadata/source_config.py", "_derive_priority_list"),
    ("metadata/source_config.py", "_build_source_entry"),
    ("metadata/source_config.py", "_legacy_rate_for"),
}
_MIGRATION_ONLY_PATTERNS = {"*_enabled", "rate_*"}


def _is_load(node) -> bool:
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name) and f.id == "load_settings":
            return True
        if isinstance(f, ast.Attribute) and f.attr == "load_settings":
            return True
        if isinstance(f, ast.Name) and f.id == "dict" and node.args and _is_load(node.args[0]):
            return True
        if isinstance(f, ast.Attribute) and f.attr == "copy" and _is_load(f.value):
            return True
    if isinstance(node, ast.Dict):
        return any(k is None and _is_load(v) for k, v in zip(node.keys, node.values))
    return False


def _walk(scope):
    """ast.walk, but a module's pass leaves function bodies to their own."""
    if not isinstance(scope, ast.Module):
        yield from ast.walk(scope)
        return
    todo = list(scope.body)
    while todo:
        node = todo.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            todo.extend(ast.iter_child_nodes(node))


def _pattern(node: ast.JoinedStr) -> str:
    return "".join(v.value if isinstance(v, ast.Constant) else "*" for v in node.values)


class _Scope:
    def __init__(self, consts: dict, locals_: dict, settings_names: set):
        self.consts = consts
        self.locals = locals_
        self.settings_names = settings_names

    def values(self, node):
        """The strings (or `*` patterns) `node` can be, or None if unknown."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.JoinedStr):
            return [_pattern(node)]
        if isinstance(node, ast.IfExp):
            a, b = self.values(node.body), self.values(node.orelse)
            return None if a is None or b is None else a + b
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            out = []
            for e in node.elts:
                v = self.values(e)
                if v is None:
                    return None
                out += v
            return out
        if isinstance(node, ast.Dict):
            return self.values(ast.Tuple(elts=[k for k in node.keys if k is not None]))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            a, b = self.values(node.left), self.values(node.right)
            return None if a is None or b is None else a + b
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and \
                node.func.id in ("frozenset", "set", "tuple", "list") and node.args:
            return self.values(node.args[0])
        if isinstance(node, ast.Name):
            if node.id in self.locals:
                return self.locals[node.id]
            if node.id in self.consts:
                return self.values(self.consts[node.id])
        return None

    def is_settings(self, node) -> bool:
        return _is_load(node) or (isinstance(node, ast.Name) and node.id in self.settings_names)


def _scan():
    """{key or pattern: [sites]} and the unresolved (file, function) sites."""
    found: dict[str, list[str]] = {}
    unresolved: set[tuple[str, str]] = set()
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        tree = ast.parse(path.read_text())
        consts = {}
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and node.value is not None:
                        consts[t.id] = node.value
        funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for func in [tree, *funcs]:
            fname = getattr(func, "name", "<module>")
            loads, other = set(), set()
            if fname != "<module>":
                for a in func.args.args + func.args.kwonlyargs:
                    if a.arg == "settings":
                        loads.add("settings")
            scope = _Scope(consts, {}, set())
            for n in _walk(func):
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            (loads if _is_load(n.value) else other).add(t.id)
                            v = scope.values(n.value)
                            if v is not None:
                                scope.locals[t.id] = v
                elif isinstance(n, (ast.For, ast.comprehension)) and isinstance(n.target, ast.Name):
                    v = scope.values(n.iter)
                    if v is not None:
                        scope.locals[n.target.id] = v
            scope.settings_names = loads - other
            for n in _walk(func):
                base = key_node = None
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and \
                        n.func.attr in ("get", "setdefault", "pop") and n.args:
                    base, key_node = n.func.value, n.args[0]
                elif isinstance(n, ast.Subscript):
                    base, key_node = n.value, n.slice
                elif isinstance(n, ast.Compare) and len(n.ops) == 1 and \
                        isinstance(n.ops[0], (ast.In, ast.NotIn)):
                    base, key_node = n.comparators[0], n.left
                if base is None or not scope.is_settings(base):
                    continue
                keys = scope.values(key_node)
                if keys is None:
                    unresolved.add((rel, fname))
                    continue
                for k in keys:
                    found.setdefault(k, []).append(f"{rel}:{n.lineno}")
    return found, unresolved


def _known(key: str) -> bool:
    if "*" in key:
        return key in config.SETTINGS_KEY_PATTERNS or key in _MIGRATION_ONLY_PATTERNS
    return config.is_known_settings_key(key)


def test_every_settings_key_the_code_uses_is_registered():
    found, unresolved = _scan()
    unknown = {k: v[:3] for k, v in found.items() if not _known(k)}
    assert not unknown, (
        "settings keys read or written by code but not registered in "
        f"app/config.py (the sweep would delete them): {unknown}"
    )
    assert unresolved <= _COVERED_ELSEWHERE, (
        "settings accessed with a key the scan can't resolve; register the "
        f"keys and add the site to _COVERED_ELSEWHERE: {sorted(unresolved - _COVERED_ELSEWHERE)}"
    )
    assert len(found) > 100  # the scan still sees the code


def test_runtime_state_keys_are_known():
    from app.routers.settings import _RUNTIME_STATE_KEYS
    assert [k for k in _RUNTIME_STATE_KEYS if not config.is_known_settings_key(k)] == []


def test_event_legacy_keys_are_known():
    from app.notifications import events
    keys = [m.legacy_setting_key for m in events.REGISTRY.values() if m.legacy_setting_key]
    assert [k for k in keys if not config.is_known_settings_key(k)] == []


def test_economy_keys_are_known():
    from app.routers.economy import _CONFIG_KEYS, _CONFIG_READONLY_KEYS
    assert "mam_economy_fl_wedge_offer_enabled" not in _CONFIG_KEYS  # retired (ADR-0027)
    assert [k for k in _CONFIG_KEYS + _CONFIG_READONLY_KEYS
            if not config.is_known_settings_key(k)] == []


def test_secret_keys_mirror_the_secret_store():
    from app.secrets import SECRET_KEYS
    assert config.SECRET_SETTINGS_KEYS == frozenset(SECRET_KEYS)


# ─── the sweep ───────────────────────────────────────────────


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", path)
    monkeypatch.setattr(config, "_logging_ready", True)
    monkeypatch.setattr(config, "_pending_info", [])
    monkeypatch.setattr(config, "_settings_cache", {"mtime": object(), "data": None})
    monkeypatch.setattr(config, "_build_label", lambda: "abc1234")
    return path


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))


def test_unknown_keys_are_dropped_logged_and_backed_up_once(settings_file, caplog):
    saved = {
        "metadata_sources": {},
        "policy_lookup_torrent_info": True,
        "rate_goodreads": 30,
        "rate_mam": 3,
    }
    _write(settings_file, saved)

    with caplog.at_level(logging.INFO, logger="seshat.config"):
        s = config.load_settings()

    assert "policy_lookup_torrent_info" not in s and "rate_goodreads" not in s
    assert s["rate_mam"] == 3
    on_disk = json.loads(settings_file.read_text())
    assert "policy_lookup_torrent_info" not in on_disk
    backup = settings_file.with_name("settings.json.pre-sweep-abc1234")
    assert json.loads(backup.read_text()) == saved
    assert {r.getMessage() for r in caplog.records} >= {
        "settings sweep: dropped 'policy_lookup_torrent_info' (no code reads it)",
        "settings sweep: dropped 'rate_goodreads' (no code reads it)",
    }

    # A later sweep under the same build keeps the first backup.
    _write(settings_file, {**on_disk, "another_dead_key": 1})
    config.load_settings()
    assert json.loads(backup.read_text()) == saved


def test_drops_before_logging_is_configured_are_logged_once_it_is(
    settings_file, monkeypatch, caplog,
):
    """The first load at startup can run before `apply_logging`; its drops
    used to vanish from the log (live check, 2026-10-09)."""
    monkeypatch.setattr(config, "_logging_ready", False)
    _write(settings_file, {"metadata_sources": {}, "weekly_audit_day": 6})

    with caplog.at_level(logging.INFO, logger="seshat.config"):
        config.load_settings()
        assert not any("weekly_audit_day" in r.getMessage() for r in caplog.records)
        config.apply_logging(False)

    messages = [r.getMessage() for r in caplog.records]
    assert "settings sweep: dropped 'weekly_audit_day' (no code reads it)" in messages
    assert config._pending_info == []


def test_runtime_and_secret_keys_survive(settings_file):
    keep = {
        "metadata_sources": {},
        "v2_12_1_dual_row_backfill_done": True,
        "goodreads_backoff": {"book_page": {"until": 1}},
        "metadata_cache.amazon.stall_notified_at": 123.0,
        "format_dedup_release_tick_seconds": 30,
        "qbit_password": "",
    }
    _write(settings_file, keep)

    s = config.load_settings()

    for key, value in keep.items():
        assert s[key] == value


def test_nothing_is_swept_before_the_per_source_migration(settings_file):
    """An install old enough to have no `metadata_sources` still needs its
    legacy per-source keys for the startup migration that builds it."""
    _write(settings_file, {"goodreads_enabled": True, "rate_goodreads": 30})

    s = config.load_settings()

    assert s["goodreads_enabled"] is True and s["rate_goodreads"] == 30
    assert not list(settings_file.parent.glob("*.pre-sweep-*"))


def test_known_key_patterns():
    assert config.is_known_settings_key("metadata_cache.goodreads.stall_notified_at")
    assert not config.is_known_settings_key("metadata_cache.goodreads.other")
    assert fnmatch.fnmatchcase("rate_kobo", "rate_*")
