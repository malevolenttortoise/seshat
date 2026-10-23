// The Dashboard's "Clean up missing books" (discovery fix pass S8, G209):
// the discovery rules applied to missing books listed before them.
// Preview runs every step on copies of the databases and reports what it
// would change; Apply runs the same steps for real and follows the
// preview it was shown with (the server refuses an old or applied one).
// Both Dashboard shells open the same dialog from this hook.
import { useCallback, useState } from "react";
import { api } from "../api";
import { REASON_GROUPS } from "../lib/bookFilters";
import { useVisibleInterval } from "./useVisibleInterval";

const STATUS_PATH = "/discovery/missing-cleanup/status";
const RUNNING_POLL_MS = 2000;

export type CleanupAction = "remove" | "hide" | "fold" | "file" | "merge";

export interface CleanupRow {
  library: string;
  id: number | null;
  title: string;
  authors?: string[];
  series?: string | null;
  index?: number | null;
  hidden?: boolean;
  hidden_reason?: string | null;
  detail?: string;
  // Step 3: the family the row joins; false when that family is shown.
  reason?: string;
  stays_shown?: boolean;
  // Step 4: the owned book the copy folds into.
  into_id?: number;
  into_title?: string;
}

export interface CleanupStep {
  key: string;
  name: string;
  action: CleanupAction;
  count: number;
  sample: CleanupRow[];
  skipped_count: number;
}

export interface CleanupReport {
  id: string;
  mode: "preview" | "apply";
  started_at: number;
  finished_at: number | null;
  preview_id?: string;
  steps: CleanupStep[];
  errors: string[];
}

export interface CleanupStatus {
  running: boolean;
  mode: "preview" | "apply" | null;
  library_name?: string;
  step_name?: string;
  preview_ttl_s: number;
  last_preview: CleanupReport | null;
  last_apply: CleanupReport | null;
}

/** What each step is, for the dialog before any preview has run. */
export const CLEANUP_STEPS: { key: string; name: string; blurb: string }[] = [
  {
    key: "no_eligible_author",
    name: "No author you scan",
    blurb: "Removes missing books none of whose authors you scan in that library: you own one of their books there, or you put them on the allow list.",
  },
  {
    key: "openlibrary_only",
    name: "Found only by OpenLibrary",
    blurb: "Removes books no other source knows, unless a MAM scan found a torrent for them.",
  },
  {
    key: "filtered",
    name: "Comics, box sets, anthologies and other non-books",
    blurb: "Hides them with their reason, as new books are hidden now (Hidden → Filtered).",
  },
  {
    key: "owned_copy",
    name: "Copies of books you own",
    blurb: "Folds a missing copy into the one owned book it is. A copy that matches several books stays listed.",
  },
  {
    key: "listed_copy",
    name: "Books listed twice",
    blurb: "Folds two missing rows of one book into the one more sources know, which takes the other's links.",
  },
  {
    key: "series_in_title",
    name: "Series named in the title",
    blurb: "Files a standalone book in the series its title names.",
  },
  {
    key: "cache_remerge",
    name: "Amazon and Goodreads books merged again",
    blurb: "Merges what the source caches already hold again, so books an older merge folded into another book come back as their own.",
  },
];

const VERBS: Record<CleanupAction, [string, string]> = {
  remove: ["Remove", "Removed"],
  hide: ["Hide", "Hidden"],
  fold: ["Fold in", "Folded in"],
  file: ["File", "Filed"],
  merge: ["Add", "Added"],
};

/** "Remove" in a preview, "Removed" after Apply. */
export function stepVerb(action: CleanupAction, mode: "preview" | "apply"): string {
  const v = VERBS[action] ?? [action, action];
  return mode === "apply" ? v[1] : v[0];
}

const reasonLabel = (reason?: string | null): string =>
  REASON_GROUPS.find((g) => g.reasons.includes(reason ?? ""))?.label ?? (reason || "");

/** The short note beside a sample row: why the step takes it. */
export function rowNote(stepKey: string, row: CleanupRow): string {
  switch (stepKey) {
    case "filtered":
      return reasonLabel(row.reason) + (row.stays_shown ? " · stays shown: that filter is off" : "");
    case "owned_copy":
    case "listed_copy":
      return row.into_title ? `into “${row.into_title}”` : "";
    case "series_in_title":
      return row.detail ?? "";
    case "cache_remerge":
      return row.hidden ? `hidden: ${reasonLabel(row.hidden_reason)}` : "listed as missing";
    default:
      return "";
  }
}

const reason = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function useMissingCleanup(onFail: (message: string) => void) {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<CleanupStatus | null>(null);
  const [starting, setStarting] = useState(false);

  const load = useCallback(async () => {
    try {
      setStatus(await api.get<CleanupStatus>(STATUS_PATH));
    } catch (e) {
      onFail(`Couldn't read the cleanup's status: ${reason(e)}`);
    }
  }, [onFail]);

  // A run goes on in the background: poll while the dialog shows it. (The
  // interval restarts on a new delay, not a new callback.)
  useVisibleInterval(load, open && status?.running ? RUNNING_POLL_MS : 0);

  const show = () => {
    setOpen(true);
    load();
  };
  const close = () => setOpen(false);

  const start = async (path: string, body: unknown, what: string) => {
    setStarting(true);
    try {
      await api.post(path, body);
    } catch (e) {
      onFail(`${what}: ${reason(e)}`);
    }
    setStarting(false);
    await load();
  };
  const preview = () =>
    start("/discovery/missing-cleanup/preview", undefined, "Couldn't start the preview");

  const last = status?.last_preview ?? null;
  // The Apply that followed the last preview, if there was one.
  const applied = last && status?.last_apply?.preview_id === last.id ? status.last_apply : null;
  const fresh =
    !!last?.finished_at && Date.now() / 1000 - last.finished_at < (status?.preview_ttl_s ?? 0);
  const canApply = !!last && fresh && !applied && !status?.running;
  const changes = last ? last.steps.reduce((n, s) => n + s.count, 0) : 0;

  const apply = () => {
    if (!last) return Promise.resolve();
    return start("/discovery/missing-cleanup/apply", { preview_id: last.id }, "Couldn't apply the cleanup");
  };

  return {
    open, show, close, status, starting, running: !!status?.running,
    preview, apply, last, applied, fresh, canApply, changes,
  };
}
