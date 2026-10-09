import { afterEach, describe, it } from "vitest";
import BooksPage from "../../pages/DiscBooksPage";
import { expectRendered, renderPage, resetPageEnv, type Viewport } from "../render";
import { booksRoutes } from "../fixtures/books";

afterEach(resetPageEnv);

// As App.tsx mounts it for Missing and Hidden Books.
describe.each<Viewport>(["desktop", "phone"])("Books page (%s)", (viewport) => {
  it("Missing", async () => {
    const r = await renderPage(<BooksPage title="Missing" apiPath="/discovery/missing" />, { viewport, routes: booksRoutes });
    expectRendered(r, "The Glass Orchard", "Rootbound");
  });

  it("Hidden Books with the owned filter", async () => {
    const r = await renderPage(
      <BooksPage title="Hidden Books" apiPath="/discovery/books/hidden" showOwnedFilter />,
      { viewport, routes: booksRoutes },
    );
    expectRendered(r, "Ninefold Tide");
  });
});
