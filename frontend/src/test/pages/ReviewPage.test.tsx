import { afterEach, describe, expect, it } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import ReviewPage from "../../pages/ReviewPage";
import { expectRendered, renderPage, resetPageEnv, type Viewport } from "../render";
import { reviewPendingOnly, reviewRedropFailRoutes, reviewWithImportFailure } from "../fixtures/review";

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

  // G155: an action's failure stays through the page's refreshes (the
  // 30s poll; here the catch-up refresh on returning to the tab).
  it("a failed Re-drop's message after the list refreshes", async () => {
    const r = await renderPage(<ReviewPage />, { viewport, routes: reviewRedropFailRoutes });
    const redrop = r.getAllByRole("button").find((b) => /^Re-drop/i.test((b.textContent ?? "").trim()))!;
    act(() => { fireEvent.click(redrop); });
    await r.settle();
    const lists = () => r.requests.filter((q) => q === "GET /v1/review").length;
    const before = lists();
    act(() => { document.dispatchEvent(new Event("visibilitychange")); });
    await r.settle();
    expect(lists()).toBeGreaterThan(before);
    expectRendered(r, "Re-drop failed: CWA ingest folder not writable");
  });
});
