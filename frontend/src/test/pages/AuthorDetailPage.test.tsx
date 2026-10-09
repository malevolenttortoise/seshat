import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import AuthorDetailPage from "../../pages/DiscAuthorDetailPage";
import { expectRendered, renderPage, resetPageEnv, type PageRender, type Viewport } from "../render";
import {
  authorDetailFailedRoutes,
  authorDetailRoutes,
  authorDetailScanStartRoutes,
  authorDetailScanningRoutes,
  authorDetailUnlinkRoutes,
} from "../fixtures/authorDetail";

afterEach(resetPageEnv);

describe.each<Viewport>(["desktop", "phone"])("Author detail (%s)", (viewport) => {
  it("an author with series, standalones and a second library", async () => {
    const r = await renderPage(<AuthorDetailPage authorId="calibre-library:11" onNav={vi.fn()} />, {
      viewport,
      routes: authorDetailRoutes,
    });
    expectRendered(r, "Ada Quill", "Orchard Cycle");
  });

  it("with pen-name links while a source scan runs", async () => {
    const r = await renderPage(<AuthorDetailPage authorId="calibre-library:11" onNav={vi.fn()} />, {
      viewport,
      routes: authorDetailScanningRoutes,
    });
    expectRendered(r, "Ada Quill", viewport === "desktop" ? "A. Q. Thorne" : "Pen names");
  });

  it("when the author fails to load", async () => {
    const r = await renderPage(<AuthorDetailPage authorId="calibre-library:11" onNav={vi.fn()} />, {
      viewport,
      routes: authorDetailFailedRoutes,
    });
    expectRendered(r, "Couldn't load this author: database is locked");
  });
});

const buttonsNamed = (r: PageRender, name: string) =>
  r.getAllByRole("button").filter((b) => (b.textContent ?? "").trim() === name);

async function press(r: PageRender, el: Element) {
  act(() => { fireEvent.click(el); });
  await r.settle();
}

// Wave 5b S17 baselines: the states the bulk actions and the scan
// buttons decide, on the code before they move into hooks.
describe.each<Viewport>(["desktop", "phone"])("Author detail S17 states (%s)", (viewport) => {
  it("select mode with the standalones of both libraries picked", async () => {
    const r = await renderPage(<AuthorDetailPage authorId="calibre-library:11" onNav={vi.fn()} />, {
      viewport,
      routes: authorDetailRoutes,
    });
    await press(r, buttonsNamed(r, "Select")[0]);
    const picks = buttonsNamed(r, viewport === "desktop" ? "Select standalone" : "Select");
    expect(picks.length).toBeGreaterThan(1);
    for (const b of picks) await press(r, b);
    expectRendered(r, "Ada Quill", "Skip MAM");
  });

  it("right after starting a source scan and a MAM scan", async () => {
    const r = await renderPage(<AuthorDetailPage authorId="calibre-library:11" onNav={vi.fn()} />, {
      viewport,
      routes: authorDetailScanStartRoutes,
    });
    await press(r, buttonsNamed(r, viewport === "desktop" ? "Re-sync" : "Re-scan sources")[0]);
    await press(r, buttonsNamed(r, "Scan MAM")[0]);
    expectRendered(r, "Ada Quill");
  });
});

describe.each<Viewport>(["desktop", "phone"])("Author detail pen-name unlink (%s)", (viewport) => {
  it("deletes the link by the route the backend has, then reloads the list", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const r = await renderPage(<AuthorDetailPage authorId="calibre-library:11" onNav={vi.fn()} />, {
      viewport,
      routes: authorDetailUnlinkRoutes,
    });
    if (viewport === "desktop") {
      act(() => { fireEvent.click(r.getByText("×")); });
    } else {
      act(() => { fireEvent.click(r.getByText("Pen names")); });
      act(() => { fireEvent.click(r.getByText("Unlink")); });
    }
    await r.settle();
    expect(r.unmatched).toEqual([]);
    const del = r.requests.indexOf("DELETE /discovery/authors/pen-name-link/3");
    expect(del).toBeGreaterThan(-1);
    expect(r.requests.slice(del + 1)).toContain("GET /discovery/authors/11/pen-names");
  });
});
