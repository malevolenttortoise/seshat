// Grab from MAM — paste a MAM link or torrent ID, see what it is, grab it.
//
// Each row's preview comes from /api/v1/manual-grab/preview (what MAM
// says + what you already own); Grab sends the ticked rows to a
// server-side job and the rows update as it runs. A torrent Seshat
// already grabbed can't be grabbed again; one you own or already
// snatched on MAM starts unticked and is yours to decide (ADR-0023).
import { useEffect, useState } from "react";
import { useTheme } from "../theme";
import { useNavigation } from "../providers/NavigationProvider";
import { Btn } from "../components/Btn";
import { Spin } from "../components/Spin";
import { useViewport } from "../hooks/useViewport";
import { useMobileCodepath } from "../components/mobile";
import { GrabPreviewRow } from "../components/manualGrab/GrabPreviewRow";
import { useManualGrabBatch } from "../components/manualGrab/useManualGrabBatch";
import { SNATCH_LAG_HINT, firstLine } from "../components/manualGrab/text";
import MobileGrabFromMamPage from "./MobileGrabFromMamPage";

export default function GrabFromMamPage({ initial }: { initial?: string | number | null }) {
  const vp = useViewport();
  if (useMobileCodepath(vp)) return <MobileGrabFromMamPage initial={initial} />;
  return <DesktopGrabFromMamPage initial={initial} />;
}

function DesktopGrabFromMamPage({ initial }: { initial?: string | number | null }) {
  const t = useTheme();
  const batch = useManualGrabBatch();
  const { nav } = useNavigation();
  const [link, setLink] = useState(firstLine(initial));

  useEffect(() => {
    const carried = firstLine(initial);
    if (carried) {
      batch.clear();
      batch.addLinks([carried]);
      // Consumed: drop the page arg so a refresh doesn't look it up again.
      nav("pipe-manual-grab", null);
    }
    // Only the hand-over from Import on first render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const preview = () => {
    const value = link.trim();
    if (!value) return;
    batch.clear();
    batch.addLinks([value]);
  };

  const label =
    `Grab ${batch.tickedCount}` + (batch.flCount ? ` · ${batch.flCount * 50}k BP` : "");

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <div>
        <h1 style={{ fontSize: 24, fontWeight: 700, color: t.text, margin: "0 0 4px" }}>
          Grab from MAM
        </h1>
        <p style={{ fontSize: 14, color: t.textDim, margin: 0 }}>
          Paste a MAM link or torrent ID. Seshat looks it up, shows you what it is and
          what you already have, and grabs it when you say so.
        </p>
      </div>

      <div style={{ background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 12, padding: 20 }}>
        <div style={{ display: "flex", gap: 8 }}>
          <input
            value={link}
            onChange={(e) => setLink(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && preview()}
            placeholder="https://www.myanonamouse.net/t/1274788  or  1274788"
            style={{
              flex: 1, padding: "9px 12px", fontSize: 14, borderRadius: 6,
              background: t.inp, color: t.text, border: `1px solid ${t.border}`,
            }}
          />
          <Btn variant="primary" onClick={preview} disabled={!link.trim()}>
            Look up
          </Btn>
        </div>
        <p style={{ fontSize: 12, color: t.td, margin: "8px 0 0" }}>{SNATCH_LAG_HINT}</p>
      </div>

      {batch.error && (
        <div style={{
          background: t.err + "22", border: `1px solid ${t.err}55`, color: t.err,
          padding: "10px 14px", borderRadius: 8, fontSize: 13,
        }}>
          {batch.error}
        </div>
      )}

      {batch.entries.length > 0 && (
        <div style={{ background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 12, padding: "8px 20px 16px" }}>
          {batch.entries.map((e) => (
            <GrabPreviewRow
              key={e.key}
              entry={e}
              onTick={(on) => batch.setTicked(e.key, on)}
              onBuyFl={(on) => batch.setBuyFl(e.key, on)}
              onConfirmSnatched={() => batch.confirmSnatched(e.key)}
              onCancelConfirm={() => batch.cancelConfirm(e.key)}
            />
          ))}
          <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", gap: 12, paddingTop: 12 }}>
            {batch.grabbing && <Spin size={16} />}
            <Btn
              variant="primary"
              onClick={batch.grabAll}
              disabled={!batch.tickedCount || batch.pending || batch.grabbing}
            >
              {label}
            </Btn>
          </div>
        </div>
      )}
    </div>
  );
}
