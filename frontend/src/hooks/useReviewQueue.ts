// The Review page's queue: the list (30s visible poll), its import
// failures / pending split, and every action on a review.
//
// ReviewPage + MobileReviewPage each carried this (wave 5b, issue 22;
// S6a first made the two copies agree on failure messages). The shells
// keep the presentation: confirm prompts and the note each one sends
// (`claimForOwned(…, note)`, `bulk(action, note)`). Failures land in
// `error`, as they did; a successful refresh clears it, so an action
// refreshes before it reports.
import { useEffect, useState } from "react";
import { api } from "../api";
import { useVisibleInterval } from "./useVisibleInterval";

export interface ReviewListResponse<T> {
  items: T[];
  pending_count: number;
}

interface BulkResult {
  processed: number;
  failed: number;
  errors: string[];
}

interface ActionResult {
  ok: boolean;
  error?: string | null;
}

export function useReviewQueue<T extends { id: number; status: string }>() {
  const [items, setItems] = useState<T[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);

  async function refresh() {
    try {
      const r = await api.get<ReviewListResponse<T>>("/v1/review");
      setItems(r.items);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => { refresh(); }, []);
  useVisibleInterval(refresh, 30_000);

  /** Run one review's action: busy on that row, refresh after, error on failure. */
  async function onRow(id: number, run: () => Promise<void>) {
    setBusyId(id);
    try {
      await run();
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyId(null);
    }
  }

  const approve = (id: number, metadata?: Record<string, unknown>) =>
    onRow(id, async () => {
      await api.post(`/v1/review/${id}/approve`, { metadata: metadata || null });
    });

  const saveEdits = (id: number, metadata: Record<string, unknown>) =>
    onRow(id, async () => {
      await api.post(`/v1/review/${id}/save`, { metadata });
    });

  async function reEnrich(id: number, metadata: Record<string, unknown>): Promise<boolean> {
    setBusyId(id);
    setError(null);
    try {
      await api.post(`/v1/review/${id}/re-enrich`, { metadata });
      await refresh();
      return true;
    } catch (e) {
      setError(String(e));
      return false;
    } finally {
      setBusyId(null);
    }
  }

  const reject = (id: number) =>
    onRow(id, async () => {
      await api.post(`/v1/review/${id}/reject`, { note: "rejected via UI" });
    });

  const claimForOwned = (id: number, library_slug: string, book_id: number, note: string) =>
    onRow(id, async () => {
      await api.post(`/v1/review/${id}/claim-for-owned`, { library_slug, book_id, note });
    });

  /** Re-drop / Mark as imported: the server answers `{ok, error}`. */
  async function importAction(id: number, path: "redrop" | "mark-imported", failure: string) {
    setBusyId(id);
    setError(null);
    try {
      const r = await api.post<ActionResult>(`/v1/review/${id}/${path}`);
      // Refresh first: a successful refresh clears `error`.
      await refresh();
      if (!r.ok) setError(`${failure}: ${r.error ?? "unknown error"}`);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyId(null);
    }
  }

  const redrop = (id: number) => importAction(id, "redrop", "Re-drop failed");
  const markImported = (id: number) => importAction(id, "mark-imported", "Couldn't mark as imported");

  /** Approve / reject every pending review; `note` is sent when given. */
  async function bulk(action: "approve" | "reject", note?: string) {
    setBulkBusy(true);
    setError(null);
    try {
      const r = await api.post<BulkResult>(
        `/v1/review/bulk/${action}`,
        note === undefined ? undefined : { note },
      );
      // Refresh first: a successful refresh clears `error`.
      await refresh();
      if (r.failed > 0) {
        setError(
          `${action === "approve" ? "Approved" : "Rejected"} ${r.processed}, ${r.failed} failed. First errors: ${r.errors.slice(0, 3).join("; ")}`,
        );
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setBulkBusy(false);
    }
  }

  // Import failures (wave 5a) come first and take their own actions;
  // bulk approve / reject only ever touch pending reviews.
  const failed = (items ?? []).filter((i) => i.status === "import_failed");
  const pending = (items ?? []).filter((i) => i.status !== "import_failed");

  return {
    items, error, busyId, bulkBusy, failed, pending, refresh,
    approve, saveEdits, reEnrich, reject, claimForOwned, redrop, markImported, bulk,
  };
}
