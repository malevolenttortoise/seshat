import { afterEach, describe, it } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import ImportExportPage from "../../pages/DiscImportExportPage";
import { expectRendered, renderPage, resetPageEnv, type Viewport } from "../render";
import { IMPORT_URLS, importRoutes } from "../fixtures/importExport";

afterEach(resetPageEnv);

describe.each<Viewport>(["desktop", "phone"])("Import / Export page (%s)", (viewport) => {
  it("empty", async () => {
    const r = await renderPage(<ImportExportPage />, { viewport, routes: importRoutes });
    expectRendered(r, "Import");
  });

  it("a fetched preview", async () => {
    const r = await renderPage(<ImportExportPage />, { viewport, routes: importRoutes });
    act(() => { fireEvent.change(r.container.querySelector("textarea")!, { target: { value: IMPORT_URLS } }); });
    const fetchBtn = r.getAllByRole("button").find((b) => /^(Fetch & Preview|Preview)$/.test((b.textContent ?? "").trim()))!;
    act(() => { fireEvent.click(fetchBtn); });
    await r.settle();
    expectRendered(r, "Rootbound", "Couldn't read the page");
  });
});
