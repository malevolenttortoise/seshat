import { afterEach, describe, it } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import LogsPage from "../../pages/LogsPage";
import { expectRendered, renderPage, resetPageEnv, type Viewport } from "../render";
import { logsRoutes } from "../fixtures/logs";

afterEach(resetPageEnv);

describe.each<Viewport>(["desktop", "phone"])("Logs page (%s)", (viewport) => {
  it("all lines", async () => {
    const r = await renderPage(<LogsPage />, { viewport, routes: logsRoutes });
    expectRendered(r, "book page blocked");
  });

  it("the Announces tab", async () => {
    const r = await renderPage(<LogsPage />, { viewport, routes: logsRoutes });
    act(() => { fireEvent.click(r.getAllByText("Announces")[0]); });
    await r.settle();
    expectRendered(r, "Red Lantern", "category_not_allowed");
  });
});
