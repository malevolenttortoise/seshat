# 0029. Filtered discovery rows are kept, hidden with a reason

- Status: Accepted (design 2026-10-12; built in the discovery fix pass)
- Date: 2026-10-12

## Context

The 2026-10 discovery audit sampled Mark's 9,284 missing books: about half didn't belong. The largest families were
comics and manga (452), box sets and omnibuses (431), anthologies, magazines and story collections (184 by title
alone; the sample found more with no tell in the title), untranslated editions (305), and smaller junk families
(splits, samplers, seller listings, study guides). Mark ruled comics and box sets filtered by default with a setting to
let them through (D2, D10), anthologies and collections filtered (D4, D9), the junk families filtered as bug fixes, and
a per-author switch that stops listing an author's missing books (D6).

Until now a filter meant *skip at merge*: `_is_book_set` titles were never written. That gives no way to show a
family later without re-scanning the sources (Goodreads book pages are the scarcest request Seshat makes), no record
of what was filtered, and nothing for the next scan or the Goodreads worker to recognise.

## Decision

- A book a discovery filter rejects is **created hidden, with a reason** (`books.hidden_reason`: `comic`, `box_set`,
  `anthology`, `not_english`, `split`, `sampler`, `listing`, `study_guide`, `junk`, `author_off`). Mark's own hides
  carry no reason.
- **Settings act on reasons, not on scans:** turning "Hide comics and manga" or "Hide box sets and omnibuses" off shows
  the rows with that reason at once; turning it back on hides them again. D6's switch hides an author's missing rows
  as `author_off` (only rows with no other contributor still listing) and shows them again when switched back on.
- **"Show" on one row is final:** it un-hides the row and marks the reason `shown`, so later scans and settings leave
  it alone.
- The Hidden page keeps the two apart: "Hidden by you" (default) and "Filtered", with reason chips.
- Filtered rows still take part in matching (the title identity, [ADR-0028](0028-one-volume-aware-title-identity.md)),
  so a later scan or the Goodreads worker recognises them instead of creating or re-checking them.
- **Not this mechanism:** books from an author who isn't scan-eligible, and OpenLibrary-only books (it matches and
  enriches but never creates a missing book), are not written at all.

## Consequences

- The database keeps the filtered rows (on the reference install after the cleanup: ~1,100 filtered by family plus
  434 `author_off`, growing with each scan).
  Accepted: a toggle that works without requests, and a visible record of what was filtered.
- Every list, count and query that means "missing books" must keep reading `hidden = 0`; a filtered row is hidden in
  every respect except the Hidden page's Filtered tab.
- A mis-filter (a real book flagged as an anthology) is recoverable from the Filtered tab and stays recovered.
- Existing installs get the same state through the cleanup Hygiene job (dry run first), not by a re-scan.

## Related

- [0021](0021-roster-gates-discovery-author-creation.md) — roster and scan eligibility (amended: scan eligibility is
  narrower than the linking roster).
- [0028](0028-one-volume-aware-title-identity.md) — the identity filtered rows still take part in.
