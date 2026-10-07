// Mobile twin of GrabFromMamPage: same review list, mobile chrome. No
// drag-and-drop on phones, so uploads go through the file picker.
import { useState } from "react";
import { useTheme } from "../theme";
import { MobileBackButton, MobileBtn } from "../components/mobile";
import { GrabPreviewRow } from "../components/manualGrab/GrabPreviewRow";
import { useManualGrabBatch } from "../components/manualGrab/useManualGrabBatch";
import { SNATCH_LAG_HINT, grabLabel, linesOf } from "../components/manualGrab/text";
import { TorrentDropZone } from "../components/manualGrab/TorrentDropZone";
import { MAX_BATCH, wedgeEligible } from "../components/manualGrab/types";
import { useCarriedLinks } from "../components/manualGrab/useCarriedLinks";

export default function MobileGrabFromMamPage({ initial }: { initial?: string | number | null }) {
  const t = useTheme();
  const batch = useManualGrabBatch();
  const [text, setText] = useState("");
  useCarriedLinks(batch, initial);

  const add = () => {
    const lines = linesOf(text);
    if (!lines.length) return;
    batch.addLinks(lines);
    setText("");
  };

  const full = batch.entries.length >= MAX_BATCH;
  const blocked =
    !batch.tickedCount || batch.pending || batch.grabbing || batch.wedgeShort ||
    (batch.useWedges && batch.wedgeCount > 0 && !batch.wedges);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <MobileBackButton to="dashboard" label="Dashboard" />
      <div>
        <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: t.text }}>Grab from MAM</h1>
        <p style={{ fontSize: 13, color: t.td, margin: "4px 0 0" }}>
          Paste MAM links or IDs (one per line), or add .torrent files you downloaded.
          Up to {MAX_BATCH} at a time.
        </p>
      </div>

      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="MAM links or torrent IDs, one per line"
        rows={3}
        disabled={full}
        style={{
          width: "100%", padding: 10, fontSize: 16, borderRadius: 10,
          fontFamily: "ui-monospace, monospace", resize: "vertical",
          background: t.inp, color: t.text, border: `1px solid ${t.border}`,
        }}
      />
      <MobileBtn primary fullWidth onClick={add} disabled={!text.trim() || full}>
        Look up
      </MobileBtn>
      <p style={{ fontSize: 12, color: t.td, margin: 0 }}>{SNATCH_LAG_HINT}</p>
      <TorrentDropZone onFiles={(f) => void batch.addFiles(f)} multiple compact disabled={full} />

      {batch.notice && <div style={{ color: t.warn, fontSize: 13 }}>{batch.notice}</div>}
      {batch.error && <div style={{ color: t.err, fontSize: 13 }}>{batch.error}</div>}

      {batch.entries.length > 0 && (
        <div>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 13, color: t.td }}>
            <span>{batch.entries.length} of {MAX_BATCH}{batch.pending && " · looking up…"}</span>
            <button
              onClick={batch.clear}
              disabled={batch.grabbing}
              style={{ background: "transparent", border: "none", color: t.accent, fontSize: 14, padding: 8 }}
            >
              Clear
            </button>
          </div>
          {batch.entries.map((e) => (
            <GrabPreviewRow
              key={e.key}
              entry={e}
              compact
              willWedge={batch.useWedges && wedgeEligible(e)}
              onTick={(on) => batch.setTicked(e.key, on)}
              onConfirmSnatched={() => batch.confirmSnatched(e.key)}
              onCancelConfirm={() => batch.cancelConfirm(e.key)}
              onRemove={batch.grabbing ? undefined : () => batch.remove(e.key)}
              onRetry={() => batch.retry(e.key)}
            />
          ))}

          {(batch.useWedges || batch.eligibleForWedges > 0) && (
            <label style={{ fontSize: 14, color: t.text2, display: "flex", alignItems: "center", gap: 10, padding: "10px 0" }}>
              <input
                type="checkbox"
                checked={batch.useWedges}
                disabled={batch.grabbing}
                onChange={(e) => void batch.setUseWedges(e.target.checked)}
                style={{ width: 20, height: 20, accentColor: t.accent }}
              />
              <span>
                Use wedges on rows that aren't free
                {batch.useWedges && batch.wedges && (
                  <span style={{ color: t.td }}> ({batch.wedges.spendable} spendable)</span>
                )}
              </span>
            </label>
          )}
          {batch.wedgeError && <div style={{ fontSize: 12, color: t.err }}>{batch.wedgeError}</div>}
          {batch.wedgeShort && batch.wedges && (
            <div style={{ fontSize: 12, color: t.err, paddingBottom: 8 }}>
              Needs {batch.wedgeCount} wedges, {batch.wedges.spendable} spendable
              ({batch.wedges.wedges} − {batch.wedges.reserved} reserved). Untick rows or turn wedges off.
            </div>
          )}
          {batch.willQueue > 0 && (
            <div style={{ fontSize: 12, color: t.td, paddingBottom: 8 }}>
              {batch.willQueue} will queue: your snatch budget is full
            </div>
          )}
          <MobileBtn primary fullWidth onClick={batch.grabAll} disabled={blocked}>
            {batch.grabbing ? "Grabbing…" : grabLabel(batch)}
          </MobileBtn>
        </div>
      )}
    </div>
  );
}
