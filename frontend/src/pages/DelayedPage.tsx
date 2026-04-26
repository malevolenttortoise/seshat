// DelayedPage — manage .torrent files that were rotated out of the
// queue when it was full (FIFO eviction).
//
// The backend scans the delayed folder on disk (no DB tracking) and
// returns filenames parsed into grab_id + mam_torrent_id. The user
// can push a file back to the queue (re-inject via the dispatcher)
// or delete it permanently.
import { useEffect, useState } from "react";
import { Btn } from "../components/Btn";
import { Section } from "../components/Section";
import { Spin } from "../components/Spin";
import { api } from "../api";
import { useTheme } from "../theme";
import { useViewport } from "../hooks/useViewport";
import { useMobileCodepath } from "../components/mobile";
import MobileDelayedPage from "./MobileDelayedPage";

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

export default function DelayedPage() {
  const vp = useViewport();
  if (useMobileCodepath(vp)) return <MobileDelayedPage />;
  return <DesktopDelayedPage />;
}

function DesktopDelayedPage() {
  const theme = useTheme();
  const [data, setData] = useState<DelayedListResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyFile, setBusyFile] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function load() {
    try {
      const r = await api.get<DelayedListResponse>("/v1/delayed");
      setData(r);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function reinject(filename: string) {
    setBusyFile(filename);
    setMessage(null);
    try {
      const r = await api.post<{ ok: boolean; error?: string }>(
        `/v1/delayed/${encodeURIComponent(filename)}/reinject`,
      );
      setMessage(
        r.ok
          ? `Re-injected ${filename} — it's back in the pipeline.`
          : `Re-inject failed: ${r.error || "unknown error"}`,
      );
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyFile(null);
    }
  }

  async function remove(filename: string) {
    setBusyFile(filename);
    setMessage(null);
    try {
      await api.del(`/v1/delayed/${encodeURIComponent(filename)}`);
      setMessage(`Deleted ${filename}.`);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyFile(null);
    }
  }

  return (
    <div>
      <h1
        style={{
          fontSize: 24,
          fontWeight: 700,
          color: theme.text,
          marginBottom: 4,
        }}
      >
        Delayed Torrents
      </h1>
      <p style={{ fontSize: 14, color: theme.textDim, marginBottom: 20 }}>
        When the snatch queue was full, the oldest queued grab's .torrent
        was dumped here via FIFO rotation. Re-inject to push it back
        through the pipeline, or delete if you no longer want it.
      </p>

      {error && (
        <div
          style={{
            background: theme.err + "22",
            border: `1px solid ${theme.err}55`,
            color: theme.err,
            padding: "10px 14px",
            borderRadius: 8,
            fontSize: 13,
            marginBottom: 16,
          }}
        >
          {error}
        </div>
      )}
      {message && (
        <div
          style={{
            background: theme.ok + "22",
            border: `1px solid ${theme.ok}55`,
            color: theme.ok,
            padding: "10px 14px",
            borderRadius: 8,
            fontSize: 13,
            marginBottom: 16,
          }}
        >
          {message}
        </div>
      )}

      {data === null ? (
        <div style={{ display: "flex", justifyContent: "center", padding: 40 }}>
          <Spin />
        </div>
      ) : data.items.length === 0 ? (
        <Section
          title="Empty"
          subtitle="No delayed .torrent files on disk."
        >
          <p style={{ fontSize: 13, color: theme.textDim }}>
            Files appear here when the snatch queue is full and a new grab
            triggers FIFO rotation. The folder is at{" "}
            <code style={{ fontSize: 12, color: theme.text2 }}>
              {data.path || "(not configured)"}
            </code>
            .
          </p>
        </Section>
      ) : (
        <Section
          title={`${data.items.length} file(s)`}
          subtitle={`Folder: ${data.path}`}
          right={
            <Btn variant="ghost" onClick={load}>
              Refresh
            </Btn>
          }
        >
          <table
            style={{
              width: "100%",
              borderCollapse: "collapse",
              fontSize: 13,
            }}
          >
            <thead>
              <tr
                style={{
                  textAlign: "left",
                  color: theme.textDim,
                  fontWeight: 600,
                  fontSize: 11,
                  textTransform: "uppercase",
                  letterSpacing: 0.4,
                }}
              >
                <th style={{ padding: "8px 6px" }}>Grab</th>
                <th style={{ padding: "8px 6px" }}>MAM ID</th>
                <th style={{ padding: "8px 6px" }}>Size</th>
                <th style={{ padding: "8px 6px", textAlign: "right" }}>
                  Actions
                </th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((item) => (
                <tr
                  key={item.filename}
                  style={{ borderTop: `1px solid ${theme.borderL}` }}
                >
                  <td style={{ padding: "8px 6px", color: theme.text2 }}>
                    #{item.grab_id}
                  </td>
                  <td style={{ padding: "8px 6px" }}>
                    <a
                      href={`https://www.myanonamouse.net/t/${item.mam_torrent_id}`}
                      target="_blank"
                      rel="noreferrer"
                      style={{
                        color: theme.accent,
                        textDecoration: "none",
                      }}
                    >
                      {item.mam_torrent_id}
                    </a>
                  </td>
                  <td style={{ padding: "8px 6px", color: theme.textDim }}>
                    {(item.size_bytes / 1024).toFixed(1)} KB
                  </td>
                  <td
                    style={{
                      padding: "8px 6px",
                      textAlign: "right",
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        gap: 6,
                        justifyContent: "flex-end",
                      }}
                    >
                      <Btn
                        variant="primary"
                        disabled={busyFile === item.filename}
                        onClick={() => reinject(item.filename)}
                      >
                        {busyFile === item.filename ? (
                          <Spin size={14} />
                        ) : (
                          "Re-inject"
                        )}
                      </Btn>
                      <Btn
                        variant="danger"
                        disabled={busyFile === item.filename}
                        onClick={() => remove(item.filename)}
                      >
                        Delete
                      </Btn>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      )}
    </div>
  );
}
