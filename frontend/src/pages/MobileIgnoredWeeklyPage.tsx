// Mobile-native ignored-weekly review. Each author is a collapsible
// section with their rejected books inside as a compact grid;
// "Promote to allowed" sits in the section header.
import { useEffect, useState } from "react";
import { api } from "../api";
import { useTheme } from "../theme";
import {
  MobileBtn,
  MobileSection,
  MobileBackButton,
} from "../components/mobile";

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

export default function MobileIgnoredWeeklyPage() {
  const t = useTheme();
  const [groups, setGroups] = useState<AuthorGroup[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const load = async () => {
    try {
      const r = await api.get<{ groups: AuthorGroup[] }>(
        "/v1/tentative/ignored-weekly",
      );
      setGroups(r.groups);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  };

  useEffect(() => {
    load();
  }, []);

  const promote = async (authorBlob: string) => {
    setBusy(true);
    try {
      await api.post("/v1/authors/allowed", { names: [authorBlob] });
      const norm = authorBlob
        .toLowerCase()
        .replace(/[^a-z0-9 ']/g, " ")
        .replace(/\s+/g, " ")
        .trim();
      await api
        .del(`/v1/authors/ignored/${encodeURIComponent(norm)}`)
        .catch(() => null);
      setMessage(`Promoted "${authorBlob}" to allowed.`);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <MobileBackButton to="dashboard" label="Dashboard" />

      <div>
        <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: t.text }}>
          Weekly Ignored
        </h1>
        <p style={{ fontSize: 13, color: t.td, margin: "4px 0 0" }}>
          Ignored authors whose books appeared this week. Promote to
          move them to the allowed list.
        </p>
      </div>

      {error && (
        <div
          style={{
            padding: "10px 14px",
            background: t.redb,
            border: `1px solid ${t.redt}`,
            color: t.red,
            borderRadius: 10,
            fontSize: 13,
          }}
        >
          {error}
        </div>
      )}
      {message && (
        <div
          style={{
            padding: "10px 14px",
            background: t.grnb,
            border: `1px solid ${t.grnt}`,
            color: t.grn,
            borderRadius: 10,
            fontSize: 13,
          }}
        >
          {message}
        </div>
      )}

      {groups === null ? (
        <div style={{ padding: 24, textAlign: "center", color: t.tg }}>
          Loading…
        </div>
      ) : groups.length === 0 ? (
        <div
          style={{
            padding: 24,
            textAlign: "center",
            color: t.tg,
            fontSize: 13,
            background: t.bg2,
            border: `1px solid ${t.borderL}`,
            borderRadius: 12,
          }}
        >
          No ignored-author torrents this week.
        </div>
      ) : (
        groups.map((g) => (
          <MobileSection
            key={g.author_blob}
            title={g.author_blob}
            count={g.count}
            defaultOpen={false}
            right={
              <MobileBtn
                variant="primary"
                onClick={() => promote(g.author_blob)}
                disabled={busy}
                style={{ minHeight: 36, fontSize: 13 }}
              >
                Promote
              </MobileBtn>
            }
          >
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fill, minmax(120px, 1fr))",
                gap: 8,
              }}
            >
              {g.torrents.map((tor) => (
                <a
                  key={tor.mam_torrent_id}
                  href={`https://www.myanonamouse.net/t/${tor.mam_torrent_id}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: 4,
                    background: t.bg3,
                    border: `1px solid ${t.borderL}`,
                    borderRadius: 8,
                    padding: 8,
                    textDecoration: "none",
                  }}
                >
                  <div
                    style={{
                      aspectRatio: "2/3",
                      background: t.bg4,
                      borderRadius: 4,
                      overflow: "hidden",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                    }}
                  >
                    {tor.cover_path ? (
                      <img
                        src={`/api/v1/grabs/cover/${tor.mam_torrent_id}`}
                        alt=""
                        loading="lazy"
                        style={{ width: "100%", height: "100%", objectFit: "cover" }}
                      />
                    ) : (
                      <span style={{ color: t.tg, fontSize: 20 }}>?</span>
                    )}
                  </div>
                  <div
                    style={{
                      fontSize: 11,
                      color: t.text2,
                      overflow: "hidden",
                      display: "-webkit-box",
                      WebkitLineClamp: 2,
                      WebkitBoxOrient: "vertical",
                    }}
                  >
                    {tor.torrent_name}
                  </div>
                </a>
              ))}
            </div>
          </MobileSection>
        ))
      )}
    </div>
  );
}
