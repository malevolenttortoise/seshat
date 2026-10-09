import { afterEach, describe, it, vi } from "vitest";
import { BookSidebar } from "../../components/BookSidebar";
import { expectRendered, renderPage, resetPageEnv, type Viewport } from "../render";
import {
  missingOnMam,
  ownedWithSuggestion,
  sidebarMissingRoutes,
  sidebarOwnedRoutes,
} from "../fixtures/bookSidebar";

afterEach(resetPageEnv);

describe.each<Viewport>(["desktop", "phone"])("Book sidebar (%s)", (viewport) => {
  it("a missing book MAM has", async () => {
    const r = await renderPage(
      <BookSidebar book={missingOnMam} closing={false} onClose={vi.fn()} onAction={vi.fn()} onEdit={vi.fn()} />,
      { viewport, routes: sidebarMissingRoutes },
    );
    expectRendered(r, "The Glass Orchard", "Use wedge");
  });

  it("an owned book with a series suggestion", async () => {
    const r = await renderPage(
      <BookSidebar book={ownedWithSuggestion} closing={false} onClose={vi.fn()} onAction={vi.fn()} onEdit={vi.fn()} />,
      { viewport, routes: sidebarOwnedRoutes },
    );
    expectRendered(r, "Ninefold Tide", "Tidewater");
  });
});
