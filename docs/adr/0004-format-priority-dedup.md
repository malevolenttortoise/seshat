# 0004. Format-priority dedup against owned + in-flight + held siblings

- Status: Accepted
- Date: 2026-05-11 (shipped v2.9.0)

## Context

The same uploader sometimes posts multiple formats of one book minutes apart. On 2026-05-09 the Keleros "Delves"/"Duchy" incident grabbed all four uploads because the naive "owned check" couldn't fire — the first format hadn't reached *Owned* status before the second arrived. Dedup that only checks owned books is therefore insufficient.

## Decision

Gate single-format announces (`app/orchestrator/format_dedup.py::evaluate_format_dedup`) against **three** sibling sources, not just owned books:

1. `grabs` in non-terminal (in-flight) states
2. `pending_holds` rows in `pending` state
3. per-library `books` with `owned=1`, filtered by media type

Decision rules:

- **Enabled format** → always **allow**; preempt any held lower-priority sibling.
- **Disabled format + owned sibling** (any priority) → **skip**.
- **Disabled format + higher-priority in-flight or held sibling** → **skip**.
- **Disabled format alone** → **hold** for `format_dedup_hold_seconds` (default 600); replace any lower-priority hold for the same dedup key.

A 60s hold-release scheduler re-evaluates due holds against fresh state; a hold that has already paid its time penalty is treated as "grab now." The dedup key reuses the cross-library matcher's `match_key(first_author, title)` so announce keys line up with the keys used against per-library `books`.

`format_priority` is a per-media-type ordered list of `{fmt, enabled}` (order *is* priority); empty `{}` disables the gate entirely.

## Consequences

- Slow split-uploads no longer lose the preferred format.
- `pending_holds` does not auto-purge; at observed volume (~10 rows/day) this is negligible, but a TTL cleaner is a known future patch.
- Three inject paths expose an `override_format_dedup` flag (API-only, no UI checkbox — matches the `buy_personal_fl` convention).

## Related

- [0003](0003-bundle-dedup-prefer-duplicates.md) — bundle dedup; deliberately does **not** use this gate.

## Amendment (2026-10-12, discovery fix pass)

- **An IRC announce of a book already owned in that media type is never grabbed**, enabled format or not (Mark,
  D7). Until now rule (a) "owned sibling blocks" ran only in the disabled branch, so every epub / m4b announce of an
  owned book went through unless claim-for-owned caught it, and the claim skips rows already linked to MAM: 77 of
  1,946 snatched auto-grabs were owned books. The new skip (`owned_same_format`) runs in dispatch after claim-for-owned
  and before this gate; a held announce re-checks it at release.
- It covers IRC announces only. Send to pipeline, the inject / external-grab API and Manual Grab
  ([0023](0023-manual-grab-upload-rules-and-user-gates.md)) are the user's choice and go through.
- It steps aside in a library with active replacement on: there an owned book is still grabbed so the replacement
  detector can compare editions.
- Rule (a)'s owned lookup now matches by the title identity ([0028](0028-one-volume-aware-title-identity.md)) and any
  contributor (as claim-for-owned, [0013](0013-claim-for-owned-contributor-aware.md)), not `match_key` on the primary
  author.
