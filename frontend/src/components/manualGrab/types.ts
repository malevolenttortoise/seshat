// Shapes returned by /api/v1/manual-grab (app/routers/manual_grab.py).
// Shared by the Grab from MAM page and, later, Proactive Search's
// review surface.

export type PreviewStatus =
  | "ready"
  | "owned"
  | "in_flight_sibling"
  | "snatched_on_mam"
  | "policy_skip"
  | "already_grabbed"
  | "removed_from_mam"
  | "excluded_uploader"
  | "bad_input"
  | "lookup_failed";

// Rows that can never be ticked. The rest start unticked unless
// "ready"; ticking them is the user's decision (ADR-0023).
export const BLOCKING_STATUSES: ReadonlySet<PreviewStatus> = new Set([
  "already_grabbed",
  "removed_from_mam",
  "excluded_uploader",
  "bad_input",
  "lookup_failed",
]);

export interface PreviewRow {
  kind: "link";
  input: string;
  status: PreviewStatus;
  message: string;
  torrent_id: string | null;
  title: string;
  authors: string[];
  narrators: string[];
  series: { name: string; index: string }[];
  category: string;
  filetype: string;
  size_bytes: number | null;
  seeders: number | null;
  vip: boolean;
  freeleech: boolean;
  personal_freeleech: boolean;
  my_snatched: boolean;
  owned_in: { library_slug: string; format: string }[];
  in_flight: boolean;
  policy_tier: string;
  policy_grab: boolean;
  wedge_eligible: boolean;
  grab_id: number | null;
  cover_url: string | null;
}

export type JobRowStatus =
  | "pending"
  | "working"
  | "submitted"
  | "queued"
  | "refused"
  | "failed";

export interface JobRow {
  index: number;
  kind: string;
  input: string;
  torrent_id: string | null;
  status: JobRowStatus;
  reason: string;
  message: string;
  grab_id: number | null;
  personal_fl_bought: boolean;
}

export interface GrabJob {
  job_id: string;
  done: boolean;
  rows: JobRow[];
}

// One row of the review list: what the user typed, its preview once
// it arrives, the user's choices, and (after Grab all) its outcome.
export interface GrabEntry {
  key: string;
  input: string;
  preview: PreviewRow | null; // null while the lookup is pending
  ticked: boolean;
  buyFl: boolean;
  overrideSnatched: boolean;
  confirming: boolean; // the "Download again?" confirm is open (D13)
  result: JobRow | null;
}

export const isFree = (p: PreviewRow) =>
  p.vip || p.freeleech || p.personal_freeleech;
