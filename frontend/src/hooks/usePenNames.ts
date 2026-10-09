// An author's pen-name / co-author links: the list, the person search
// that finds an alias to link, and link / unlink.
//
// DiscAuthorDetailPage + MobileAuthorDetailPage each carried this logic
// (wave 5b, issue 22; S4a first made the two copies send the same
// requests). The shells keep the presentation: the link-type picker,
// confirm prompts and toast wording. `link` / `unlink` throw on failure
// so each shell reports it its own way.
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { PersonHit, PersonSearchResponse } from "../lib/authorDetail";
import type { LinkType, PenNameLink, PenNamesResponse } from "../types";

export interface UsePenNamesOptions {
  /** The per-library author id (0 = none: nothing is loaded). */
  authorIdNum: number;
  /** The author's canonical person, left out of search results. */
  personId: number | null | undefined;
  /** Runs after a link lands, before the search clears (the pages reload the author). */
  onLinked?: () => Promise<unknown> | void;
}

export interface UsePenNames {
  penLinks: PenNameLink[];
  penQ: string;
  setPenQ: (q: string) => void;
  /** Person hits for `penQ` (2+ characters, 300ms debounce), minus this author. */
  penResults: PersonHit[];
  /** A link or unlink is in flight. */
  penBusy: boolean;
  clearSearch: () => void;
  /** Link `aliasPersonId` to `personId`; needs a `personId`. */
  link: (aliasPersonId: number, linkType: LinkType | string) => Promise<void>;
  unlink: (linkId: number) => Promise<void>;
}

export function usePenNames({ authorIdNum, personId, onLinked }: UsePenNamesOptions): UsePenNames {
  const [penLinks, setPenLinks] = useState<PenNameLink[]>([]);
  const [penQ, setPenQ] = useState("");
  const [penResults, setPenResults] = useState<PersonHit[]>([]);
  const [penBusy, setPenBusy] = useState(false);

  const linksUrl = `/discovery/authors/${authorIdNum}/pen-names`;

  useEffect(() => {
    if (!authorIdNum) return;
    api
      .get<PenNamesResponse>(`/discovery/authors/${authorIdNum}/pen-names`)
      .then((r) => setPenLinks(r.links || []))
      .catch(() => {});
  }, [authorIdNum]);

  useEffect(() => {
    if (penQ.length < 2) {
      setPenResults([]);
      return;
    }
    const tm = setTimeout(() => {
      api
        .get<PersonSearchResponse>(
          `/discovery/persons/search?q=${encodeURIComponent(penQ)}`,
        )
        .then((r) =>
          setPenResults(
            (r.persons || []).filter((x) => x.person_id !== personId),
          ),
        )
        .catch(() => {});
    }, 300);
    return () => clearTimeout(tm);
  }, [penQ, personId]);

  const clearSearch = useCallback(() => {
    setPenQ("");
    setPenResults([]);
  }, []);

  const link = async (aliasPersonId: number, linkType: LinkType | string) => {
    setPenBusy(true);
    try {
      await api.post("/discovery/persons/link-pen-names", {
        canonical_person_id: personId,
        alias_person_id: aliasPersonId,
        link_type: linkType,
      });
      // Refresh both the legacy per-library chip list AND the unified
      // person view (which reads pen_names from v2): `onLinked` reloads
      // the page-level author, pulling in the fresh person.pen_names.
      const r = await api.get<PenNamesResponse>(linksUrl);
      setPenLinks(r.links || []);
      await onLinked?.();
      setPenQ("");
      setPenResults([]);
    } finally {
      setPenBusy(false);
    }
  };

  const unlink = async (linkId: number) => {
    setPenBusy(true);
    try {
      await api.del(`/discovery/authors/pen-name-link/${linkId}`);
      // Re-read the list: what the server holds now.
      const r = await api.get<PenNamesResponse>(linksUrl);
      setPenLinks(r.links || []);
    } finally {
      setPenBusy(false);
    }
  };

  return { penLinks, penQ, setPenQ, penResults, penBusy, clearSearch, link, unlink };
}
