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

## Amendment (2026-10-21, built in wave B S3)

- **Reasons:** `comic`, `box_set`, `anthology`, `not_english`, `split`, `sampler`, `listing`, `study_guide`,
  `not_a_book` (colouring books, notebooks, RPG rulebooks, prepacks; G198), `junk`, plus `shown` and `author_off`.
  Seller listings include format and edition words ("[Unabridged Audiobook]", "Signed", "*Very Special Edition*").
  The rules (`app/discovery/filters.py`) were held to no match among the reference install's 3,775 owned titles
  outside the families Mark owns real members of (box sets, anthologies, manga).
- **Books match only within their filter family** (G197), and the family used for matching is **read from the title
  alone, the same way for a stored row and an incoming book.** A family known only from a book's description, its
  4+ credited authors, its series name ("Lucifer (2015-2017)") or a page count ("#2" at 30 pages is an issue) decides
  whether a *new* row is created hidden, never what a book matches: those signals depend on the source (a stored row
  keeps no contributor list; sources name a comic's run differently), and the F23 replay showed 20 books that would
  have been created again as hidden copies of the row they are. So a hidden comic row never absorbs the prose book,
  a filtered book never merges into a listed one, and existing rows are left as they are (wave C's cleanup re-applies
  the rules to them).
- **A "Series #N" title** ("The Expanse #3") is junk unless the author owns a book of the same stem ("The Last
  Paladin #7"). "Title: Series #N" ("Organ Donor: Afterlife of Ebon Black #1") is a book: the old skip of every
  title ending " #N" dropped it.
- **A superhero's name before the title's subtitle marks a comic** unless the title says novel / novelization; a
  prose tie-in that doesn't (Jim Butcher's "Spider-Man: The Darkest Hours") is hidden as a comic and recoverable from
  the Filtered tab.
- Settings: `hide_comics` and `hide_box_sets` (on by default) in settings.json; saving either re-applies it to that
  family's unowned rows in every library.

## Amendment (2026-10-22, wave C S8: the missing-books cleanup)

- **Its own action, Preview → Apply** (G209), not a job in the Hygiene chain: the chain has no preview and makes
  network requests; the cleanup deletes rows and makes none. Steps, in order: remove missing books none of whose
  authors is scan-eligible in that library (owning books in the other library doesn't count, G212; the List-missing
  switch doesn't either, so a switched-off author's books stay); remove books only OpenLibrary knows, where a MAM
  torrent found for the row counts as another source (G210); hide the filtered families on stored rows with the
  creation verdict (title, description, series name, page count, a stored Goodreads page's genres); fold a missing
  copy into the one owned library book it matches ([ADR-0028](0028-one-volume-aware-title-identity.md), amended);
  file a standalone book in the series its title names; merge the source caches again under the identity (F13).
- **Step 6 re-merges as a cache-only scan would**, from what is stored: each scan-eligible author's Amazon cache
  (the scan's author check, the source's scan switch, a blacklisted author record and the library-only setting all
  stop it), and the Goodreads candidates the worker merged into an existing row, from their stored pages, under
  the worker's own rules (a book whose Goodreads id a row already holds is known and stays where it is: for a book
  Amazon renamed, the id is the only link). Only authors a scan has visited: the worker caches every library author,
  and merging an unscanned one is that author's first scan (~21,000 rows on the reference install), the re-scans'
  job. No author-row writes (`_merge_result(touch_author=False)`). Ids the old merge left on a wrong row stay there
  (moving them, measured, landed on a duplicate about as often as on the book).
- **Never touched:** owned books (except as a fold's winner), books hidden with no reason (yours), `shown` and
  `author_off` books, and, for the removals, books a grab is linked to.
- **Preview runs the steps on copies** of every library DB and `seshat.db` (SQLite backup), with the cleanup task's
  own connections pointed at them; the source caches are read in place and never written. Apply needs a preview
  from the last hour. Measured on the reference install (2026-10-22 copies): 667 removed (no scan-eligible author),
  824 removed (OpenLibrary only; 523 kept by a MAM torrent), 1,354 hidden, 9 folded (22 left listed as ambiguous),
  72 filed, 1,158 created by step 6 (700 hidden by the filters; skipped: 4 blacklisted Amazon records, 19 authors
  failing the author check, 62 known Goodreads ids); preview 13 s, the copies and caches unchanged.

## Amendment (2026-10-23, wave D S28: what wave C's filters missed)

- **Foreign titles with no edition marker** (G233): beside the marker and word-list rules (now with more Italian,
  Spanish, Portuguese, German, Polish and Czech words, held to no owned title), a title is `not_english` when every
  word of it (author names, possessives and short words left out) is one no English blurb uses, two or more of them
  ("Gefallene Sonnen", "Un amore diabolico", "Vangin valtaistuin"), or when its blurb is in another language and a
  word of the title is unseen ("Hechicero", "Preludio a Dune"). The blurb alone isn't enough: Hardcover files English
  titles with a translation's blurb ("The Wrath of Mulgarath"). The vocabulary is a fixed file,
  `app/discovery/wordlists/english_words.txt` (~39,000 words from the reference install's 8,700 English blurbs,
  `tools/build_english_words.py`): built per install it would be empty on a fresh one and every title would look
  foreign; a missing file turns the test off. The vocabulary test runs last, after every other family, so an English
  comic issue named with invented words ("Hopcross Jilly #2") keeps its family.
- **The Goodreads worker checks the title before an autocomplete** (G234): a foreign title is set aside
  (`not_english`) without the request, and the next candidate's autocomplete isn't held for it.
- **Box sets:** "The Complete <Name>" (not "Complete Guide / Book of"), word and Roman ranges ("Books I-V", "Books One
  Through Six"), set names beside "Trilogy" ("Quintet", "Quartet", "Duology"…, not "Part I of an … Duology"), G235;
  a list of titles after the last colon, three or more, two of them 2+ words or after a set word ("The Wax & Wayne
  Series: Alloy of Law, Shadows of Self, …"), G236. Not a list: a description's ("A Paranormal Romance with …, …"),
  genre words ("LitRPG, Progression Fantasy, …"), volume marks ("…, Book 1"), a byline ("by Card, Orson Scott, …"),
  or a title worded as an anthology, sampler or listing.
- Also: "Word Search" is not a book; a web novel's "Chapter 001-050" is a split; a comic sold under its author's name
  with an issue number ("Patricia Briggs' Alpha & Omega: Cry Wolf #2") is a comic (C43, C45).
- Measured on 2026-10-23 copies, old vs new verdict over every stored title: 0 owned titles change; listed rows newly
  `not_english` 134 (about 120 real translations, the rest English books with rare or Latin titles, "Sua Sponte",
  "Secondhand Spaceman", "Tripoint", accepted: recoverable, and a later copy with a fuller title is listed again),
  `box_set` 23, `comic` 5, `split` 5. The cleanup's preview hides 172 rows in step 3 and creates the Cry Wolf / Moon
  Called comic issues hidden instead of listed.

## Amendment (2026-10-23, wave D S30: books listed twice)

- **The missing-books cleanup folds listed copies of one book into each other** (G237), a new step after the
  owned-copy fold: rows sharing a contributor that the title identity calls one book. The row more sources know
  (then the older) is kept; ids, links, contributors and grab links carry over (`merge_books`). Not folded (C44): a
  match on series position alone (a title that is only its series name: "Star Trek: New Frontier" and "The Returned,
  Part 1" both filed at #1), and a bare "X" against "X: Y" whose "Y" isn't the series ("Without Warning" and
  "Without Warning - After America", "Dark Faith" and "Dark Faith: Invocations"; "Zero Point" and "Zero Point: The
  Owner: Book Two" fold). Family, omnibus and two-series checks were measured and left out: no change, or only right
  folds lost (Tithe, KTF, Takhini Wolves…).
- Reading the step's folds found identity calls that would fold two books (ADR-0028, amended) and, beside G230, that
  a series parenthetical naming a position ("Far Thoughts and Pale Gods (The Complete Short Fiction of Greg Bear Book
  2)") marked a book an omnibus, so the cache re-merge re-created it beside its copy.
- Measured on 2026-10-23 copies: 183 folds, every one read (about 180 right; wrong: "Marvel 1602: Fantastick Four"
  into "Fantastic Four", two unflagged comic rows).

## Amendment (2026-10-23, wave D S30b: Mark's sort of the cleanup preview)

- **An orientation edition is listed hidden as a listing** (G246): "Birds of Prey - Bisexual Edition (Pinwheel Book
  20)" when Mark owns the other cut. "Bisexual / Bi / Gay / Lesbian / Queer / Hetero(sexual) / Straight Edition" marks
  it at creation and in the cleanup's step 3, only when no other family does; matching (`book_class`) reads the title
  without the rule, so the edition stays its own row beside the owned one and announces are unaffected. An owned row is
  never filtered ("Human Resources: Hetero Edition" stays as it is).
- **C45 also takes a volume:** the author's possessive name and "Vol. N" / "Volume N" mark a comic ("Patricia Briggs'
  Mercy Thompson: Moon Called Vol. 1", "Brandon Sanderson's White Sand Vol. 2"). Amazon's lone "(Volume N)" tag doesn't
  ("Matt Forbeck's Brave New World: Revolution (Volume 1)" is a novel), nor does a magazine ("Isaac Asimov's Science
  Fiction Magazine, Volume 4").
- **A last part that is only a byline is a listing**, whoever it names: "Dracula : by Bram Stoker", which Amazon filed
  under Holly Black. The "by" must follow a colon or dash and be lower case (a title-case subtitle starts "By": "Learn
  German: By Reading Urban Fantasy"). Without the separator real titles do it ("Milked by Her Master", "On The
  Anatomization of an Unknown Man (1637) by Frans Mier"), so "Dracula by Bram Stoker" stays listed.
- Measured on the 2026-10-23 post-update copies (verdicts over every stored title): 4 listed comics hidden (Dresden
  Files, White Sand, Moon Called), 2 Dresden comic omnibuses `box_set` → `comic`, the edition `listing`; 0 other owned
  changes.

## Related

- [0021](0021-roster-gates-discovery-author-creation.md) — roster and scan eligibility (amended: scan eligibility is
  narrower than the linking roster).
- [0028](0028-one-volume-aware-title-identity.md) — the identity filtered rows still take part in.
