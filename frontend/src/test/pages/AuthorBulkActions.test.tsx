// The author page's bulk actions: the selection is split per library
// (book ids are per library), one request each, and the results come
// back as one confirm and one set of toasts. Wave 5b S17 moves this
// into a hook both shells share; this pins what it sends and says.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import AuthorDetailPage from "../../pages/DiscAuthorDetailPage";
import { EVT } from "../../types";
import { renderPage, resetPageEnv, type PageRender, type Viewport } from "../render";
import { authorDetailBulkRoutes } from "../fixtures/authorDetail";
import { AUDIO_SLUG, EBOOK_SLUG } from "../fixtures/common";

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

const buttonsNamed = (r: PageRender, name: string) =>
  r.getAllByRole("button").filter((b) => (b.textContent ?? "").trim() === name);

async function press(r: PageRender, el: Element) {
  act(() => { fireEvent.click(el); });
  await r.settle();
}

/** The ids a bulk request sent for one library (sorted). */
function sentIds(r: PageRender, kind: string, slug: string): number[] {
  const prefix = `POST /discovery/books/bulk-${kind}?slug=${slug} `;
  const req = r.requests.find((q) => q.startsWith(prefix));
  if (!req) throw new Error(`no ${prefix}`);
  return (JSON.parse(req.slice(prefix.length)).book_ids as number[]).sort((x, y) => x - y);
}

async function selectBothLibraries(r: PageRender, viewport: Viewport) {
  await press(r, buttonsNamed(r, "Select")[0]);
  for (const b of buttonsNamed(r, viewport === "desktop" ? "Select standalone" : "Select")) await press(r, b);
}

describe.each<Viewport>(["desktop", "phone"])("Author bulk actions (%s)", (viewport) => {
  it("hide sends one request per library and reports the total", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const r = await renderPage(<AuthorDetailPage authorId="calibre-library:11" onNav={vi.fn()} />, {
      viewport,
      routes: authorDetailBulkRoutes,
    });
    await selectBothLibraries(r, viewport);
    await press(r, buttonsNamed(r, "Hide")[0]);
    expect(r.unmatched).toEqual([]);
    const ebook = sentIds(r, "hide", EBOOK_SLUG);
    expect(ebook).toEqual(expect.arrayContaining([104, 105]));
    expect(sentIds(r, "hide", AUDIO_SLUG)).toEqual([301]);
    expect(confirm).toHaveBeenCalledWith(`Hide ${ebook.length + 1} book(s)?`);
    expect(toasts).toEqual(["success: Hidden 3 book(s)"]);
    // Select mode ends with the action.
    expect(buttonsNamed(r, "Select").length).toBeGreaterThan(0);
    expect(buttonsNamed(r, "Cancel")).toHaveLength(0);
  });

  it("delete names both synced kinds, and reports a library that failed", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const r = await renderPage(<AuthorDetailPage authorId="calibre-library:11" onNav={vi.fn()} />, {
      viewport,
      routes: authorDetailBulkRoutes,
    });
    await selectBothLibraries(r, viewport);
    await press(r, buttonsNamed(r, "Delete")[0]);
    expect(r.unmatched).toEqual([]);
    const picked = sentIds(r, "delete", EBOOK_SLUG).length + sentIds(r, "delete", AUDIO_SLUG).length;
    expect(confirm).toHaveBeenCalledWith(
      `Delete ${picked} book(s)? Calibre-synced / Audiobookshelf-synced books will be skipped.`,
    );
    expect(toasts).toEqual([
      "warn: Partial failure: 1 of 2 libraries errored. database is locked",
      "success: Deleted 1 book(s), skipped 1 Calibre-synced",
    ]);
  });

  it("nothing is sent when the confirm is declined", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const r = await renderPage(<AuthorDetailPage authorId="calibre-library:11" onNav={vi.fn()} />, {
      viewport,
      routes: authorDetailBulkRoutes,
    });
    await selectBothLibraries(r, viewport);
    await press(r, buttonsNamed(r, "Hide")[0]);
    expect(r.requests.filter((q) => q.includes("/bulk-"))).toEqual([]);
    expect(toasts).toEqual([]);
  });
});
