import { afterEach, describe, it, vi } from "vitest";
import { GlobalMetadataCacheStatusIcon } from "../../components/GlobalMetadataCacheStatusIcon";
import { expectRendered, renderPage, resetPageEnv } from "../render";
import { cacheStatusCooldownRoutes, cacheStatusRoutes } from "../fixtures/cacheStatus";

afterEach(resetPageEnv);

// The navbar's cloud icon (desktop nav only): worst-of-both health.
describe("Navbar cache-worker icon (desktop)", () => {
  it("both workers active", async () => {
    const r = await renderPage(<GlobalMetadataCacheStatusIcon onClick={vi.fn()} />, {
      viewport: "desktop",
      routes: cacheStatusRoutes,
    });
    expectRendered(r, "☁");
  });

  it("Goodreads in cooldown", async () => {
    const r = await renderPage(<GlobalMetadataCacheStatusIcon onClick={vi.fn()} />, {
      viewport: "desktop",
      routes: cacheStatusCooldownRoutes,
    });
    expectRendered(r, "☁");
  });
});
