import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import AuthorDetailPage from "../../pages/DiscAuthorDetailPage";
import { expectRendered, renderPage, resetPageEnv, type Viewport } from "../render";
import {
  authorDetailFailedRoutes,
  authorDetailRoutes,
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
