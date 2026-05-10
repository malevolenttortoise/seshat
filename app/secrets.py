"""
Secret store for Seshat.

Credentials (MAM cookie, qBit password, IRC password, API keys) live
in the `seshat_auth.db` SQLite file alongside the auth_users table.
They're stored as encrypted-at-rest via Fernet symmetric encryption
using the auth_secret as the key material.

This module provides get/set/list operations. The rest of the app
reads credentials through `get_secret(key)` instead of
`settings.get(key)`. The Settings page PATCH endpoint refuses writes
to secret keys; only the dedicated Credentials page routes through
here.

Why not just settings.json?
  - settings.json is a plain JSON file readable by anyone with
    container access. A user backing up their appdata or sharing a
    docker-compose snippet would leak credentials.
  - The auth DB is already 0600-permissioned and logically separate.
  - Fernet encryption means even a DB dump doesn't leak the raw
    values without the auth_secret.

The Fernet key is derived from the auth_secret (which is already
persisted securely — see auth_secret.py).
"""
from __future__ import annotations

import base64
import hashlib
import logging
from typing import Optional

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken

from app.auth_db import get_auth_db
from app.auth_secret import get_auth_secret

_log = logging.getLogger("seshat.secrets")

# Secret keys that are stored encrypted and never displayed.
# Only genuinely sensitive values (passwords, tokens, cookies).
SECRET_KEYS: dict[str, str] = {
    "mam_session_id": "MAM session cookie",
    "mam_irc_password": "MAM IRC password",
    "qbit_password": "qBittorrent password",
    "hardcover_api_key": "Hardcover API Bearer token",
    "abs_api_key": "Audiobookshelf API Bearer token",
    "cwa_password": "Calibre-Web-Automated user password (push-back)",
}
# `mam_browser_session_id` (mbsc) was removed in v2.4.0 — TOS-disallowed.
# Existing rows in the secrets table from prior versions are harmless
# (they're never read by any code path) and will get cleared next time
# the user manually deletes them via the (now-removed) UI row, OR they
# stay until manual SQL cleanup. Leaving them is the safer option vs.
# auto-purging on upgrade.


# ─── Schema ────────────────────────────────────────────────────

_SECRETS_TABLE = """
CREATE TABLE IF NOT EXISTS secrets (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


async def init_secrets_table() -> None:
    """Ensure the secrets table exists in the auth DB."""
    db = await get_auth_db()
    try:
        await db.executescript(_SECRETS_TABLE)
        await db.commit()
    finally:
        await db.close()


# ─── Encryption ────────────────────────────────────────────────

def _fernet_key() -> bytes:
    """Derive a Fernet key from the auth secret.

    Fernet needs exactly 32 bytes of URL-safe base64. We SHA-256
    hash the auth secret to get a deterministic 32-byte key.
    """
    raw = get_auth_secret().encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    return base64.urlsafe_b64encode(digest)


def _encrypt(plaintext: str) -> str:
    f = Fernet(_fernet_key())
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def _decrypt(ciphertext: str) -> str:
    f = Fernet(_fernet_key())
    return f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")


# ─── Public API ────────────────────────────────────────────────

async def get_secret(key: str) -> Optional[str]:
    """Read a decrypted secret, or None if not set."""
    db = await get_auth_db()
    try:
        cursor = await db.execute(
            "SELECT value FROM secrets WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        try:
            return _decrypt(str(row["value"]))
        except (InvalidToken, Exception):
            _log.warning("secret %r failed to decrypt — possibly corrupted", key)
            return None
    finally:
        await db.close()


async def set_secret(key: str, value: str) -> None:
    """Encrypt and store a secret."""
    encrypted = _encrypt(value)
    db = await get_auth_db()
    try:
        await db.execute(
            """
            INSERT INTO secrets (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, encrypted),
        )
        await db.commit()
    finally:
        await db.close()


async def delete_secret(key: str) -> None:
    db = await get_auth_db()
    try:
        await db.execute("DELETE FROM secrets WHERE key = ?", (key,))
        await db.commit()
    finally:
        await db.close()


async def list_configured() -> dict[str, bool]:
    """Return {key: True/False} for every known secret key."""
    db = await get_auth_db()
    try:
        cursor = await db.execute("SELECT key FROM secrets")
        rows = await cursor.fetchall()
        stored = {str(r["key"]) for r in rows}
    finally:
        await db.close()
    return {k: k in stored for k in SECRET_KEYS}


async def migrate_from_settings() -> int:
    """Copy secrets from settings.json into the encrypted store, then
    blank any settings.json key whose value already lives in the store.

    Returns the number of secrets newly migrated this call. Note this
    runs at every boot — it's idempotent. The blanking step runs even
    when no NEW migration happened, because pre-v2.2.8 versions of
    this routine only blanked settings.json on the first migration
    and left stale plaintext values stranded thereafter (e.g. an old
    `mam_session_id` from before the encrypted-store cutover that the
    rest of the app would then read instead of the live rotated value).
    """
    from app.config import load_settings, save_settings

    settings = load_settings()
    migrated = 0

    for key in SECRET_KEYS:
        value = settings.get(key)
        if value and isinstance(value, str) and value.strip():
            existing = await get_secret(key)
            if existing:
                continue
            await set_secret(key, value.strip())
            migrated += 1

    # Blank settings.json for every secret key that has a live value
    # in the encrypted store, regardless of whether THIS call was the
    # one that put it there. The encrypted store is canonical; any
    # settings.json copy is at best redundant and at worst stale.
    settings = dict(load_settings())
    blanked = 0
    for key in SECRET_KEYS:
        if not settings.get(key):
            continue
        if await get_secret(key):
            settings[key] = ""
            blanked += 1
    if blanked:
        save_settings(settings)
        _log.info(
            "Blanked %d stale plaintext secret(s) in settings.json "
            "(canonical copy lives in encrypted store)",
            blanked,
        )

    if migrated > 0:
        _log.info("Migrated %d secret(s) from settings.json to auth DB", migrated)

    return migrated
