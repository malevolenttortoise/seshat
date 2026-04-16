// Number formatting utilities used across all pages.

/** Format a number with comma separators: 91925.3 → "91,925.3" */
export function fmtNum(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n.toLocaleString("en-US");
}

/** Format bytes to human-readable: 1234567890 → "1.15 GB" */
export function fmtBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i > 0 ? 2 : 0)} ${units[i]}`;
}

/** Format seconds as a compact duration: 3661 → "1h 1m", 86400 → "24h 0m" */
export function fmtDuration(seconds: number): string {
  if (seconds <= 0) return "0m";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h === 0) return `${m}m`;
  return `${h}h ${m}m`;
}

/** Format a ratio with 1 decimal: 91925.2345 → "91,925.2" */
export function fmtRatio(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const fixed = n.toFixed(1);
  const [whole, dec] = fixed.split(".");
  return `${parseInt(whole).toLocaleString("en-US")}.${dec}`;
}

// ─── Discovery-domain formatters (from AthenaScout) ─────────

/** Percentage: pct(50, 200) → 25, pct(0, 0) → 100 */
export const pct = (o: number, t: number): number =>
  t === 0 ? 100 : Math.floor((o / t) * 1000) / 10;

/** Relative time: "Just now" / "5m ago" / "3h ago" / "2d ago" / "Never" */
export const timeAgo = (ts: number | null | undefined): string => {
  if (!ts) return "Never";
  const d = Math.floor((Date.now() / 1000 - ts) / 60);
  if (d < 1) return "Just now";
  if (d < 60) return `${d}m ago`;
  if (d < 1440) return `${Math.floor(d / 60)}h ago`;
  return `${Math.floor(d / 1440)}d ago`;
};

/** Date string → YYYY-MM-DD (truncating time component) */
export const fmtDate = (d: string | null | undefined): string =>
  d && d.length >= 10 ? d.substring(0, 10) : d || "";
