import { afterEach, describe, it } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import { MetadataSourcesPanel } from "../../components/MetadataSourcesPanel";
import { expectRendered, renderPage, resetPageEnv, type PageRender } from "../render";
import { metadataSourcesCooldownRoutes, metadataSourcesRoutes } from "../fixtures/metadataSources";

afterEach(resetPageEnv);

async function open(r: PageRender, source: string): Promise<void> {
  // The source list's row label; the panel shows one source's details.
  const row = r.getAllByText(source).find((el) => el.closest("[style*='grid']"));
  act(() => { fireEvent.click(row ?? r.getAllByText(source)[0]); });
  await r.settle();
}

// The panel on Settings → Metadata Sources: per-source config, the
// traffic card, and the Amazon / Goodreads cache cards the wave-4
// trial is watched on.
describe("Metadata Sources panel (desktop)", () => {
  it("Amazon's details with its cache card", async () => {
    const r = await renderPage(<MetadataSourcesPanel />, { viewport: "desktop", routes: metadataSourcesRoutes });
    await open(r, "Amazon");
    expectRendered(r, "Amazon");
  });

  it("Goodreads' details with its cache card", async () => {
    const r = await renderPage(<MetadataSourcesPanel />, { viewport: "desktop", routes: metadataSourcesRoutes });
    await open(r, "Goodreads");
    expectRendered(r, "Goodreads");
  });

  it("Goodreads' cache card in cooldown", async () => {
    const r = await renderPage(<MetadataSourcesPanel />, { viewport: "desktop", routes: metadataSourcesCooldownRoutes });
    await open(r, "Goodreads");
    expectRendered(r, "Goodreads");
  });
});
