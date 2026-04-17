// IgnoredWeeklyPage — review ignored-author torrents from the past week.
//
// Groups ignored torrents by author, showing each author's rejected
// books with covers in an expandable dropdown. The user can promote
// an author to the allowed list directly from this view.
import { useEffect, useState } from "react";
import { Btn } from "../components/Btn";
import { Section } from "../components/Section";
import { Spin } from "../components/Spin";
import { api } from "../api";
import { useTheme } from "../theme";

interface TorrentEntry {
  torrent_name: string;
  mam_torrent_id: string;
  cover_path: string | null;
}

interface AuthorGroup {
  author_blob: string;
  count: number;
  torrents: TorrentEntry[];
}

export default function IgnoredWeeklyPage() {
  const theme = useTheme();
  const [groups, setGroups] = useState<AuthorGroup[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function load() {
    try {
      const r = await api.get<{ groups: AuthorGroup[] }>("/v1/tentative/ignored-weekly");
      setGroups(r.groups);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => { load(); }, []);

  async function promoteToAllowed(authorBlob: string) {
    setBusy(true);
    try {
      await api.post("/v1/authors/allowed", { names: [authorBlob] });
      // Also remove from ignored list
      const norm = authorBlob.toLowerCase().replace(/[^a-z0-9 ']/g, " ").replace(/\s+/g, " ").trim();
      await api.del(`/v1/authors/ignored/${encodeURIComponent(norm)}`).catch(() => null);
      setMessage(`Promoted "${authorBlob}" to allowed list.`);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h1 style={{ fontSize: 24, fontWeight: 700, color: theme.text, marginBottom: 4 }}>
        Ignored weekly review
      </h1>
      <p style={{ fontSize: 14, color: theme.textDim, marginBottom: 20 }}>
        Authors on your ignored list whose books appeared this week.
        Click an author to see their rejected books. Promote to move
        them to the allowed list.
      </p>

      {error && (
        <div style={{ background: theme.err + "22", border: `1px solid ${theme.err}55`, color: theme.err, padding: "10px 14px", borderRadius: 8, fontSize: 13, marginBottom: 16 }}>
          {error}
        </div>
      )}
      {message && (
        <div style={{ background: theme.ok + "22", border: `1px solid ${theme.ok}55`, color: theme.ok, padding: "10px 14px", borderRadius: 8, fontSize: 13, marginBottom: 16 }}>
          {message}
        </div>
      )}

      {groups === null ? (
        <div style={{ display: "flex", justifyContent: "center", padding: 40 }}><Spin /></div>
      ) : groups.length === 0 ? (
        <Section title="No ignored activity this week" subtitle="No announces from ignored authors in the last 7 days.">
          <p style={{ fontSize: 13, color: theme.textDim }}>Check back next week, or browse the Authors &rarr; Ignored tab to review the full list.</p>
        </Section>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {groups.map((g) => {
            const expanded = !collapsed.has(g.author_blob);
            return (
              <div
                key={g.author_blob}
                style={{
                  background: theme.bg2,
                  border: `1px solid ${theme.borderL}`,
                  borderRadius: 10,
                  overflow: "hidden",
                }}
              >
                <div
                  onClick={() => setCollapsed(prev => { const next = new Set(prev); if (expanded) next.add(g.author_blob); else next.delete(g.author_blob); return next; })}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    padding: "12px 16px",
                    cursor: "pointer",
                  }}
                >
                  <div>
                    <span style={{ fontSize: 15, fontWeight: 600, color: theme.text }}>{g.author_blob}</span>
                    <span style={{ marginLeft: 10, fontSize: 12, color: theme.textDim }}>
                      {g.count} torrent{g.count !== 1 ? "s" : ""}
                    </span>
                  </div>
                  <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <Btn
                      variant="primary"
                      disabled={busy}
                      onClick={(e) => { e.stopPropagation(); promoteToAllowed(g.author_blob); }}
                    >
                      Promote to allowed
                    </Btn>
                    <span style={{ color: theme.textDim, fontSize: 16 }}>
                      {expanded ? "▾" : "▸"}
                    </span>
                  </div>
                </div>

                {expanded && (
                  <div style={{ borderTop: `1px solid ${theme.borderL}`, padding: "12px 16px" }}>
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: 10 }}>
                      {g.torrents.map((t) => (
                        <div
                          key={t.mam_torrent_id}
                          style={{
                            display: "flex",
                            gap: 10,
                            padding: 8,
                            background: theme.bg3,
                            borderRadius: 8,
                          }}
                        >
                          {t.cover_path && (
                            <img
                              src={`/api/v1/covers/${encodeURIComponent(t.cover_path)}`}
                              alt=""
                              style={{ width: 50, height: 75, objectFit: "cover", borderRadius: 4, flexShrink: 0 }}
                              onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
                            />
                          )}
                          <div style={{ minWidth: 0 }}>
                            <div style={{ fontSize: 13, color: theme.text, fontWeight: 600, wordBreak: "break-word" }}>
                              {t.torrent_name}
                            </div>
                            <a
                              href={`https://www.myanonamouse.net/t/${t.mam_torrent_id}`}
                              target="_blank"
                              rel="noreferrer"
                              style={{ fontSize: 11, color: theme.accent, textDecoration: "none" }}
                            >
                              MAM #{t.mam_torrent_id}
                            </a>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
