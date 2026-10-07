# 0023. Manual Grab: what an upload must prove, and which gates a user grab skips

- Status: Accepted
- Date: 2026-10-06

## Context

Manual Grab lets the user paste MAM links or drop `.torrent` files into Seshat (up to 30 per batch) and grab them. It is the first path where the bytes come from the user rather than from MAM, and the first where the user sees a full preview of each torrent before deciding.

Two facts shaped it:

- Every `.torrent` MAM serves carries `comment = "MID=<torrent id>,UID=<downloading account>"` (388 of 388 Seshat-grabbed files checked). The file's announce URL carries the downloader's passkey.
- Claim-for-owned and format-priority dedup run on every dispatch, including manual injects. They were dormant on manual grabs only because those callers sent no category. Manual Grab fills the metadata in, which would wake them.

## Decision

**An uploaded `.torrent` must prove it is the user's own MAM download.**

| Check | Outcome |
| --- | --- |
| No `MID=` in the comment | Refused: not a MAM `.torrent` |
| `UID=` is not the account's `user_status.uid` | Refused: downloaded by another MAM account (its passkey isn't ours). If our UID can't be read, the upload waits; never fail open |
| Torrent ID or info hash matches a blocking grab | Refused `already_grabbed`, no override ([0022](0022-one-mam-download-per-torrent.md)) |
| Search API says not found | Refused `torrent_removed_from_mam` |
| `my_snatched` | **Not** a refusal: the upload *is* the snatch |

An upload never calls MAM's download endpoint. Its bytes go through `submit_torrent_bytes` after its grab row is claimed under `grab_claim_lock()`. No wedge can apply: an app spends one only with `fl` on `download.php` (the download already happened), and MAM answers `bonusBuy.php?spendtype=personalFL` ("Buy as FL") from an app with "Not allowed via API" (found live 2026-10-07). A torrent wedged on MAM's site shows as personal FL once the search API catches up (5-20 min).

**A manual grab is the user's decision, informed by the preview.** Manual Grab skips claim-for-owned and format-priority dedup (never claimed, never held). The preview shows owned and in-flight siblings and leaves those rows unticked, so ticking one is the decision to grab. A `my_snatched` row is the one exception that asks twice: ticking it opens a confirm, and only that confirm sets `override_mam_snatched` for that row.

**MAM lookups are paced.** `tor.id` takes a single ID (MAM's API reference), so a batch needs one search call per row. Preview lookups, cover fetches and Grab all go through one server-side pacer at the `rate_mam` gap.

## Consequences

- A `.torrent` shared by a friend, or one from another tracker, can't be grabbed through Seshat. Non-MAM torrents still work through qBit directly (orphan adoption).
- While the cookie is down, uploads can't be confirmed (no UID to compare).
- Pasting a link for a book you own no longer quietly repairs its MAM link; announces still do that.
- `my_snatched` and `personal_freeleech` lag 5-20 min on MAM's side. A link pasted minutes after a browser download is downloaded again (the page warns; it can't guard). A personal-FL buy must mark its grab free directly rather than wait for the search API to say so.
- A 30-row batch takes about two minutes to fill in (lookup + cover per row) and about one more to grab.
