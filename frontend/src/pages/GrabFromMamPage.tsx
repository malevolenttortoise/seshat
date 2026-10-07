// Grab from MAM — paste MAM links / torrent IDs or drop .torrent files
// (up to 30 at once), see what each one is, grab the ones you tick.
//
// Each row's preview comes from /api/v1/manual-grab/preview (what MAM
// says + what you already own); Grab sends the ticked rows to a
// server-side job and the rows update as it runs. A torrent Seshat
// already grabbed can't be grabbed again; one you own or already
// snatched on MAM starts unticked and is yours to decide (ADR-0023).
import { useState } from "react";
import { useTheme } from "../theme";
import { useNavigation } from "../providers/NavigationProvider";
import { Btn } from "../components/Btn";
import { Spin } from "../components/Spin";
import { useViewport } from "../hooks/useViewport";
import { useMobileCodepath } from "../components/mobile";
import { GrabPreviewRow } from "../components/manualGrab/GrabPreviewRow";
import { useManualGrabBatch, type ManualGrabBatch } from "../components/manualGrab/useManualGrabBatch";
import { SNATCH_LAG_HINT, grabLabel, linesOf } from "../components/manualGrab/text";
import { TorrentDropZone } from "../components/manualGrab/TorrentDropZone";
import { useCarriedLinks } from "../components/manualGrab/useCarriedLinks";
import { MAX_BATCH, wedgeEligible } from "../components/manualGrab/types";
import MobileGrabFromMamPage from "./MobileGrabFromMamPage";

export default function GrabFromMamPage({ initial }: { initial?: string | number | null }) {
  const vp = useViewport();
  if (useMobileCodepath(vp)) return <MobileGrabFromMamPage initial={initial} />;
  return <DesktopGrabFromMamPage initial={initial} />;
}

function DesktopGrabFromMamPage({ initial }: { initial?: string | number | null }) {
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

  const banner = (color: string, msg: string) => (
    <div style={{
      background: color + "22", border: `1px solid ${color}55`, color,
      padding: "10px 14px", borderRadius: 8, fontSize: 13,
    }}>
      {msg}
    </div>
  );

  const full = batch.entries.length >= MAX_BATCH;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <div>
        <h1 style={{ fontSize: 24, fontWeight: 700, color: t.text, margin: "0 0 4px" }}>
          Grab from MAM
        </h1>
        <p style={{ fontSize: 14, color: t.textDim, margin: 0 }}>
          Paste MAM links or torrent IDs, or drop .torrent files you downloaded from MAM,
          up to {MAX_BATCH} at a time. Seshat shows what each one is and what you already
          have, then grabs the ones you tick. A dropped .torrent goes straight to
          qBittorrent, with no second download from MAM.
        </p>
      </div>

      <div style={{ background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 12, padding: 20 }}>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => (e.ctrlKey || e.metaKey) && e.key === "Enter" && add()}
          placeholder={"One per line:\nhttps://www.myanonamouse.net/t/1274788\n1274722"}
          rows={4}
          disabled={full}
          style={{
            width: "100%", padding: 12, fontSize: 13, fontFamily: "monospace",
            borderRadius: 8, resize: "vertical",
            background: t.inp, color: t.text, border: `1px solid ${t.border}`,
          }}
        />
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 8 }}>
          <Btn variant="primary" onClick={add} disabled={!text.trim() || full}>
            Look up
          </Btn>
          <span style={{ fontSize: 12, color: t.td }}>{SNATCH_LAG_HINT}</span>
        </div>
        <div style={{ marginTop: 14 }}>
          <TorrentDropZone onFiles={(f) => void batch.addFiles(f)} multiple disabled={full} />
        </div>
      </div>

      {batch.notice && banner(t.warn, batch.notice)}
      {batch.error && banner(t.err, batch.error)}

      {batch.entries.length > 0 && (
        <div style={{ background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 12, padding: "12px 20px 16px" }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", paddingBottom: 8 }}>
            <span style={{ fontSize: 13, color: t.td }}>
              {batch.entries.length} of {MAX_BATCH}
              {batch.pending && " · looking up…"}
            </span>
            <Btn variant="ghost" size="sm" onClick={batch.clear} disabled={batch.grabbing}>
              Clear list
            </Btn>
          </div>

          {batch.entries.map((e) => (
            <GrabPreviewRow
              key={e.key}
              entry={e}
              willWedge={batch.useWedges && wedgeEligible(e)}
              onTick={(on) => batch.setTicked(e.key, on)}
              onConfirmSnatched={() => batch.confirmSnatched(e.key)}
              onCancelConfirm={() => batch.cancelConfirm(e.key)}
              onRemove={batch.grabbing ? undefined : () => batch.remove(e.key)}
              onRetry={() => batch.retry(e.key)}
            />
          ))}

          <GrabFooter batch={batch} />
        </div>
      )}
    </div>
  );
}

function GrabFooter({ batch }: { batch: ManualGrabBatch }) {
  const t = useTheme();
  const { nav } = useNavigation();
  const showWedges = batch.offerWedges && (batch.useWedges || batch.eligibleForWedges > 0);
  const blocked =
    !batch.tickedCount || batch.pending || batch.grabbing || batch.wedgeShort ||
    (batch.useWedges && batch.wedgeCount > 0 && !batch.wedges);
  return (
    <div style={{ borderTop: `1px solid ${t.borderL}`, paddingTop: 12, marginTop: 4, display: "flex", flexDirection: "column", gap: 8 }}>
      {showWedges && (
        <label style={{ fontSize: 13, color: t.text2, display: "flex", alignItems: "center", gap: 8 }}>
          <input
            type="checkbox"
            checked={batch.useWedges}
            disabled={batch.grabbing}
            onChange={(e) => void batch.setUseWedges(e.target.checked)}
            style={{ accentColor: t.accent }}
          />
          Use wedges on the ticked rows that aren't free
          {batch.useWedges && batch.wedges && (
            <span style={{ color: t.td }}>
              ({batch.wedges.spendable} spendable: {batch.wedges.wedges} − {batch.wedges.reserved} reserved)
            </span>
          )}
        </label>
      )}
      {batch.wedgesSwitchedOff && batch.eligibleForWedges > 0 && (
        <div style={{ fontSize: 12, color: t.td }}>
          Wedges are off for manual grabs. Turn them on in{" "}
          <a
            href="#"
            onClick={(e) => { e.preventDefault(); nav("pipe-mam"); }}
            style={{ color: t.accent }}
          >
            MAM Status › Wedges on manual grabs
          </a>
          .
        </div>
      )}
      {batch.wedgeError && <div style={{ fontSize: 12, color: t.err }}>{batch.wedgeError}</div>}
      {batch.wedgeShort && batch.wedges && (
        <div style={{ fontSize: 12, color: t.err }}>
          Needs {batch.wedgeCount} wedges, {batch.wedges.spendable} spendable
          ({batch.wedges.wedges} − {batch.wedges.reserved} reserved). Untick rows or turn wedges off.
        </div>
      )}
      <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", gap: 12 }}>
        {batch.willQueue > 0 && (
          <span style={{ fontSize: 12, color: t.td }}>
            {batch.willQueue} will queue: your snatch budget is full
          </span>
        )}
        {batch.grabbing && <Spin size={16} />}
        <Btn variant="primary" onClick={batch.grabAll} disabled={blocked}>
          {grabLabel(batch)}
        </Btn>
      </div>
    </div>
  );
}
