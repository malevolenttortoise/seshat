// What the book sidebar's actions send and what they leave on screen.
// Wave 5b S14 moves suggestions, reingest, send to pipeline and the
// contributors out of BookSidebar; the render snapshots cover what each
// shows, these cover the request each action makes and what follows it.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import { BookSidebar } from "../../components/BookSidebar";
import { EVT, type Book } from "../../types";
import { renderPage, resetPageEnv, type PageRender, type Routes } from "../render";
import {
  coauthored,
  missingOnMam,
  ownedWithSuggestion,
  preflightShort,
  reingestTwoCandidates,
  sidebarCoauthoredRoutes,
  sidebarMissingRoutes,
  sidebarOwnedRoutes,
  sidebarSnatchedRoutes,
  snatchedOnMam,
} from "../fixtures/bookSidebar";

let toasts: string[] = [];
const onToast = (e: Event) => {
  const d = (e as CustomEvent<{ kind: string; msg: string }>).detail;
  toasts.push(`${d.kind}: ${d.msg}`);
};
beforeEach(() => {
  toasts = [];
  window.addEventListener(EVT.Toast, onToast);
});
afterEach(() => {
  window.removeEventListener(EVT.Toast, onToast);
  vi.restoreAllMocks();
  resetPageEnv();
});

async function sidebar(book: Book, routes: Routes) {
  const onEdit = vi.fn();
  const r = await renderPage(
    <BookSidebar book={book} closing={false} onClose={vi.fn()} onAction={vi.fn()} onEdit={onEdit} />,
    { viewport: "desktop", routes },
  );
  return { r, onEdit };
}

async function click(r: PageRender, el: Element) {
  act(() => { fireEvent.click(el); });
  await r.settle();
}

const button = (r: PageRender, text: string) => r.getByText(text, { exact: false }).closest("button")!;

const finishedJob = (result: unknown) => ({
  job_id: "j1", done: true, total: 1, completed: 1, rows: [], result, error: null,
});

describe("Book sidebar actions", () => {
  it("send to pipeline with the wedge ticked, then the tick clears", async () => {
    const alert = vi.spyOn(window, "alert").mockImplementation(() => {});
    const { r } = await sidebar(missingOnMam, {
      ...sidebarMissingRoutes,
      "POST /v1/mam/economy/preflight": { ...preflightShort, sufficient: true, shortfall_gb: 0 },
      "POST /discovery/send-to-pipeline": finishedJob({ sent: 1 }),
    });
    const tick = r.getByText("Use wedge").closest("label")!.querySelector("input")!;
    await click(r, tick);
    expect(tick.checked).toBe(true);
    await click(r, button(r, "Send to pipeline"));
    expect(r.unmatched).toEqual([]);
    expect(r.requests).toContain('POST /v1/mam/economy/preflight {"torrent_id":"900002"}');
    expect(r.requests).toContain('POST /discovery/send-to-pipeline {"book_ids":[102],"use_wedge_override":true}');
    expect(alert).toHaveBeenCalledWith("Sent to pipeline for download!");
    expect(tick.checked).toBe(false);
  });

  it("the buffer gate's Cancel closes the banner without sending", async () => {
    const { r } = await sidebar(missingOnMam, {
      ...sidebarMissingRoutes,
      "POST /v1/mam/economy/preflight": preflightShort,
    });
    await click(r, button(r, "Send to pipeline"));
    expect(r.queryByText("not enough upload buffer", { exact: false })).not.toBeNull();
    await click(r, button(r, "Cancel"));
    expect(r.queryByText("not enough upload buffer", { exact: false })).toBeNull();
    expect(r.requests.filter((q) => q.startsWith("POST /discovery/send-to-pipeline"))).toEqual([]);
  });

  it("reingest starts with the picked candidate", async () => {
    const { r, onEdit } = await sidebar(snatchedOnMam, {
      ...sidebarSnatchedRoutes,
      "POST /discovery/books/106/reingest/probe": reingestTwoCandidates,
      "POST /discovery/books/106/reingest/start": { ok: true, grab_id: 77, pipeline_run_id: 88, error: null },
    });
    await click(r, button(r, "Reingest from disk"));
    await click(r, button(r, "Orchard Cycle 1-3"));
    expect(r.unmatched).toEqual([]);
    expect(r.requests).toContain(
      `POST /discovery/books/106/reingest/start?slug=calibre-library ${JSON.stringify({ candidate: reingestTwoCandidates.candidates[1] })}`,
    );
    expect(toasts).toContain("success: Reingest started: grab #77, run #88. Check the Review queue.");
    expect(r.queryByText("Multiple matches found", { exact: false })).toBeNull();
    expect(onEdit).toHaveBeenCalled();
  });

  it("removing one of two authors", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const { r, onEdit } = await sidebar(coauthored, {
      ...sidebarCoauthoredRoutes,
      "DELETE /discovery/books/107/contributors/12": {
        contributors: [coauthored.contributors![0]],
        removed_author_orphaned: true,
      },
    });
    await click(r, r.getByLabelText("Remove Bram Okafor"));
    expect(r.unmatched).toEqual([]);
    expect(r.requests).toContain("DELETE /discovery/books/107/contributors/12?slug=calibre-library");
    expect(r.queryByText("Bram Okafor")).toBeNull();
    expect(toasts).toContain("success: Removed Bram Okafor — now-empty author will be cleaned up automatically");
    expect(onEdit).toHaveBeenCalled();
  });

  it("replacing a sole author", async () => {
    const { r } = await sidebar(missingOnMam, {
      ...sidebarMissingRoutes,
      "GET /discovery/authors/search": { authors: [{ id: 14, name: "Cora Lindqvist" }] },
      "DELETE /discovery/books/102/contributors/11": {
        contributors: [{ author_id: 14, name: "Cora Lindqvist", position: 0, role: null }],
        removed_author_orphaned: false,
      },
    });
    await click(r, r.getByLabelText("Remove Ada Quill"));
    act(() => {
      fireEvent.change(r.getByPlaceholderText("Search authors in this library…"), { target: { value: "Cora" } });
    });
    await r.settle();
    await click(r, button(r, "Cora Lindqvist"));
    await click(r, button(r, "Replace author"));
    expect(r.unmatched).toEqual([]);
    expect(r.requests).toContain(
      "DELETE /discovery/books/102/contributors/11?slug=calibre-library&replacement_author_id=14",
    );
    expect(r.queryByText("Replace sole author")).toBeNull();
    expect(toasts).toContain("success: Removed Ada Quill from this book");
  });

  it("applying a series suggestion", async () => {
    const changed = vi.fn();
    window.addEventListener(EVT.SuggestionsChanged, changed);
    const { r, onEdit } = await sidebar(ownedWithSuggestion, {
      ...sidebarOwnedRoutes,
      "POST /discovery/series-suggestions/12/apply": { ok: true },
    });
    await click(r, button(r, "Apply"));
    window.removeEventListener(EVT.SuggestionsChanged, changed);
    expect(r.unmatched).toEqual([]);
    expect(r.requests).toContain("POST /discovery/series-suggestions/12/apply");
    expect(changed).toHaveBeenCalled();
    expect(r.queryByText("Series Suggestion")).toBeNull();
    expect(onEdit).toHaveBeenCalled();
  });
});
