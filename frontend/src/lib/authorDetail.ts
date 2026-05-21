// Shared loader for the author-detail page (desktop + mobile).
//
// v2.20.0 — when an author row is linked to a `persons` row (the new
// cross-library identity table), this loader fetches the unified
// `/discovery/persons/{person_id}` view and adapts it to the existing
// `AuthorDetail` shape the components already render. The adapter
// preserves the slug-scoped view the user navigated from (so clicking
// "Calibre tab" still shows Calibre as primary), while letting the
// other library blocks under `cross_library{}` come from the same
// person via author_links — accurate even when names normalize
// differently (C.W. Lamb vs Charles W. Lamb merge correctly).
//
// Falls back to the legacy /authors/{aid} response when the row isn't
// yet linked (e.g. a row inserted between sync hooks before the next
// migration sweep catches up).
import { api } from "../api";
import type { Author, Book, Series } from "../types";

// Matches `_author_detail_for_slug` output + the `person_id` field
// that /authors/{aid} now stamps additively in v2.20.0.
interface PerLibraryAuthorBlock extends Author {
  series?: Series[];
  standalone_books?: Book[];
}

interface CrossLibraryEntry {
  library_name: string;
  content_type: string;
  app_type?: string;
  author: PerLibraryAuthorBlock;
}

export interface AuthorDetail extends PerLibraryAuthorBlock {
  active_library_slug?: string;
  active_content_type?: string;
  cross_library?: Record<string, CrossLibraryEntry>;
  global_stats?: {
    owned: number;
    missing: number;
    total: number;
    series_count: number;
  };
  person_id?: number | null;
  // v2.20.0 Phase 3 — present when the loader fetched via
  // /persons/{person_id}. The badge row consumes this. Empty object
  // when the loader fell back to the legacy /authors/{aid} path
  // (page renders without the badge row).
  source_ids?: Record<string, string | null>;
  low_confidence?: boolean;
}

// Shape returned by /discovery/persons/{person_id}.
export interface PersonDetail {
  person_id: number;
  canonical_name: string;
  display_name: string;
  normalized_name: string;
  display_name_override: string | null;
  bio: string | null;
  image_url: string | null;
  source_ids: Record<string, string | null>;
  libraries: {
    library_slug: string;
    library_name: string;
    content_type: string;
    app_type: string;
    author_id: number;
    author: PerLibraryAuthorBlock;
  }[];
  pen_names: {
    link_id: number;
    person_id: number;
    canonical_name: string;
    display_name: string;
    link_type: string;
    direction: "alias_of_this" | "this_is_alias_of";
  }[];
  global_stats: {
    owned: number;
    missing: number;
    total: number;
    series_count: number;
  };
  low_confidence: boolean;
}

// v2.20.0 Phase 4 — search result shape returned by
// /discovery/persons/search.
export interface PersonHit {
  person_id: number;
  canonical_name: string;
  display_name: string;
  normalized_name: string;
  library_slugs: string[];
  author_ids_by_slug: Record<string, number>;
  content_types: string[];
}

export interface PersonSearchResponse {
  q: string;
  persons: PersonHit[];
}


/**
 * Adapt a /persons/{person_id} response to the AuthorDetail shape the
 * existing author-detail components render. `preferredSlug` selects
 * which library's block lands at the top level; remaining libraries
 * populate `cross_library{}`. When `preferredSlug` doesn't match any
 * library in the response, the first library is used as primary.
 */
export function adaptPersonToAuthorDetail(
  person: PersonDetail,
  preferredSlug: string | null,
): AuthorDetail | null {
  if (person.libraries.length === 0) return null;
  const primary =
    person.libraries.find((l) => l.library_slug === preferredSlug) ||
    person.libraries[0];
  const cross: Record<string, CrossLibraryEntry> = {};
  for (const lib of person.libraries) {
    if (lib.library_slug === primary.library_slug) continue;
    cross[lib.library_slug] = {
      library_name: lib.library_name,
      content_type: lib.content_type,
      app_type: lib.app_type,
      author: lib.author,
    };
  }
  return {
    ...primary.author,
    active_library_slug: primary.library_slug,
    active_content_type: primary.content_type,
    cross_library: cross,
    global_stats: person.global_stats,
    person_id: person.person_id,
    source_ids: person.source_ids,
    low_confidence: person.low_confidence,
    // The canonical name from the person row beats the per-library
    // name when they differ — Phase 1 consolidation picks the
    // source-ID-densest variant, which is what the user expects to
    // see as the primary heading.
    name: person.display_name || primary.author.name,
    bio: person.bio ?? primary.author.bio,
    image_url: person.image_url ?? primary.author.image_url,
  };
}

/**
 * Two-step fetch: resolve person_id via /authors/{aid}, then fetch
 * /persons/{person_id} for the unified view. Falls back to the
 * legacy /authors/{aid} response when person_id is null (author not
 * yet linked).
 */
export async function loadAuthorDetailViaPerson(
  authorIdNum: number,
  authorSlug: string | null,
  signal?: AbortSignal,
): Promise<AuthorDetail> {
  const qs = authorSlug
    ? `?include_cross_library=1&slug=${encodeURIComponent(authorSlug)}`
    : `?include_cross_library=1`;
  const legacy = await api.get<AuthorDetail>(
    `/discovery/authors/${authorIdNum}${qs}`,
    signal,
  );
  if (legacy.person_id == null) {
    // Row isn't in the identity graph yet — render via the legacy
    // shape (which already includes accurate-enough cross_library via
    // the v2.20.0 author_links-first fanout).
    return legacy;
  }
  try {
    const person = await api.get<PersonDetail>(
      `/discovery/persons/${legacy.person_id}`,
      signal,
    );
    const adapted = adaptPersonToAuthorDetail(person, authorSlug);
    return adapted ?? legacy;
  } catch (e) {
    if (api.isAbort(e)) throw e;
    // Person fetch failed for a non-abort reason (e.g. transient 5xx).
    // Fall back to the legacy response so the page still renders.
    return legacy;
  }
}
