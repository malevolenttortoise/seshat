import { afterEach, describe, it } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import SettingsPage from "../../pages/SettingsPage";
import { expectRendered, renderPage, resetPageEnv, type PageRender, type Viewport } from "../render";
import { settingsRoutes, settingsSectionRoutes } from "../fixtures/settingsPage";

afterEach(resetPageEnv);

async function click(r: PageRender, el: Element) {
  act(() => { fireEvent.click(el); });
  await r.settle();
}

// Desktop shows one section at a time (Pipeline first) and the phone
// starts with every section collapsed, so the page-shell test covers
// little of either; the per-section ones below are wave 5b S16's
// baselines for splitting the page.
describe.each<Viewport>(["desktop", "phone"])("Settings (%s)", (viewport) => {
  it("a configured install", async () => {
    const r = await renderPage(<SettingsPage />, { viewport, routes: settingsRoutes });
    expectRendered(r, viewport === "desktop" ? "Save" : "Settings");
  });
});

// The sidebar's sections after Pipeline (the default, above).
const DESKTOP_SECTIONS = [
  "Review & Enrichment", "Grab Policy", "Snatch Budget", "MyAnonamouse", "Download Client",
  "Sinks & Delivery", "Notifications", "Quality Metadata", "Active Replacement", "Metadata Sources",
  "Author Scanning", "Library Management", "Audiobookshelf", "Discovery MAM", "Operational",
  "Data Management",
];

describe("Settings sections (desktop)", () => {
  it.each(DESKTOP_SECTIONS)("%s", async (label) => {
    const r = await renderPage(<SettingsPage />, { viewport: "desktop", routes: settingsSectionRoutes });
    // The sidebar item (the group headers share a name with some, and
    // aren't clickable).
    const item = r.getAllByText(label, { exact: true }).find((el) => (el as HTMLElement).style.cursor === "pointer")!;
    await click(r, item);
    expectRendered(r, label);
  });
});

describe("Settings sections (phone)", () => {
  it("every section open", async () => {
    const r = await renderPage(<SettingsPage />, { viewport: "phone", routes: settingsSectionRoutes });
    const headers = Array.from(r.container.querySelectorAll("section > header"));
    for (const h of headers) await click(r, h);
    expectRendered(r, "Settings");
  }, 30_000);
});
