import { afterEach, describe, it } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import GrabFromMamPage from "../../pages/GrabFromMamPage";
import { expectRendered, renderPage, resetPageEnv, type Viewport } from "../render";
import { MANUAL_GRAB_CARRIED, manualGrabRoutes } from "../fixtures/manualGrab";

afterEach(resetPageEnv);

describe.each<Viewport>(["desktop", "phone"])("Manual Grab (%s)", (viewport) => {
  it("two carried links previewed", async () => {
    const r = await renderPage(<GrabFromMamPage initial={MANUAL_GRAB_CARRIED} />, { viewport, routes: manualGrabRoutes });
    expectRendered(r, "The Glass Orchard", "Saltwind");
  });

  it("wedges on, short of wedges", async () => {
    const r = await renderPage(<GrabFromMamPage initial={MANUAL_GRAB_CARRIED} />, { viewport, routes: manualGrabRoutes });
    const toggle = r.getByText("aren't free", { exact: false }).closest("label")!.querySelector("input")!;
    act(() => { fireEvent.click(toggle); });
    await r.settle();
    expectRendered(r, "Needs 2 wedges");
  });
});
