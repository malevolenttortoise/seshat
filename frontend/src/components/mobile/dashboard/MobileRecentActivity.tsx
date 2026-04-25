// Recent grabs list. The desktop dashboard uses an inline ul-style
// row; on mobile each grab gets a tappable row with the torrent
// name, author/series fragment, and a relative-time badge.
import { useTheme } from "../../../theme";
import { useViewport } from "../../../hooks/useViewport";
import { RADIUS, scaleFor } from "../tokens";

export interface RecentGrab {
  torrent_name?: string;
  grabbed_at?: string;
}

export interface MobileRecentActivityProps {
  grabs: RecentGrab[];
  // Cap the rendered list. Default 5 (matches Hermes inline cap).
  max?: number;
  // Empty state message — overridable since the same component is
  // used for "Recent Activity" and "Recent Grabs" contexts.
  emptyText?: string;
}

function timeAgo(iso?: string): string {
  if (!iso) return "—";
  const ts = Date.parse(iso);
  if (Number.isNaN(ts)) return "—";
  const mins = Math.floor((Date.now() - ts) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  const days = Math.floor(hrs / 24);
  return `${days}d`;
}

export function MobileRecentActivity({
  grabs,
  max = 5,
  emptyText = "No recent activity.",
}: MobileRecentActivityProps) {
  const t = useTheme();
  const vp = useViewport();
  const s = scaleFor(vp);

  if (grabs.length === 0) {
    return (
      <div
        style={{
          padding: s.pad.tight,
          fontSize: s.type.caption,
          color: t.tg,
          textAlign: "center",
        }}
      >
        {emptyText}
      </div>
    );
  }

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: s.space.xs,
      }}
    >
      {grabs.slice(0, max).map((g, i) => (
        <div
          key={i}
          style={{
            display: "flex",
            alignItems: "center",
            gap: s.space.sm,
            padding: `${s.space.sm}px ${s.space.md}px`,
            background: t.bg3,
            borderRadius: RADIUS.sm,
          }}
        >
          <span
            style={{
              flex: 1,
              minWidth: 0,
              fontSize: s.type.caption,
              color: t.text2,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {g.torrent_name || "(unnamed)"}
          </span>
          <span
            style={{
              fontSize: s.type.micro,
              color: t.tg,
              fontWeight: 600,
              flexShrink: 0,
            }}
          >
            {timeAgo(g.grabbed_at)}
          </span>
        </div>
      ))}
    </div>
  );
}
