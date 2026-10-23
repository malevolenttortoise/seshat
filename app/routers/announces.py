"""
Announce log viewer endpoint.

    GET /api/v1/announces

Serves the persisted SQLite `announces` audit table — distinct from
the in-memory `app/routers/logs.py` ring buffer. Each row here is
one dispatcher decision: filter allow / filter skip / v2.9.0 dedup
hold / dedup skip. The LogsPage "Announces" tab pulls from this
endpoint to surface structured decision data (with reasons,
filetypes, dedup outcomes) rather than free-text log lines.

Filters supported via query params:
  - decision: comma-separated subset of {allow, skip, hold}
  - reason: substring match against decision_reason
  - q: substring match against torrent_name / author_blob / category
  - limit: cap rows returned (default 200, max 1000)

The response includes `decision_counts` keyed on `decision` so the
UI can show "N Allow / M Skip / K Hold" without a second round-trip.
Counts honor the `q` and `reason` filters but NOT `decision` itself
(so the chips can show how many would be visible under each filter).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.database import get_db
from app.storage.grabs import parse_categories

router = APIRouter(prefix="/api/v1/announces", tags=["announces"])


class AnnounceRow(BaseModel):
    """One row of the announces table, shaped for the UI."""
    id: int
    seen_at: str
    torrent_name: str
    author_blob: str
    category: str
    filetype: str
    decision: str
    decision_reason: str
    matched_author: str
    # Every MAM content tag the announce carried (wave 5a, G124): explains
    # a `category_not_allowed` skip. None when only `category` is known.
    categories: Optional[list[str]] = None


class AnnouncesResponse(BaseModel):
    rows: list[AnnounceRow]
    total_matched: int
    decision_counts: dict[str, int]


@router.get("", response_model=AnnouncesResponse)
async def list_announces(
    limit: int = Query(200, ge=1, le=1000),
    decision: Optional[str] = Query(
        None,
        description="Comma-separated decisions to keep (allow, skip, hold).",
    ),
    reason: Optional[str] = Query(
        None,
        description="Substring match against decision_reason.",
    ),
    q: Optional[str] = Query(
        None,
        description="Substring match against torrent_name / author_blob / category.",
    ),
) -> AnnouncesResponse:
    """Return recent announces filtered by the supplied query params."""
    base_where: list[str] = []
    base_params: list[object] = []

    if reason:
        base_where.append("decision_reason LIKE ?")
        base_params.append(f"%{reason}%")

    if q:
        like = f"%{q}%"
        base_where.append(
            "(torrent_name LIKE ? OR author_blob LIKE ? OR category LIKE ?)"
        )
        base_params.extend([like, like, like])

    decisions: Optional[set[str]] = None
    if decision:
        wanted = {
            d.strip().lower()
            for d in decision.split(",")
            if d.strip()
        }
        wanted &= {"allow", "skip", "hold"}
        if wanted:
            decisions = wanted

    db = await get_db()
    try:
        base_sql = ""
        if base_where:
            base_sql = " WHERE " + " AND ".join(base_where)

        # Decision counts BEFORE applying the decision filter — so the
        # chips show how many would be visible if the user clicked each.
        count_sql = (
            "SELECT decision, COUNT(*) AS n FROM announces"
            + base_sql
            + " GROUP BY decision"
        )
        cur = await db.execute(count_sql, base_params)
        decision_counts = {
            (row["decision"] or "unknown"): int(row["n"])
            for row in await cur.fetchall()
        }

        # Now apply the decision filter for the rows query.
        rows_where = list(base_where)
        rows_params = list(base_params)
        if decisions:
            placeholders = ",".join("?" * len(decisions))
            rows_where.append(f"decision IN ({placeholders})")
            rows_params.extend(sorted(decisions))

        rows_sql_where = ""
        if rows_where:
            rows_sql_where = " WHERE " + " AND ".join(rows_where)

        total_cur = await db.execute(
            "SELECT COUNT(*) AS n FROM announces" + rows_sql_where,
            rows_params,
        )
        total_row = await total_cur.fetchone()
        total_matched = int(total_row["n"]) if total_row else 0

        rows_cur = await db.execute(
            "SELECT id, seen_at, torrent_name, author_blob, category, "
            "filetype, decision, decision_reason, matched_author, categories_json "
            "FROM announces"
            + rows_sql_where
            + " ORDER BY id DESC LIMIT ?",
            (*rows_params, limit),
        )
        rows = [
            AnnounceRow(
                id=int(r["id"]),
                seen_at=str(r["seen_at"] or ""),
                torrent_name=str(r["torrent_name"] or ""),
                author_blob=str(r["author_blob"] or ""),
                category=str(r["category"] or ""),
                filetype=str(r["filetype"] or ""),
                decision=str(r["decision"] or ""),
                decision_reason=str(r["decision_reason"] or ""),
                matched_author=str(r["matched_author"] or ""),
                categories=parse_categories(r["categories_json"]),
            )
            for r in await rows_cur.fetchall()
        ]
    finally:
        await db.close()

    return AnnouncesResponse(
        rows=rows,
        total_matched=total_matched,
        decision_counts=decision_counts,
    )
