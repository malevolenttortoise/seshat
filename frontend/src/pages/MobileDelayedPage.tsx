// Mobile-native delayed-torrents page. Card-per-file with re-inject
// and delete actions. Trivial port of the desktop table.
import { useEffect, useState } from "react";
import { api } from "../api";
import { useTheme } from "../theme";
import { fmtBytes } from "../lib/format";
import { MobileBtn, MobileBackButton } from "../components/mobile";

interface DelayedItem {
  filename: string;
  grab_id: number;
  mam_torrent_id: string;
  size_bytes: number;
}

interface DelayedListResponse {
  path: string;
  items: DelayedItem[];
}

export default function MobileDelayedPage() {
  const t = useTheme();
  const [data, setData] = useState<DelayedListResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyFile, setBusyFile] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = async () => {
    try {
      const r = await api.get<DelayedListResponse>("/v1/delayed");
      setData(r);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  };

  useEffect(() => {
    load();
  }, []);

  const reinject = async (filename: string) => {
    setBusyFile(filename);
    setMessage(null);
    try {
      const r = await api.post<{ ok: boolean; error?: string }>(
        `/v1/delayed/${encodeURIComponent(filename)}/reinject`,
      );
      setMessage(
        r.ok ? `Re-injected ${filename}` : `Failed: ${r.error || "unknown"}`,
      );
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyFile(null);
    }
  };

  const remove = async (filename: string) => {
    if (!confirm(`Delete ${filename}?`)) return;
    setBusyFile(filename);
    setMessage(null);
    try {
      await api.del(`/v1/delayed/${encodeURIComponent(filename)}`);
      setMessage(`Deleted ${filename}`);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyFile(null);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <MobileBackButton to="dashboard" label="Dashboard" />

      <div>
        <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: t.text }}>
          Delayed Torrents
        </h1>
        <p style={{ fontSize: 13, color: t.td, margin: "4px 0 0" }}>
          .torrent files rotated out when the queue was full.
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

      {data === null ? (
        <div style={{ padding: 24, textAlign: "center", color: t.tg }}>
          Loading…
        </div>
      ) : data.items.length === 0 ? (
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
          No delayed .torrent files on disk.
        </div>
      ) : (
        <>
          <div style={{ fontSize: 12, color: t.tg }}>
            {data.items.length} file(s) · {data.path}
          </div>
          {data.items.map((item) => (
            <div
              key={item.filename}
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 8,
                padding: 12,
                background: t.bg2,
                border: `1px solid ${t.border}`,
                borderRadius: 12,
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "baseline",
                  justifyContent: "space-between",
                  gap: 8,
                }}
              >
                <div style={{ fontSize: 14, fontWeight: 700, color: t.text }}>
                  Grab #{item.grab_id}
                </div>
                <span style={{ fontSize: 12, color: t.tg }}>
                  {fmtBytes(item.size_bytes)}
                </span>
              </div>
              <a
                href={`https://www.myanonamouse.net/t/${item.mam_torrent_id}`}
                target="_blank"
                rel="noopener noreferrer"
                style={{
                  fontSize: 13,
                  color: t.accent,
                  textDecoration: "none",
                }}
              >
                MAM #{item.mam_torrent_id} ↗
              </a>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                <MobileBtn
                  variant="primary"
                  primary
                  fullWidth
                  onClick={() => reinject(item.filename)}
                  disabled={busyFile === item.filename}
                >
                  {busyFile === item.filename ? "…" : "Re-inject"}
                </MobileBtn>
                <MobileBtn
                  variant="danger"
                  fullWidth
                  onClick={() => remove(item.filename)}
                  disabled={busyFile === item.filename}
                >
                  Delete
                </MobileBtn>
              </div>
            </div>
          ))}
        </>
      )}
    </div>
  );
}
