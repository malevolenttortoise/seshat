# 0024. Batch grabs from the app run as background jobs (start, then poll)

- Status: Accepted
- Date: 2026-10-08

## Context

Two in-app paths grab many torrents in one request: Discovery's **send to pipeline**
(`POST /api/discovery/send-to-pipeline`, any number of books, also the sidebar's single-book
send) and the Tentative page's **bulk approve** (`POST /api/v1/tentative/bulk/approve`, no
cap; no ids = every pending row). Each item costs up to two MAM requests (a torrent-info
lookup and the `.torrent` download).

Before the 2026-10 audit both ran synchronously and unpaced. A 31-row bulk approve already took
about two minutes in one request, past what the reverse proxy in front of
`seshat.deepstonecrypt.org` waits (Cloudflare ~100s, nginx ~60s): the server finished the batch
while the page reported an error. The audit then put every MAM request through one pacer
(issue 05, ADR-0023 amended: one at a time, `rate_mam` apart), which makes these batches slower
still: about 6 seconds per item at the default 3-second gap.

Manual Grab's Grab all already solved the same problem (its D14): the request starts an
in-memory job and returns at once; the page polls for per-row status.

## Decision

Both endpoints become jobs, on the same in-memory machinery as Manual Grab
(`app/orchestrator/jobs.py`, shared with it):

| | Start | Poll |
| --- | --- | --- |
| Send to pipeline | `POST /api/discovery/send-to-pipeline` (body unchanged) | `GET /api/discovery/send-to-pipeline/{job_id}` |
| Tentative bulk approve | `POST /api/v1/tentative/bulk/approve` (body unchanged) | `GET /api/v1/tentative/bulk/approve/{job_id}` |

Start and poll both return the job:

```json
{"job_id": "…", "done": false, "total": 31, "completed": 12,
 "rows": [{"…": "per-row status: pending | working | done, ok, error"}],
 "result": null, "error": null}
```

- `result` is set when `done`, and is **exactly the old response** (`sent` / `skipped` /
  `failed` / `message` / `results` for send to pipeline; `processed` / `failed` / `errors` for
  bulk approve), so callers read the same fields as before.
- Checks that fail before any grab still answer at once with their old errors (400, 404, 503).
  A request with nothing to grab (no Found books, no pending rows) answers with a job that is
  already done, its `result` the old answer.
- An unknown or expired `job_id` (the process restarted, or the job finished over an hour ago) is
  404 with Manual Grab's wording: "No such batch. Seshat may have restarted; anything it had
  already grabbed is in the grab history."
- Every send goes through the job, including the sidebar's single-book send: one shape for the
  endpoint, and the frontend helper (`runBatchJob`) hides the polling.
- **Unchanged:** `POST /api/v1/grabs/inject-batch` stays synchronous. It is the contract for
  external callers (bookmarklets, scripts) and has never been called on the live container; it
  is paced like everything else, so a large batch is slower. Tentative bulk reject and dismiss
  stay synchronous (no MAM requests).

## Consequences

- No more proxy errors on long batches; the page waits on short polls instead of one request.
- Jobs live in memory, as Manual Grab's do: a restart loses the rows a job hadn't reached
  (every row it did reach is an ordinary grab, in the grab history), and an open page's next poll
  gets the 404 above.
- This changes the HTTP shape of the two POSTs, the escape hatch the strict
  behaviour-preservation rule allows (audit PRD carried constraints). Any external caller of these
  two in-app endpoints, none known, would have to poll.
- A single-book send now takes one extra round trip (≤1.5s) before the sidebar reports it.
