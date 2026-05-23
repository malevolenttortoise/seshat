// v2.20.0 Phase 5 — author identity triage UI.
//
// Surfaces three buckets the Phase 1 migration + ongoing identity
// graph can produce:
//   - Low-confidence persons (multiple library rows with no shared
//     source IDs — likely "John Smith" collisions).
//   - Unlinked authors (per-library rows that aren't in author_links
//     yet — usually sync-hook race conditions).
//   - Normalized-name collisions (multiple persons share the same
//     normalized_name — legacy data from before the UNIQUE index).
//
// Each row offers manual link/unlink controls. The page also exposes
// a "recompute consolidation" button that re-runs the Phase 1
// tiebreak + low-confidence flagging pass.
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { toast } from "../lib/toast";
import { useTheme } from "../theme";
import type { NavFn } from "../types";

interface TriageLink {
  library_slug: string;
  author_id: number;
  link_confidence: "high" | "low";
  author_name?: string | null;
}

interface LowConfidencePerson {
  person_id: number;
  canonical_name: string;
  display_name: string;
  normalized_name: string;
  links: TriageLink[];
}

interface UnlinkedAuthor {
  library_slug: string;
  author_id: number;
  name: string;
  normalized_name: string | null;
}

interface NormalizedCollision {
  normalized_name: string;
  persons: {
    person_id: number;
    canonical_name: string;
    display_name: string;
  }[];
}

interface TriageResponse {
  low_confidence: LowConfidencePerson[];
  unlinked_authors: UnlinkedAuthor[];
  normalized_collisions: NormalizedCollision[];
}


interface AuthorTriagePageProps {
  onNav?: NavFn;
}


export default function AuthorTriagePage({ onNav }: AuthorTriagePageProps) {
  const t = useTheme();
  const [data, setData] = useState<TriageResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const r = await api.get<TriageResponse>("/discovery/persons/triage");
      setData(r);
    } catch (e) {
      toast.error((e as Error).message || "Failed to load triage");
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const approveLinks = async (person_id: number, display_name: string) => {
    if (
      !confirm(
        `Approve ${display_name}'s links? This confirms the linked ` +
        `library rows really are the same person and exempts them from ` +
        `future low-confidence flagging (survives recompute).`,
      )
    ) return;
    setBusy(true);
    try {
      const r = await api.post<{ status: string; approved: number }>(
        `/discovery/persons/${person_id}/approve-links`,
      );
      toast.success(`Approved ${r.approved} link(s)`);
      await refresh();
    } catch (e) {
      toast.error((e as Error).message || "Approve failed");
    }
    setBusy(false);
  };

  const unlinkAuthorFromPerson = async (
    person_id: number, library_slug: string, author_id: number,
  ) => {
    if (
      !confirm(
        `Unlink ${library_slug}/${author_id} from this person? ` +
        `A new person will be created for it.`,
      )
    ) return;
    setBusy(true);
    try {
      const r = await api.post<{
        status: string;
        new_person_id: number;
        old_person_dropped: boolean;
      }>(
        `/discovery/persons/${person_id}/unlink-author`,
        { library_slug, author_id },
      );
      toast.success(
        `Unlinked → new person ${r.new_person_id}` +
        (r.old_person_dropped ? " (old person dropped)" : ""),
      );
      await refresh();
    } catch (e) {
      toast.error((e as Error).message || "Unlink failed");
    }
    setBusy(false);
  };

  const linkAuthorToPerson = async (
    target_person_id: number, library_slug: string, author_id: number,
  ) => {
    setBusy(true);
    try {
      const r = await api.post<{
        status: string;
        old_person_dropped: boolean;
      }>(
        `/discovery/persons/${target_person_id}/link-author`,
        { library_slug, author_id },
      );
      if (r.status === "already_linked") {
        toast.info("Already linked");
      } else {
        toast.success(
          `Linked to person ${target_person_id}` +
          (r.old_person_dropped ? " (orphan source person dropped)" : ""),
        );
      }
      await refresh();
    } catch (e) {
      toast.error((e as Error).message || "Link failed");
    }
    setBusy(false);
  };

  const mergePersons = async (
    canonical_person_id: number, alias_person_id: number,
  ) => {
    // "Merge" = walk each author_link on the alias person and re-point
    // it at the canonical. The link-author endpoint handles each row
    // including dropping the alias when its last link moves.
    if (
      !confirm(
        `Merge person ${alias_person_id} INTO person ${canonical_person_id}? ` +
        `All linked authors will be moved to the canonical person.`,
      )
    ) return;
    setBusy(true);
    try {
      // Fetch alias person's links via the detail endpoint.
      const detail = await api.get<{
        libraries: { library_slug: string; author_id: number }[];
      }>(`/discovery/persons/${alias_person_id}`);
      for (const lib of detail.libraries) {
        await api.post(
          `/discovery/persons/${canonical_person_id}/link-author`,
          { library_slug: lib.library_slug, author_id: lib.author_id },
        );
      }
      toast.success(`Merged ${detail.libraries.length} link(s)`);
      await refresh();
    } catch (e) {
      toast.error((e as Error).message || "Merge failed");
    }
    setBusy(false);
  };

  const recompute = async () => {
    if (
      !confirm(
        "Re-run the Phase 1 consolidation pass? This re-picks canonical_name/" +
        "bio/image_url tiebreaks and recomputes low_confidence flags.",
      )
    ) return;
    setBusy(true);
    try {
      const r = await api.post<{ flagged: number }>(
        "/discovery/persons/recompute-consolidation",
      );
      toast.success(`Re-flagged ${r.flagged} low-confidence link(s)`);
      await refresh();
    } catch (e) {
      toast.error((e as Error).message || "Recompute failed");
    }
    setBusy(false);
  };

  if (loading) {
    return (
      <div style={{ padding: 24, color: t.text }}>Loading triage…</div>
    );
  }
  if (!data) {
    return (
      <div style={{ padding: 24, color: t.text }}>Failed to load triage.</div>
    );
  }

  return (
    <div style={{ padding: "12px 0", color: t.text }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 16,
        }}
      >
        <h1 style={{ fontSize: 24, fontWeight: 700, margin: 0 }}>
          Author Identity Triage
        </h1>
        <div style={{ display: "flex", gap: 8 }}>
          <button
            type="button"
            onClick={recompute}
            disabled={busy}
            style={btnStyle(t, "neutral")}
          >
            Re-run consolidation
          </button>
          <button
            type="button"
            onClick={refresh}
            disabled={busy}
            style={btnStyle(t, "neutral")}
          >
            Refresh
          </button>
        </div>
      </div>

      <p style={{ fontSize: 13, color: t.td, lineHeight: 1.5 }}>
        Cross-library identity issues from the persons / author_links
        graph. Use the controls to manually fix linkages. Unlinking
        creates a new person row for the orphaned author; linking
        moves a per-library row to a different person (dropping the
        source person if it becomes empty).
      </p>

      <Bucket
        title="Low-confidence links"
        count={data.low_confidence.length}
        empty="No low-confidence links — every multi-linked person shares at least one source ID with itself."
        t={t}
      >
        {data.low_confidence.map((p) => (
          <PersonCard key={p.person_id} t={t} subdued={busy}>
            <div style={{ fontWeight: 600 }}>
              {p.display_name}{" "}
              <span style={{ color: t.tg, fontSize: 11 }}>
                (person #{p.person_id})
              </span>
            </div>
            <div style={{ fontSize: 11, color: t.tg, marginBottom: 6 }}>
              normalized: <code>{p.normalized_name}</code>
            </div>
            <table style={{ width: "100%", fontSize: 12 }}>
              <tbody>
                {p.links.map((l) => (
                  <tr key={`${l.library_slug}:${l.author_id}`}>
                    <td>
                      <div style={{ fontWeight: 500 }}>
                        {l.author_name ?? <span style={{ color: t.tg, fontStyle: "italic" }}>name unavailable</span>}
                      </div>
                      <div style={{ fontSize: 10, color: t.tg }}>
                        {l.library_slug} / author #{l.author_id}
                      </div>
                    </td>
                    <td style={{ color: t.redt, fontSize: 10 }}>
                      {l.link_confidence}
                    </td>
                    <td style={{ textAlign: "right" }}>
                      <button
                        type="button"
                        onClick={() => unlinkAuthorFromPerson(
                          p.person_id, l.library_slug, l.author_id,
                        )}
                        disabled={busy}
                        style={btnStyle(t, "warn", "sm")}
                      >
                        Unlink → new person
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
              <button
                type="button"
                onClick={() => approveLinks(p.person_id, p.display_name)}
                disabled={busy}
                style={btnStyle(t, "approve", "sm")}
                title="Confirm these links are the same person — exempts from future flagging"
              >
                ✓ Approve as same person
              </button>
              {onNav ? (
                <button
                  type="button"
                  onClick={() => onNav("disc-author-detail", p.links[0]?.author_id)}
                  style={btnStyle(t, "neutral", "sm")}
                >
                  Open detail
                </button>
              ) : null}
            </div>
          </PersonCard>
        ))}
      </Bucket>

      <Bucket
        title="Unlinked per-library authors"
        count={data.unlinked_authors.length}
        empty="Every per-library author has an author_links row."
        t={t}
      >
        {data.unlinked_authors.map((a) => (
          <PersonCard
            key={`${a.library_slug}:${a.author_id}`}
            t={t}
            subdued={busy}
          >
            <div style={{ fontWeight: 600 }}>{a.name}</div>
            <div style={{ fontSize: 11, color: t.tg, marginBottom: 6 }}>
              {a.library_slug} / author #{a.author_id}
              {a.normalized_name ? (
                <>
                  {" "}· normalized: <code>{a.normalized_name}</code>
                </>
              ) : null}
            </div>
            <p style={{ fontSize: 12, color: t.td, margin: 0 }}>
              No person row. Trigger a sync or use the
              "Recompute consolidation" button to backfill.
            </p>
          </PersonCard>
        ))}
      </Bucket>

      <Bucket
        title="Normalized-name collisions"
        count={data.normalized_collisions.length}
        empty="No persons share a normalized_name."
        t={t}
      >
        {data.normalized_collisions.map((c) => (
          <PersonCard key={c.normalized_name} t={t} subdued={busy}>
            <div style={{ fontWeight: 600 }}>
              <code>{c.normalized_name}</code>{" "}
              <span style={{ color: t.tg, fontSize: 11 }}>
                ({c.persons.length} persons share this)
              </span>
            </div>
            <div style={{ marginTop: 6 }}>
              {c.persons.map((p) => (
                <div
                  key={p.person_id}
                  style={{
                    display: "flex",
                    gap: 8,
                    padding: "4px 0",
                    alignItems: "center",
                  }}
                >
                  <span style={{ flex: 1, fontSize: 12 }}>
                    {p.display_name}{" "}
                    <span style={{ color: t.tg, fontSize: 10 }}>
                      (person #{p.person_id})
                    </span>
                  </span>
                  {p === c.persons[0] ? (
                    <span style={{ fontSize: 11, color: t.grnt }}>
                      canonical
                    </span>
                  ) : (
                    <button
                      type="button"
                      onClick={() => mergePersons(c.persons[0].person_id, p.person_id)}
                      disabled={busy}
                      style={btnStyle(t, "warn", "sm")}
                    >
                      Merge → first
                    </button>
                  )}
                </div>
              ))}
            </div>
          </PersonCard>
        ))}
      </Bucket>
    </div>
  );
}


function Bucket({
  title, count, empty, children, t,
}: {
  title: string;
  count: number;
  empty: string;
  children: React.ReactNode;
  t: ReturnType<typeof useTheme>;
}) {
  return (
    <section style={{ marginTop: 24 }}>
      <h2 style={{ fontSize: 16, fontWeight: 600, color: t.text, marginBottom: 8 }}>
        {title}{" "}
        <span style={{ color: t.tg, fontSize: 13, fontWeight: 400 }}>
          ({count})
        </span>
      </h2>
      {count === 0 ? (
        <p style={{ fontSize: 12, color: t.tg, fontStyle: "italic" }}>
          {empty}
        </p>
      ) : (
        <div style={{ display: "grid", gap: 8 }}>{children}</div>
      )}
    </section>
  );
}


function PersonCard({
  children, t, subdued,
}: {
  children: React.ReactNode;
  t: ReturnType<typeof useTheme>;
  subdued?: boolean;
}) {
  return (
    <div
      style={{
        padding: 12,
        background: t.bg2,
        border: `1px solid ${t.border}`,
        borderRadius: 6,
        opacity: subdued ? 0.7 : 1,
      }}
    >
      {children}
    </div>
  );
}


function btnStyle(
  t: ReturnType<typeof useTheme>,
  tone: "neutral" | "warn" | "approve",
  size: "sm" | "md" = "md",
): React.CSSProperties {
  const padding = size === "sm" ? "3px 8px" : "6px 12px";
  const fontSize = size === "sm" ? 11 : 13;
  if (tone === "warn") {
    return {
      padding,
      fontSize,
      background: t.redb || t.bg3,
      color: t.redt || t.text,
      border: `1px solid ${t.red || t.border}`,
      borderRadius: 4,
      cursor: "pointer",
    };
  }
  if (tone === "approve") {
    return {
      padding,
      fontSize,
      background: t.grnb || t.bg3,
      color: t.grnt || t.text,
      border: `1px solid ${t.grn || t.border}`,
      borderRadius: 4,
      cursor: "pointer",
      fontWeight: 600,
    };
  }
  return {
    padding,
    fontSize,
    background: t.bg3,
    color: t.text,
    border: `1px solid ${t.border}`,
    borderRadius: 4,
    cursor: "pointer",
  };
}
