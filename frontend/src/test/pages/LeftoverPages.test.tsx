// Wave 5b S18 baselines (G156): the pages that still carried their own
// copy of the book sidebar's open / close state or the "is MAM on"
// fetch. Taken before they move onto useBookSidebar / useMamEnabled.
// (Settings' discovery-data section is in SettingsPage.test.tsx's Data
// Management snapshot.)
import { afterEach, describe, it, vi } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import DiscSeriesDetailPage from "../../pages/DiscSeriesDetailPage";
import HiddenPage from "../../pages/DiscHiddenPage";
import AuthorsPage from "../../pages/DiscAuthorsPage";
import { expectRendered, renderPage, resetPageEnv, type PageRender, type Viewport } from "../render";
import {
  authorsRoutes,
  hiddenRoutes,
  hiddenSidebarRoutes,
  seriesDetailRoutes,
  seriesDetailSidebarRoutes,
} from "../fixtures/leftoverPages";
import { EBOOK_SLUG } from "../fixtures/common";

afterEach(resetPageEnv);

async function openBook(r: PageRender, title: string) {
  act(() => { fireEvent.click(r.getAllByText(title)[0]); });
  await r.settle();
}

describe.each<Viewport>(["desktop", "phone"])("Series detail (%s)", (viewport) => {
  it("a series with an incidental contributor", async () => {
    const r = await renderPage(<DiscSeriesDetailPage seriesId={`${EBOOK_SLUG}:7`} onNav={vi.fn()} />, {
      viewport,
      routes: seriesDetailRoutes,
    });
    expectRendered(r, "Orchard Cycle", "Bram Okafor");
  });

  it("with a book's sidebar open", async () => {
    const r = await renderPage(<DiscSeriesDetailPage seriesId={`${EBOOK_SLUG}:7`} onNav={vi.fn()} />, {
      viewport,
      routes: seriesDetailSidebarRoutes,
    });
    await openBook(r, "The Glass Orchard");
    expectRendered(r, "Use wedge");
  });
});

describe.each<Viewport>(["desktop", "phone"])("Hidden (%s)", (viewport) => {
  it("two hidden books", async () => {
    const r = await renderPage(<HiddenPage onNav={vi.fn()} />, { viewport, routes: hiddenRoutes });
    expectRendered(r, "The Winter Graft", "A Ledger of Bees");
  });

  it("with a book's sidebar open", async () => {
    const r = await renderPage(<HiddenPage onNav={vi.fn()} />, { viewport, routes: hiddenSidebarRoutes });
    await openBook(r, "The Winter Graft");
    expectRendered(r, "Unhide");
  });
});

describe.each<Viewport>(["desktop", "phone"])("Authors (%s)", (viewport) => {
  it("two authors, MAM on", async () => {
    const r = await renderPage(<AuthorsPage onNav={vi.fn()} />, { viewport, routes: authorsRoutes });
    expectRendered(r, "Ada Quill", "Bram Okafor");
  });
});
