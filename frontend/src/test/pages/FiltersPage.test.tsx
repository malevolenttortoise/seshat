import { afterEach, describe, it } from "vitest";
import FiltersPage from "../../pages/FiltersPage";
import { expectRendered, renderPage, resetPageEnv, type Viewport } from "../render";
import { filtersRoutes } from "../fixtures/filters";

afterEach(resetPageEnv);

describe.each<Viewport>(["desktop", "phone"])("Filters page (%s)", (viewport) => {
  it("categories, formats and languages with allow / exclude set", async () => {
    const r = await renderPage(<FiltersPage />, { viewport, routes: filtersRoutes });
    expectRendered(r, "Science Fiction", "Mystery");
  });
});
