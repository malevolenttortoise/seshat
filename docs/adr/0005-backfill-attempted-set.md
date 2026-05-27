# 0005. Backfill workers track an in-process attempted-set

- Status: Accepted
- Date: 2026-05-23

## Context

A backfill worker that pulls work from a "missing X" query assumes every failure path writes something that excludes the item from the next query. Many fail-soft paths don't. When the work function returns `None`/fails silently without writing an exclusion marker, the query keeps returning the same failing IDs and the loop spins at event-loop speed.

This actually happened: v2.25.0's quality-metadata backfill looped forever on two deleted-from-source torrents whose extract returned `None` without writing a stub. Because pacing only fired on `processed` (not `skipped`) iterations, it issued ~5800 unnecessary external API calls in 10 minutes before the operator stopped it.

## Decision

Every backfill/retry worker that pulls from a "missing X" query MUST track an in-process `attempted: set` and halt when a batch contains only previously-attempted IDs:

```python
attempted: set[str] = set()
while not cancel_requested:
    batch = await get_missing_things(limit=N)
    if not batch:
        break
    new_in_batch = [i for i in batch if i not in attempted]
    if not new_in_batch:
        log.info("worker: only previously-attempted IDs remain, halting")
        break
    for item in new_in_batch:
        attempted.add(item)
        await do_work(item)
```

The attempted-set is a **safety belt for unanticipated failure modes**. It composes with — but does not replace — writing a permanent marker on known failures (see [0006](0006-mam-not-found-is-permanent.md)). The set must always be present regardless of whether a per-failure marker exists.

## Consequences

- Worst case for a novel failure mode is one wasted pass over the batch, not an unbounded loop.
- Anti-pattern to avoid: relying solely on the "missing X" query to exclude already-attempted items.

## Related

- [0006](0006-mam-not-found-is-permanent.md) — the companion permanent-marker pattern.
