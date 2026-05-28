# 0006. Treat MAM "not found in search results" as a permanent failure

- Status: Accepted
- Date: 2026-05-23

## Context

When the MAM search API returns `200` + `{"data": []}` for a `tor.id=<known_id>` query — an id Seshat has in its grabs history — the torrent existed at some past point but is no longer in MAM's index. Causes observed: uploader deleted it, staff removed it, it was trumped by a higher-quality re-upload, or it was moved to a restricted category.

Without special handling, a "missing quality metadata" query keeps returning these ids forever, feeding the runaway-loop failure mode described in [0005](0005-backfill-attempted-set.md).

## Decision

Distinguish permanent from transient failures and write a permanent marker only for the permanent class:

| Error from `get_torrent_info` | Treat as | Action |
| --- | --- | --- |
| `"not found in search results"` | **Permanent** | Write a stub row (`source="unavailable"`) so it leaves future "missing" queries |
| HTTP 5xx | Transient | Retry next run |
| HTTP 403 / empty response | Investigate session | Likely cookie/session issue — do not stub |
| Network exceptions | Transient | Retry next run |

The stub (`app/quality/pipeline.py::extract_for_torrent`) is observable in `quality_coverage_stats()` under `by_source.unavailable`. Writing a stub on a *transient* failure would mask data we could legitimately backfill later, so the discrimination matters.

## Consequences

- Deleted/removed torrents are recorded once and never retried, keeping external API volume bounded.
- This pattern handles *known* permanent failures; the attempted-set in [0005](0005-backfill-attempted-set.md) remains mandatory to catch everything else (including stub-write failures and novel error strings).
