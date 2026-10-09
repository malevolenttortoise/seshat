import { afterEach, describe, it, vi } from "vitest";
import MAMPage from "../../pages/DiscMAMPage";
import { expectRendered, renderPage, resetPageEnv, type Viewport } from "../render";
import { mamPageRoutes, mamPageScanningRoutes } from "../fixtures/mamPage";

afterEach(resetPageEnv);

describe.each<Viewport>(["desktop", "phone"])("Discovery MAM page (%s)", (viewport) => {
  it("books MAM has, both libraries", async () => {
    const r = await renderPage(<MAMPage onNav={vi.fn()} />, { viewport, routes: mamPageRoutes });
    expectRendered(r, "The Glass Orchard");
  });

  it("while a MAM scan runs", async () => {
    const r = await renderPage(<MAMPage onNav={vi.fn()} />, { viewport, routes: mamPageScanningRoutes });
    expectRendered(r, "The Glass Orchard");
  });
});
