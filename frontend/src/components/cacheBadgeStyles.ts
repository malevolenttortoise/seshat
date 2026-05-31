// Shared style helpers for the per-author cache status badges.
//
// v3.6.0 frontend parity slice — extracted from
// `AuthorCacheStatusBadge.tsx` so the new
// `GoodreadsAuthorCacheStatusBadge.tsx` reuses the chip palette +
// container layout instead of copy-pasting them. Palette colors
// mirror `SourceBadgeRow.SOURCE_PALETTE` so cache badges visually
// anchor to the existing per-author source ID chips above them.

import type { Theme } from "../theme";

export type CacheSource = "amazon" | "goodreads";

interface BadgePalette { bg: string; fg: string; br: string }

const CACHE_PALETTE: Record<CacheSource, BadgePalette> = {
  amazon:    { bg: "#3d2e1a", fg: "#f0a83c", br: "#7a5c2a" },
  goodreads: { bg: "#553b1a", fg: "#e8c070", br: "#88642a" },
};


export function cacheChipStyle(source: CacheSource): React.CSSProperties {
  const palette = CACHE_PALETTE[source];
  return {
    background: palette.bg,
    color: palette.fg,
    border: `1px solid ${palette.br}`,
    padding: "1px 7px",
    borderRadius: 99,
    fontSize: 10,
    fontWeight: 700,
    textTransform: "uppercase",
    letterSpacing: 0.5,
    flexShrink: 0,
    height: 18,
    display: "inline-flex",
    alignItems: "center",
  };
}


export function cacheBadgeContainerStyle(t: Theme): React.CSSProperties {
  return {
    display: "flex",
    gap: 12,
    alignItems: "flex-start",
    padding: "6px 12px",
    background: t.bg2,
    border: `1px solid ${t.borderL}`,
    borderRadius: 8,
    marginTop: 6,
    minHeight: 28,
  };
}


export function formatSecondsAgo(s: number): string {
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h ago`;
  return `${(s / 86400).toFixed(1)}d ago`;
}
