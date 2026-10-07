// Import-page hint (D5): the Import box adds *books* to Discovery from
// Goodreads/Hardcover/... pages. A MAM torrent link pasted there belongs
// to Grab from MAM instead; this spots it and carries it over.
import { useTheme } from "../../theme";
import { useNavigation } from "../../providers/NavigationProvider";

const MAM_RX = /myanonamouse\.net\/(t\/\d+|tor\/download\.php\?tid=\d+)/i;
const BARE_ID_RX = /^\d+$/;

export function isMamLine(line: string): boolean {
  const s = line.trim();
  return MAM_RX.test(s) || BARE_ID_RX.test(s);
}

export function MamLinkHint({ text }: { text: string }) {
  const t = useTheme();
  const { nav } = useNavigation();
  const lines = text.split("\n").map((s) => s.trim()).filter(isMamLine);
  if (!lines.length) return null;
  return (
    <div
      style={{
        display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10,
        padding: "10px 12px", borderRadius: 8, marginTop: 10,
        background: t.ylwb, border: `1px solid ${t.ylwt}`,
      }}
    >
      <span style={{ fontSize: 13, color: t.text, flex: "1 1 240px" }}>
        {lines.length === 1
          ? "That's a MAM torrent, not a book page."
          : `${lines.length} of these are MAM torrents, not book pages.`}{" "}
        Import adds books to Discovery; Grab from MAM downloads torrents.
      </span>
      <button
        onClick={() => nav("pipe-manual-grab", lines.join("\n"))}
        style={{
          padding: "6px 12px", fontSize: 13, fontWeight: 600, borderRadius: 6,
          background: t.accent, color: t.bg, border: `1px solid ${t.accent}`, cursor: "pointer",
        }}
      >
        Open in Grab from MAM →
      </button>
    </div>
  );
}
