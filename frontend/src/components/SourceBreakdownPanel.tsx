/**
 * v3.10.0 — per-source evidence for one author, plus the operator
 * blacklist control.
 *
 * Exists because some sources collapse several real people into one
 * author record. OpenLibrary's OL2719653A holds a sci-fi author's Fold
 * novels next to a political commentator's "Trump and Churchill" and a
 * children's author's "Kenny the Koala". Per-source validation can't
 * reject it — the record genuinely contains books you own, so the
 * any-owned-vs-any-catalogue check passes honestly — and no cross-source
 * signal resolves it either, because the ambiguity is inside the record.
 *
 * So this panel shows EVIDENCE, not a confidence score. `corroborated`
 * is weighted: kobo, google_books and ibdb use the author's NAME as
 * their external id and do no author-entity resolution, so their
 * agreement proves nothing. Every one of OpenLibrary's 14 "corroborated"
 * Nick Adams rows was corroborated by google_books alone — an unweighted
 * count would have argued FOR keeping the junk.
 *
 * **The titles are the real signal.** Two stronger-sounding rules were
 * built and then rejected by measurement (see the endpoint docstring):
 * "few books match your library" is ~always true because discovered rows
 * exclude owned books by construction, and "no corroboration = suspect"
 * flagged Terry Brooks, Jim Butcher and Holly Black, whose OpenLibrary
 * records are genuine. Nothing computable separated those from the one
 * actually-collapsed record. But "Trump and Churchill" and "Kenny the
 * Koala Comes to the USA" listed under a sci-fi author is obvious at a
 * glance — so the panel puts the titles in front of the operator and
 * leaves the verdict to them.
 */
import { useCallback, useEffect, useState } from "react";
import { useTheme } from "../theme";
import { api, slugQuery } from "../api";
import { Section } from "./Section";
import { Btn } from "./Btn";
import { toast } from "../lib/toast";

type SourceRow = {
  source: string;
  books: number;
  corroborated: number;
  sample_titles: string[];
  disambiguating: boolean;
  source_author_id: string | null;
  // "review" means "large contribution, nothing confirms it — worth your
  // eyes", NOT "wrong". Two stronger-sounding rules were tried and
  // rejected by measurement; see the endpoint docstring.
  flag: "review" | "none";
  note: string;
  blacklisted: boolean;
};

type Breakdown = {
  author_id: number;
  author_name: string;
  slug: string;
  owned_books: number;
  sources: SourceRow[];
};

export function SourceBreakdownPanel({
  authorId,
  slug,
  onChanged,
}: {
  // ⚠️ The NUMERIC per-library author id. The author-detail page's own
  // `authorId` prop may be the composite "slug:id" form
  // ("calibre-library:619") — passing that straight through made every
  // request 422, and because a failed load used to render nothing, the
  // panel silently didn't exist. Callers pass `authorIdNum`.
  authorId: number;
  slug?: string;
  onChanged?: () => void;
}) {
  const theme = useTheme();
  const [data, setData] = useState<Breakdown | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!authorId) return;
    try {
      // `api.*` auto-prefixes `/api`, so the path carries `/discovery`
      // (the router's own prefix) but NOT `/api` — doubling it yields a
      // misleading 405 from the SPA static fallback.
      const d = await api.get<Breakdown>(
        `/discovery/authors/${authorId}/source-breakdown${slugQuery(slug)}`,
      );
      setData(d);
      setErr(null);
    } catch (e) {
      // Deliberately NOT silent. Swallowing this is what hid the 422
      // above: the panel simply wasn't on the page, with nothing in the
      // console and nothing on screen to explain why.
      console.error("source-breakdown failed", e);
      setData(null);
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, [authorId, slug]);

  useEffect(() => {
    load();
  }, [load]);

  if (err) {
    return (
      <Section title="Source records" defaultOpen={false} count="unavailable">
        <div style={{ color: theme.err, fontSize: 13 }}>
          Couldn't load source breakdown: {err}
        </div>
      </Section>
    );
  }
  if (!data || data.sources.length === 0) return null;

  const tone = (f: SourceRow["flag"]) =>
    f === "review" ? theme.ylw : theme.grn;

  const blacklist = async (r: SourceRow) => {
    if (!r.source_author_id) return;
    const ok = window.confirm(
      `Blacklist ${r.source} record ${r.source_author_id}?\n\n` +
        `Future scans will skip this record entirely, and the ` +
        `${r.books} unowned book(s) it contributed to ${data.author_name} ` +
        `will be retracted now.\n\n` +
        `Books you own are never touched, and co-authored books are ` +
        `unlinked rather than deleted. You can undo this here; the next ` +
        `scan re-imports whatever was retracted.`,
    );
    if (!ok) return;
    setBusy(r.source);
    try {
      const res = await api.post<{ books_retracted: number }>(
        "/discovery/source-blacklist",
        {
          source: r.source,
          source_author_id: r.source_author_id,
          author_id: authorId,
          author_name: data.author_name,
          slug: slug || data.slug,
          reason: "operator: collapsed source record",
        },
      );
      toast.success(
        `Blacklisted ${r.source} ${r.source_author_id} — retracted ` +
          `${res.books_retracted} book(s)`,
      );
      await load();
      onChanged?.();
    } catch {
      toast.error(`Could not blacklist ${r.source}`);
    } finally {
      setBusy(null);
    }
  };

  const unblacklist = async (r: SourceRow) => {
    if (!r.source_author_id) return;
    setBusy(r.source);
    try {
      // The list endpoint is the only place the entry id lives; the
      // breakdown row carries the (source, source_author_id) pair.
      const { entries } = await api.get<{
        entries: { id: number; source: string; source_author_id: string }[];
      }>("/discovery/source-blacklist");
      const hit = entries.find(
        (e) =>
          e.source === r.source && e.source_author_id === r.source_author_id,
      );
      if (!hit) {
        toast.error("Blacklist entry not found");
        return;
      }
      await api.del(`/discovery/source-blacklist/${hit.id}`);
      toast.success(
        `${r.source} ${r.source_author_id} un-blacklisted — the next scan ` +
          `will re-import its books`,
      );
      await load();
      onChanged?.();
    } catch {
      toast.error(`Could not un-blacklist ${r.source}`);
    } finally {
      setBusy(null);
    }
  };

  const flagged = data.sources.filter((s) => s.flag === "review").length;
  // Open when something is flagged. This is the surface for acting on a
  // bad source record, so on the ~13% of authors that have one it should
  // be visible without a click; everywhere else it stays folded away.
  const openByDefault = flagged > 0;

  return (
    <Section
      title="Source records"
      subtitle={
        "What each source contributed for this author, and whether " +
        "anything else confirms it."
      }
      count={
        flagged > 0 ? (
          <span style={{ color: theme.ylw }}>{flagged} to review</span>
        ) : (
          `${data.sources.length} sources`
        )
      }
      defaultOpen={openByDefault}
    >
      <div style={{ display: "grid", gap: 10 }}>
        {data.sources.map((r) => (
          <div
            key={r.source}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 14,
              flexWrap: "wrap",
              padding: "10px 12px",
              borderRadius: 8,
              background: theme.bg3,
              border: `1px solid ${
                r.flag === "review" ? theme.ylw : theme.borderL
              }`,
              opacity: r.blacklisted ? 0.55 : 1,
            }}
          >
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: 8,
                background: tone(r.flag),
                flexShrink: 0,
              }}
            />
            <div style={{ minWidth: 140 }}>
              <div style={{ fontWeight: 600, color: theme.text }}>
                {r.source}
                {r.blacklisted ? (
                  <span style={{ color: theme.red, fontWeight: 400 }}>
                    {" "}
                    · blacklisted
                  </span>
                ) : null}
              </div>
              {r.source_author_id ? (
                <div
                  style={{
                    fontSize: 12,
                    color: theme.text2,
                    fontFamily: "monospace",
                  }}
                >
                  {r.source_author_id}
                </div>
              ) : null}
            </div>

            <div style={{ display: "flex", gap: 18, flexWrap: "wrap" }}>
              <Stat label="books" value={r.books} theme={theme} />
              <Stat
                label="confirmed elsewhere"
                value={r.corroborated}
                theme={theme}
                dim={r.corroborated === 0}
              />
            </div>

            <div
              style={{
                flex: 1,
                minWidth: 220,
                fontSize: 12,
                color: theme.text2,
              }}
            >
              {r.note}
              {!r.disambiguating ? (
                <div style={{ color: theme.text2, marginTop: 2 }}>
                  This source identifies authors by name only, so its
                  agreement doesn't confirm identity.
                </div>
              ) : null}
              {r.sample_titles.length > 0 ? (
                <div style={{ marginTop: 6, lineHeight: 1.5 }}>
                  <span style={{ color: theme.text2 }}>What it added: </span>
                  <span style={{ color: theme.text }}>
                    {r.sample_titles.join(" · ")}
                    {r.books > r.sample_titles.length
                      ? ` … +${r.books - r.sample_titles.length} more`
                      : ""}
                  </span>
                </div>
              ) : null}
            </div>

            {r.source_author_id && r.blacklisted ? (
              <Btn
                variant="ghost"
                size="sm"
                disabled={busy === r.source}
                onClick={() => unblacklist(r)}
              >
                {busy === r.source ? "Working…" : "Un-blacklist"}
              </Btn>
            ) : null}

            {r.source_author_id && !r.blacklisted ? (
              <Btn
                variant={r.flag === "review" ? "danger" : "ghost"}
                size="sm"
                disabled={busy === r.source}
                onClick={() => blacklist(r)}
              >
                {busy === r.source ? "Working…" : "Blacklist record"}
              </Btn>
            ) : null}
          </div>
        ))}
      </div>
    </Section>
  );
}

function Stat({
  label,
  value,
  theme,
  dim,
}: {
  label: string;
  value: number;
  theme: ReturnType<typeof useTheme>;
  dim?: boolean;
}) {
  return (
    <div style={{ textAlign: "center", minWidth: 56 }}>
      <div
        style={{
          fontSize: 18,
          fontWeight: 700,
          color: dim ? theme.text2 : theme.text,
        }}
      >
        {value}
      </div>
      <div style={{ fontSize: 11, color: theme.text2 }}>{label}</div>
    </div>
  );
}
