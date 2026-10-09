import { afterEach, describe, it } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import TentativePage from "../../pages/TentativePage";
import { expectRendered, renderPage, resetPageEnv, type Viewport } from "../render";
import { tentativeRoutes } from "../fixtures/tentative";

afterEach(resetPageEnv);

describe.each<Viewport>(["desktop", "phone"])("Tentative page (%s)", (viewport) => {
  it("two held announces", async () => {
    const r = await renderPage(<TentativePage />, { viewport, routes: tentativeRoutes });
    expectRendered(r, "The Glass Orchard", "Saltwind");
  });

  it("in select mode with books ticked", async () => {
    const r = await renderPage(<TentativePage />, { viewport, routes: tentativeRoutes });
    if (viewport === "desktop") {
      act(() => { fireEvent.click(r.getByText("Select…")); });
      act(() => { fireEvent.click(r.getAllByRole("checkbox")[0]); });
    } else {
      act(() => { fireEvent.click(r.getByText("Select")); });
      act(() => { fireEvent.click(r.getByText("Select all")); });
    }
    await r.settle();
    expectRendered(r, viewport === "desktop" ? "1 selected" : "Selecting (2)", "Saltwind");
  });
});
