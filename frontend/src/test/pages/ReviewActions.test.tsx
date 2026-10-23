import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import ReviewPage from "../../pages/ReviewPage";
import { renderPage, resetPageEnv, type PageRender, type Viewport } from "../render";
import { reviewBulkPartialRoutes, reviewRedropFailRoutes } from "../fixtures/review";

afterEach(resetPageEnv);

const button = (r: PageRender, label: RegExp) =>
  r.getAllByRole("button").find((b) => label.test((b.textContent ?? "").trim()))!;

// Wave 5b S6a: an action's failure message stays on screen. The bulk
// result's partial failures were dropped by the phone and wiped by the
// list refresh on desktop; a failed Re-drop was wiped on both.
describe.each<Viewport>(["desktop", "phone"])("Review actions (%s)", (viewport) => {
  it("bulk approve shows a partial failure", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const r = await renderPage(<ReviewPage />, { viewport, routes: reviewBulkPartialRoutes });
    act(() => { fireEvent.click(button(r, /^Approve all/i)); });
    await r.settle();
    expect(r.unmatched).toEqual([]);
    expect(r.requests).toContain("POST /v1/review/bulk/approve");
    expect(r.getByText("Approved 1, 1 failed. First errors: Saltwind: sink unreachable", { exact: false })).toBeTruthy();
  });

  it("a failed Re-drop says why", async () => {
    const r = await renderPage(<ReviewPage />, { viewport, routes: reviewRedropFailRoutes });
    act(() => { fireEvent.click(button(r, /^Re-drop/i)); });
    await r.settle();
    expect(r.unmatched).toEqual([]);
    expect(r.requests).toContain("POST /v1/review/503/redrop");
    expect(r.getByText("Re-drop failed: CWA ingest folder not writable", { exact: false })).toBeTruthy();
  });
});
