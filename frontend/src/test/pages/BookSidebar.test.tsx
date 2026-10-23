import { afterEach, describe, it, vi } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import { BookSidebar } from "../../components/BookSidebar";
import type { Book } from "../../types";
import { expectRendered, renderPage, resetPageEnv, type PageRender, type Routes, type Viewport } from "../render";
import {
  coauthored,
  compareNinefold,
  missingOnMam,
  ownedWithSuggestion,
  preflightShort,
  reingestNotFound,
  reingestTwoCandidates,
  sidebarCoauthoredRoutes,
  sidebarMissingRoutes,
  sidebarOwnedRoutes,
  sidebarSnatchedRoutes,
  snatchedOnMam,
} from "../fixtures/bookSidebar";

afterEach(resetPageEnv);

function sidebar(book: Book, viewport: Viewport, routes: Routes) {
  return renderPage(
    <BookSidebar book={book} closing={false} onClose={vi.fn()} onAction={vi.fn()} onEdit={vi.fn()} />,
    { viewport, routes },
  );
}

async function click(r: PageRender, el: Element) {
  act(() => { fireEvent.click(el); });
  await r.settle();
}

const button = (r: PageRender, text: string) => r.getByText(text, { exact: false }).closest("button")!;

describe.each<Viewport>(["desktop", "phone"])("Book sidebar (%s)", (viewport) => {
  it("a missing book MAM has", async () => {
    const r = await sidebar(missingOnMam, viewport, sidebarMissingRoutes);
    expectRendered(r, "The Glass Orchard", "Use wedge");
  });

  it("an owned book with a series suggestion", async () => {
    const r = await sidebar(ownedWithSuggestion, viewport, sidebarOwnedRoutes);
    expectRendered(r, "Ninefold Tide", "Tidewater");
  });

  it("editing a book", async () => {
    const r = await sidebar(ownedWithSuggestion, viewport, sidebarOwnedRoutes);
    await click(r, r.container.querySelector(".sb-actions button")!);
    expectRendered(r, "Source URLs", "Paste a MAM torrent URL");
  });

  it("comparing with Calibre", async () => {
    const routes = { ...sidebarOwnedRoutes, "GET /discovery/books/104/compare": compareNinefold };
    const r = await sidebar(ownedWithSuggestion, viewport, routes);
    await click(r, button(r, "Compare"));
    expectRendered(r, "Lantern Press");
  });

  it("send to pipeline stopped by the buffer gate", async () => {
    const routes = { ...sidebarMissingRoutes, "POST /v1/mam/economy/preflight": preflightShort };
    const r = await sidebar(missingOnMam, viewport, routes);
    await click(r, button(r, "Send to pipeline"));
    expectRendered(r, "not enough upload buffer");
  });

  it("reingest finds the snatch in two places", async () => {
    const routes = {
      ...sidebarSnatchedRoutes,
      "POST /discovery/books/106/reingest/probe": reingestTwoCandidates,
    };
    const r = await sidebar(snatchedOnMam, viewport, routes);
    await click(r, button(r, "Reingest from disk"));
    expectRendered(r, "Multiple matches found", "Orchard Cycle 1-3");
  });

  it("reingest finds nothing", async () => {
    const routes = { ...sidebarSnatchedRoutes, "POST /discovery/books/106/reingest/probe": reingestNotFound };
    const r = await sidebar(snatchedOnMam, viewport, routes);
    await click(r, button(r, "Reingest from disk"));
    expectRendered(r, "Could not find this snatch");
  });

  it("a book with two authors", async () => {
    const r = await sidebar(coauthored, viewport, sidebarCoauthoredRoutes);
    expectRendered(r, "Two Hands Make Rope", "Bram Okafor");
  });

  it("removing a sole author asks for a replacement", async () => {
    const r = await sidebar(missingOnMam, viewport, sidebarMissingRoutes);
    await click(r, r.getByLabelText("Remove Ada Quill"));
    expectRendered(r, "Replace sole author");
  });
});
