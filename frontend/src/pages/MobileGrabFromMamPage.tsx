// Mobile twin of GrabFromMamPage: same review list, mobile chrome.
import { useEffect, useState } from "react";
import { useTheme } from "../theme";
import { useNavigation } from "../providers/NavigationProvider";
import { MobileBackButton, MobileBtn, MobileInput } from "../components/mobile";
import { GrabPreviewRow } from "../components/manualGrab/GrabPreviewRow";
import { useManualGrabBatch } from "../components/manualGrab/useManualGrabBatch";
import { SNATCH_LAG_HINT, firstLine } from "../components/manualGrab/text";
import { TorrentDropZone } from "../components/manualGrab/TorrentDropZone";

export default function MobileGrabFromMamPage({ initial }: { initial?: string | number | null }) {
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const preview = () => {
    const value = link.trim();
    if (!value) return;
    batch.clear();
    batch.addLinks([value]);
  };

  const upload = (files: File[]) => {
    batch.clear();
    void batch.addFiles(files);
  };

  const label =
    `Grab ${batch.tickedCount}` + (batch.flCount ? ` · ${batch.flCount * 50}k BP` : "");

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <MobileBackButton to="dashboard" label="Dashboard" />
      <div>
        <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: t.text }}>Grab from MAM</h1>
        <p style={{ fontSize: 13, color: t.td, margin: "4px 0 0" }}>
          Paste a MAM link or torrent ID, or add a .torrent you downloaded.
        </p>
      </div>

      <MobileInput
        value={link}
        onChange={(e) => setLink(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && preview()}
        placeholder="MAM link or torrent ID"
        inputMode="url"
      />
      <MobileBtn primary fullWidth onClick={preview} disabled={!link.trim()}>
        Look up
      </MobileBtn>
      <p style={{ fontSize: 12, color: t.td, margin: 0 }}>{SNATCH_LAG_HINT}</p>
      <TorrentDropZone onFiles={upload} compact />

      {batch.error && (
        <div style={{ color: t.err, fontSize: 13 }}>{batch.error}</div>
      )}

      {batch.entries.length > 0 && (
        <div>
          {batch.entries.map((e) => (
            <GrabPreviewRow
              key={e.key}
              entry={e}
              compact
              onTick={(on) => batch.setTicked(e.key, on)}
              onBuyFl={(on) => batch.setBuyFl(e.key, on)}
              onConfirmSnatched={() => batch.confirmSnatched(e.key)}
              onCancelConfirm={() => batch.cancelConfirm(e.key)}
            />
          ))}
          <MobileBtn
            primary
            fullWidth
            onClick={batch.grabAll}
            disabled={!batch.tickedCount || batch.pending || batch.grabbing}
          >
            {batch.grabbing ? "Grabbing…" : label}
          </MobileBtn>
        </div>
      )}
    </div>
  );
}
