import { afterEach, describe, it } from "vitest";
import ReviewPage from "../../pages/ReviewPage";
import { expectRendered, renderPage, resetPageEnv, type Viewport } from "../render";
import { reviewPendingOnly, reviewWithImportFailure } from "../fixtures/review";

afterEach(resetPageEnv);

describe.each<Viewport>(["desktop", "phone"])("Review page (%s)", (viewport) => {
  it("pending reviews", async () => {
    const r = await renderPage(<ReviewPage />, { viewport, routes: reviewPendingOnly });
    expectRendered(r, "Saltwind", "The Glass Orchard");
  });

  it("with an import failure", async () => {
    const r = await renderPage(<ReviewPage />, { viewport, routes: reviewWithImportFailure });
    expectRendered(r, "Ninefold Tide", "Saltwind");
  });
});
