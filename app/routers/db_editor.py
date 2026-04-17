"""
Database browser + row editor.

Power-user tool for inspecting and surgically editing Seshat's
SQLite database without SSH'ing into the container. Useful for
debugging review-queue issues, confirming author-list state,
checking grab history, and repairing the occasional bad row
(e.g. a `manual_inject_<id>` torrent_name leaked onto a grab row
by a pre-v1.1.4 AthenaScout send).

v1.1 shipped read-only; v1.2 adds write endpoints:

  GET    /api/v1/db/tables                — list tables + row counts
  GET    /api/v1/db/table/{name}/schema   — column metadata
  GET    /api/v1/db/table/{name}          — paginated rows
  POST   /api/v1/db/table/{name}/update   — batch cell updates
  POST   /api/v1/db/table/{name}/add      — insert new row
  DELETE /api/v1/db/table/{name}/row/{id} — delete by rowid

Every write goes through the same `_TABLES` whitelist used by the
read endpoints, so a caller can't point the editor at
`sqlite_master` or anything outside the expected operational data.
Writes validate types against `PRAGMA table_info`, refuse to touch
the primary-key column, and refuse NOT NULL → NULL transitions.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel

from app.database import get_db as get_pipeline_db
from app.discovery.database import get_db as get_discovery_db

_log = logging.getLogger("seshat.routers.db_editor")

router = APIRouter(prefix="/api/v1/db", tags=["db_editor"])

# Whitelist of tables the browser is allowed to read. Tables not
# listed here are rejected with 404 — the name is never interpolated
# into SQL without passing through this check, so a caller can't
# point the browser at sqlite_master or anything else interesting.
_PIPELINE_TABLES: frozenset[str] = frozenset({
    "authors_allowed",
    "authors_ignored",
    "authors_weekly_skip",
    "authors_tentative_review",
    "announces",
    "grabs",
    "snatch_ledger",
    "pending_queue",
    "mam_session",
    "pipeline_runs",
    "book_review_queue",
    "tentative_torrents",
    "ignored_torrents_seen",
    "calibre_additions",
})

_DISCOVERY_TABLES: frozenset[str] = frozenset({
    "authors",
    "series",
    "books",
    "sync_log",
    "mam_scan_log",
    "book_series_suggestions",
    "pen_name_links",
})

_TABLES = _PIPELINE_TABLES | _DISCOVERY_TABLES


def _check_table(name: str) -> None:
    if name not in _TABLES:
        raise HTTPException(
            status_code=404,
            detail=f"unknown or disallowed table: {name!r}",
        )


async def _get_db(name: str):
    """Return the correct database connection for a table."""
    if name in _DISCOVERY_TABLES:
        return await get_discovery_db()
    return await get_pipeline_db()


# ─── Response models ──────────────────────────────────────────


class TableEntry(BaseModel):
    name: str
    row_count: int


class TablesResponse(BaseModel):
    tables: list[TableEntry]


class ColumnInfo(BaseModel):
    name: str
    type: str
    not_null: bool
    primary_key: bool


class SchemaResponse(BaseModel):
    table: str
    columns: list[ColumnInfo]


class RowsResponse(BaseModel):
    table: str
    total: int
    page: int
    per_page: int
    rows: list[dict[str, Any]]


# ─── Endpoints ────────────────────────────────────────────────


@router.get("/tables", response_model=TablesResponse)
async def list_tables() -> TablesResponse:
    """List every whitelisted table with its current row count."""
    entries: list[TableEntry] = []
    for table_set, get_db_fn in [
        (_PIPELINE_TABLES, get_pipeline_db),
        (_DISCOVERY_TABLES, get_discovery_db),
    ]:
        db = await get_db_fn()
        try:
            for name in sorted(table_set):
                try:
                    cur = await db.execute(f"SELECT COUNT(*) FROM [{name}]")
                    row = await cur.fetchone()
                    entries.append(TableEntry(name=name, row_count=int(row[0]) if row else 0))
                except Exception:
                    entries.append(TableEntry(name=name, row_count=0))
        finally:
            await db.close()
    entries.sort(key=lambda e: e.name)
    return TablesResponse(tables=entries)


@router.get("/table/{name}/schema", response_model=SchemaResponse)
async def table_schema(name: str) -> SchemaResponse:
    """Column metadata for a whitelisted table.

    Wraps SQLite's PRAGMA table_info; shapes each row into a
    small dataclass rather than returning the 6-tuple raw.
    """
    _check_table(name)
    db = await _get_db(name)
    try:
        cur = await db.execute(f"PRAGMA table_info([{name}])")
        rows = await cur.fetchall()
    finally:
        await db.close()
    columns = [
        ColumnInfo(
            name=str(r[1]),
            type=str(r[2] or ""),
            not_null=bool(r[3]),
            primary_key=bool(r[5]),
        )
        for r in rows
    ]
    return SchemaResponse(table=name, columns=columns)


@router.get("/table/{name}", response_model=RowsResponse)
async def list_rows(
    name: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None),
) -> RowsResponse:
    """Paginated row list for a whitelisted table.

    `search` does a case-insensitive substring match against every
    TEXT column in the table. Keeps the query simple — no per-column
    filter UI yet; the MVP scope expects the user to use browser
    find-in-page for narrower queries.
    """
    _check_table(name)
    db = await _get_db(name)
    try:
        # Column set for the search filter.
        sch = await db.execute(f"PRAGMA table_info([{name}])")
        col_info = await sch.fetchall()
        text_cols = [str(r[1]) for r in col_info if "TEXT" in str(r[2] or "").upper()]

        where = ""
        params: list[Any] = []
        if search and text_cols:
            needle = f"%{search}%"
            clauses = [f"[{c}] LIKE ?" for c in text_cols]
            where = " WHERE " + " OR ".join(clauses)
            params = [needle] * len(text_cols)

        count_cur = await db.execute(
            f"SELECT COUNT(*) FROM [{name}]{where}", params,
        )
        count_row = await count_cur.fetchone()
        total = int(count_row[0]) if count_row else 0

        offset = (page - 1) * per_page
        cur = await db.execute(
            f"SELECT * FROM [{name}]{where} LIMIT ? OFFSET ?",
            [*params, per_page, offset],
        )
        rows = await cur.fetchall()
        row_dicts = [dict(r) for r in rows]
    finally:
        await db.close()

    return RowsResponse(
        table=name,
        total=total,
        page=page,
        per_page=per_page,
        rows=row_dicts,
    )


# ─── Write endpoints (v1.2) ───────────────────────────────────

async def _column_meta(db, name: str) -> tuple[dict[str, dict], Optional[str]]:
    """Return ({column_name: meta}, pk_column_name) for a whitelisted table.

    `meta` carries `{type, notnull, pk}` for each column. The PK is
    returned separately so callers don't have to re-scan the dict
    and can guard against missing PKs (for tables where `rowid` is
    the implicit key — none of our whitelisted tables, but cheap to
    handle).
    """
    cur = await db.execute(f"PRAGMA table_info([{name}])")
    rows = await cur.fetchall()
    meta: dict[str, dict] = {}
    pk_col: Optional[str] = None
    for r in rows:
        col_name = str(r[1])
        meta[col_name] = {
            "type": str(r[2] or "").upper(),
            "notnull": bool(r[3]),
            "pk": bool(r[5]),
        }
        if r[5]:
            pk_col = col_name
    return meta, pk_col


def _coerce_value(val: Any, col_type: str, col_name: str) -> Any:
    """Coerce a JSON-sent value into the column's declared type.

    Raises `HTTPException(400)` on type mismatch — callers rely on
    the exception to reject a whole batch rather than writing a
    partial update.
    """
    if val is None or val == "":
        return None
    if "INTEGER" in col_type:
        try:
            return int(val)
        except (ValueError, TypeError):
            raise HTTPException(400, f"expected INTEGER for {col_name!r}, got {val!r}")
    if "REAL" in col_type or "FLOAT" in col_type or "DOUBLE" in col_type:
        try:
            return float(val)
        except (ValueError, TypeError):
            raise HTTPException(400, f"expected REAL for {col_name!r}, got {val!r}")
    return str(val)


@router.post("/table/{name}/update")
async def update_rows(name: str, body: dict = Body(...)) -> dict[str, Any]:
    """Batch-update cells in a whitelisted table.

    Body: `{"edits": {"<row_id>": {"<col>": value, ...}, ...}}`

    Validates types and NOT NULL constraints against PRAGMA table_info
    BEFORE applying any writes, so an invalid edit in one row
    doesn't commit a partial update across the batch. Refuses to
    touch the primary-key column.
    """
    _check_table(name)
    edits = body.get("edits") or {}
    if not edits:
        return {"status": "ok", "updated": 0}

    db = await _get_db(name)
    try:
        col_meta, pk_col = await _column_meta(db, name)
        if pk_col is None:
            raise HTTPException(500, f"table {name!r} has no primary key column")

        # Pre-validate the whole batch.
        errors: list[dict[str, Any]] = []
        for row_id, changes in edits.items():
            for col, val in changes.items():
                if col not in col_meta:
                    errors.append({"row": row_id, "column": col, "error": f"unknown column {col!r}"})
                    continue
                if col_meta[col]["pk"]:
                    errors.append({"row": row_id, "column": col, "error": "cannot edit primary key"})
                    continue
                if (val is None or val == "") and col_meta[col]["notnull"]:
                    errors.append({"row": row_id, "column": col, "error": f"column {col!r} is NOT NULL"})
        if errors:
            return {"status": "error", "errors": errors}

        updated = 0
        for row_id, changes in edits.items():
            set_parts: list[str] = []
            params: list[Any] = []
            for col, val in changes.items():
                if col_meta[col]["pk"]:
                    continue
                set_parts.append(f"[{col}] = ?")
                params.append(_coerce_value(val, col_meta[col]["type"], col))
            if not set_parts:
                continue
            try:
                params.append(int(row_id))
            except (ValueError, TypeError):
                raise HTTPException(400, f"row id {row_id!r} is not an integer")
            await db.execute(
                f"UPDATE [{name}] SET {', '.join(set_parts)} WHERE [{pk_col}] = ?",
                params,
            )
            updated += 1
        await db.commit()
        _log.info("db_editor: updated %d row(s) in %s", updated, name)
        return {"status": "ok", "updated": updated}
    finally:
        await db.close()


@router.post("/table/{name}/add")
async def add_row(name: str, body: dict = Body(...)) -> dict[str, Any]:
    """Insert a new row into a whitelisted table.

    Body: `{"values": {"<col>": value, ...}}`

    The primary-key column is skipped (auto-increment). Columns
    with NOT NULL that are omitted or null cause a 400. Unknown
    columns are silently ignored so a caller can post the whole
    row dict without pruning read-only fields.
    """
    _check_table(name)
    values = (body.get("values") or {})
    if not values:
        raise HTTPException(400, "no values provided")

    db = await _get_db(name)
    try:
        col_meta, _pk = await _column_meta(db, name)

        insert_cols: list[str] = []
        insert_vals: list[Any] = []
        for col, val in values.items():
            if col not in col_meta or col_meta[col]["pk"]:
                continue
            coerced = _coerce_value(val, col_meta[col]["type"], col)
            if coerced is None and col_meta[col]["notnull"]:
                raise HTTPException(400, f"column {col!r} is NOT NULL")
            insert_cols.append(f"[{col}]")
            insert_vals.append(coerced)

        if not insert_cols:
            raise HTTPException(400, "no writable columns in payload")

        placeholders = ",".join(["?"] * len(insert_cols))
        cur = await db.execute(
            f"INSERT INTO [{name}] ({','.join(insert_cols)}) VALUES ({placeholders})",
            insert_vals,
        )
        await db.commit()
        new_id = cur.lastrowid
        _log.info("db_editor: inserted row id=%s into %s", new_id, name)
        return {"status": "ok", "id": new_id}
    finally:
        await db.close()


@router.delete("/table/{name}/row/{row_id}")
async def delete_row(name: str, row_id: int) -> dict[str, Any]:
    """Delete one row by primary key from a whitelisted table.

    Translates FK-constraint violations into a 409 with a readable
    hint so the UI can surface "delete or reassign child rows first"
    without making the user decode sqlite3's raw error string.
    """
    _check_table(name)
    db = await _get_db(name)
    try:
        col_meta, pk_col = await _column_meta(db, name)
        if pk_col is None:
            raise HTTPException(500, f"table {name!r} has no primary key column")

        cur = await db.execute(
            f"SELECT [{pk_col}] FROM [{name}] WHERE [{pk_col}] = ?", (row_id,),
        )
        if await cur.fetchone() is None:
            raise HTTPException(404, f"row {row_id} not found in {name}")

        try:
            await db.execute(
                f"DELETE FROM [{name}] WHERE [{pk_col}] = ?", (row_id,),
            )
            await db.commit()
        except sqlite3.IntegrityError as e:
            msg = str(e)
            if "FOREIGN KEY" in msg.upper():
                raise HTTPException(
                    409,
                    "row is referenced by other records; delete or "
                    "reassign the dependent rows first",
                )
            raise HTTPException(409, f"cannot delete: {msg}")
        _log.info("db_editor: deleted row %d from %s", row_id, name)
        return {"status": "ok"}
    finally:
        await db.close()
