import { afterEach, describe, it } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import GrabFromMamPage from "../../pages/GrabFromMamPage";
import { expectRendered, renderPage, reply, resetPageEnv, type PageRender, type Viewport } from "../render";
import { MANUAL_GRAB_CARRIED, manualGrabRoutes } from "../fixtures/manualGrab";
import { economyConfig } from "../fixtures/economy";

afterEach(resetPageEnv);

async function tickWedges(r: PageRender) {
  const toggle = r.getByText("aren't free", { exact: false }).closest("label")!.querySelector("input")!;
  act(() => { fireEvent.click(toggle); });
  await r.settle();
}

describe.each<Viewport>(["desktop", "phone"])("Manual Grab (%s)", (viewport) => {
  it("two carried links previewed", async () => {
    const r = await renderPage(<GrabFromMamPage initial={MANUAL_GRAB_CARRIED} />, { viewport, routes: manualGrabRoutes });
    expectRendered(r, "The Glass Orchard", "Saltwind");
  });

  it("wedges on, short of wedges", async () => {
    const r = await renderPage(<GrabFromMamPage initial={MANUAL_GRAB_CARRIED} />, { viewport, routes: manualGrabRoutes });
    await tickWedges(r);
    expectRendered(r, "Needs 2 wedges");
  });

  it("wedges on, enough to spend", async () => {
    const routes = { ...manualGrabRoutes, "GET /v1/manual-grab/wedges": { wedges: 6, reserved: 2, spendable: 4 } };
    const r = await renderPage(<GrabFromMamPage initial={MANUAL_GRAB_CARRIED} />, { viewport, routes });
    await tickWedges(r);
    expectRendered(r, "4 spendable");
  });

  it("wedge balance unreadable", async () => {
    const routes = { ...manualGrabRoutes, "GET /v1/manual-grab/wedges": reply(502, { detail: "MAM didn't answer" }) };
    const r = await renderPage(<GrabFromMamPage initial={MANUAL_GRAB_CARRIED} />, { viewport, routes });
    await tickWedges(r);
    expectRendered(r, "MAM didn't answer");
  });

  it("wedges switched off on the MAM page", async () => {
    const routes = {
      ...manualGrabRoutes,
      "GET /v1/mam/economy/config": { ...economyConfig, mam_economy_manual_wedge_offer_enabled: false },
    };
    const r = await renderPage(<GrabFromMamPage initial={MANUAL_GRAB_CARRIED} />, { viewport, routes });
    expectRendered(r, "Wedges are off for manual grabs");
  });
});
