// Reingest from disk (v2.8.0), in the book sidebar: for a book MAM
// reports as already snatched, probe qBit + the configured download
// folder for the existing files and either auto-start the pipeline (one
// candidate) or let the user pick among several. It never re-snatches
// (the snatch-safety rule): nothing found is an error, not a fallback.
//
// The sidebar calls the hook and places the button (in the MAM row) and
// the outcome (the error / picker under it) (audit G158).
import { useState } from "react";
import { api, slugQuery } from "../../api";
import { useTheme } from "../../theme";
import { toast } from "../../lib/toast";
import type { Book } from "../../types";
import { Btn } from "../Btn";
import { Spin } from "../Spin";

// Mirror the FastAPI ProbeResponse / StartResponse shapes in
// app/discovery/routers/reingest.py.
interface ReingestCandidate {
  source: "qbit" | "fs";
  display_path: string;
  save_path: string;
  book_files: string[];
  qbit_hash: string | null;
  mtime: number;
  total_size: number;
}

interface ReingestProbeResponse {
  found: boolean;
  candidates: ReingestCandidate[];
  auto_started: boolean;
  grab_id: number | null;
  pipeline_run_id: number | null;
  // v2.8.1: when auto-start fired but the pipeline failed
  // mid-flight (qBit reported a file that wasn't on disk, sink
  // unreachable, etc.) the server returns auto_started=false +
  // error set. The UI shows the error instead of a success toast.
  error?: string | null;
  searched: string[];
  mam_torrent_name: string | null;
}

export function useReingest(book: Book, onEdit?: () => void | Promise<void>) {
  // `candidates` holds the picker payload when the probe returned more
  // than one result; `error` shows the not-found / failure message
  // inline near the button.
  const [busy, setBusy] = useState(false);
  const [candidates, setCandidates] = useState<ReingestCandidate[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const probe = async () => {
    if (busy) return;
    setBusy(true);
    setCandidates(null);
    setError(null);
    try {
      const r = await api.post<ReingestProbeResponse>(
        `/discovery/books/${book.id}/reingest/probe${slugQuery(book.library_slug)}`,
      );
      if (!r.found) {
        // Per the v2.8.0 design (option a): hard-fail with a clear
        // message when the file isn't on disk anywhere. NO automatic
        // fallback to re-snatch — the snatch-safety rule forbids it.
        setError(
          `Could not find this snatch anywhere we looked: ${(r.searched || []).join(", ") || "no sources searched"}.`,
        );
        return;
      }
      // v2.8.1: auto-start that ran but failed mid-pipeline returns
      // auto_started=false + error set. Surface that instead of a
      // misleading success toast.
      if (r.error) {
        setError(r.error);
        return;
      }
      if (r.auto_started) {
        toast.success(
          `Reingest started: grab #${r.grab_id}, run #${r.pipeline_run_id}. Check the Review queue.`,
        );
        onEdit?.();
        return;
      }
      // Multi-candidate → show picker.
      setCandidates(r.candidates || []);
    } catch (e) {
      setError(`Reingest probe failed: ${(e as Error).message || e}`);
    } finally {
      setBusy(false);
    }
  };

  const start = async (candidate: ReingestCandidate) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.post<{
        ok: boolean;
        grab_id: number;
        pipeline_run_id: number;
        error?: string | null;
      }>(
        `/discovery/books/${book.id}/reingest/start${slugQuery(book.library_slug)}`,
        { candidate },
      );
      // v2.8.1: surface mid-pipeline failures (qBit file moved,
      // sink unreachable, etc.) instead of a misleading success
      // toast. The grab/run rows still exist as audit trail.
      if (!r.ok) {
        setError(r.error || `Reingest pipeline_run #${r.pipeline_run_id} failed.`);
        return;
      }
      toast.success(
        `Reingest started: grab #${r.grab_id}, run #${r.pipeline_run_id}. Check the Review queue.`,
      );
      setCandidates(null);
      onEdit?.();
    } catch (e) {
      setError(`Reingest start failed: ${(e as Error).message || e}`);
    } finally {
      setBusy(false);
    }
  };

  return { busy, candidates, error, probe, start, closePicker: () => setCandidates(null) };
}

export type ReingestState = ReturnType<typeof useReingest>;

export function ReingestButton({ r }: { r: ReingestState }) {
  const t = useTheme();
  return (
    <Btn
      size="sm"
      onClick={r.probe}
      disabled={r.busy}
      title="Already on disk from a prior snatch — find the files and run them through enrichment + review without re-downloading from MAM."
      style={{
        background: t.ok + "22",
        color: t.ok,
        border: `1px solid ${t.ok}44`,
      }}
    >
      {r.busy ? <Spin /> : "♻"} Reingest from disk
    </Btn>
  );
}

// The error line (with the places searched, so a missing drive or wrong
// download_path shows) and the candidate picker (path + file count +
// size; a click starts the reingest with that candidate).
export function ReingestOutcome({ r }: { r: ReingestState }) {
  const t = useTheme();
  const { busy: reingestBusy, candidates: reingestCandidates, error: reingestError } = r;
  return (
    <>
      {reingestError ? (
        <div
          style={{
            marginTop: 6,
            padding: "6px 10px",
            borderRadius: 6,
            background: t.err + "15",
            border: `1px solid ${t.err}55`,
            color: t.err,
            fontSize: 12,
          }}
        >
          {reingestError}
        </div>
      ) : null}
      {reingestCandidates && reingestCandidates.length > 0 ? (
        <div
          style={{
            marginTop: 8,
            padding: 10,
            borderRadius: 8,
            background: t.bg3,
            border: `1px solid ${t.borderL}`,
          }}
        >
          <div
            style={{
              fontSize: 12,
              fontWeight: 700,
              color: t.text2,
              marginBottom: 8,
            }}
          >
            Multiple matches found — pick one:
          </div>
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 6,
            }}
          >
            {reingestCandidates.map((c, i) => (
              <button
                key={`${c.source}:${c.save_path}:${i}`}
                disabled={reingestBusy}
                onClick={() => r.start(c)}
                style={{
                  textAlign: "left",
                  padding: "6px 10px",
                  borderRadius: 6,
                  background: t.bg2,
                  border: `1px solid ${t.borderL}`,
                  color: t.text,
                  cursor: reingestBusy ? "wait" : "pointer",
                  fontSize: 12,
                }}
              >
                <div style={{ fontWeight: 600 }}>
                  [{c.source}] {c.display_path}
                </div>
                <div style={{ color: t.textDim, fontSize: 11 }}>
                  {c.book_files.length} file
                  {c.book_files.length === 1 ? "" : "s"}
                  {c.total_size > 0
                    ? ` · ${(c.total_size / 1024 / 1024).toFixed(1)} MB`
                    : ""}
                </div>
              </button>
            ))}
            <button
              disabled={reingestBusy}
              onClick={r.closePicker}
              style={{
                marginTop: 4,
                padding: "4px 10px",
                borderRadius: 6,
                background: "transparent",
                border: "none",
                color: t.textDim,
                cursor: "pointer",
                fontSize: 11,
                textAlign: "left",
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : null}
    </>
  );
}
