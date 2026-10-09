import { afterEach, describe, it, vi } from "vitest";
import AuthorDetailPage from "../../pages/DiscAuthorDetailPage";
import { expectRendered, renderPage, resetPageEnv, type Viewport } from "../render";
import { authorDetailRoutes, authorDetailScanningRoutes } from "../fixtures/authorDetail";

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
});
