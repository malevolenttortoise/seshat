// Copy and small helpers shared by both Grab from MAM shells.

// D10: MAM's my_snatched lags 5-20 min, so a link pasted right after a
// browser download would be downloaded a second time.
export const SNATCH_LAG_HINT =
  "Already downloaded the .torrent? Drop the file instead — MAM takes up to 20 min to show it as snatched, and a pasted link downloads it again.";

// What the Import page hands over: the MAM lines it spotted.
export function firstLine(text: string | number | null | undefined): string {
  return String(text ?? "").split("\n").map((s) => s.trim()).find(Boolean) ?? "";
}
