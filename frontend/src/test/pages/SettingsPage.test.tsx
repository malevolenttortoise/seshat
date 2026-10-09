import { afterEach, describe, it } from "vitest";
import SettingsPage from "../../pages/SettingsPage";
import { expectRendered, renderPage, resetPageEnv, type Viewport } from "../render";
import { settingsRoutes } from "../fixtures/settingsPage";

afterEach(resetPageEnv);

// Settings sections start collapsed, so this covers the page shell;
// the cache cards (wave 5b S5) have their own test on the panel, and
// issue 23's Settings split adds expanded-section baselines first.
describe.each<Viewport>(["desktop", "phone"])("Settings (%s)", (viewport) => {
  it("a configured install", async () => {
    const r = await renderPage(<SettingsPage />, { viewport, routes: settingsRoutes });
    expectRendered(r, viewport === "desktop" ? "Save" : "Settings");
  });
});
