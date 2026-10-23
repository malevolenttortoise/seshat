// Manual Grab fixtures: two torrent IDs carried in from the Import page,
// both paid (not freeleech) and wedge-eligible, plus the wedge balance.
import type { FetchCall } from "../render";
import { economyConfig } from "./economy";

const rows: Record<string, Record<string, unknown>> = {
  "900021": {
    title: "The Glass Orchard", authors: ["Ada Quill"], narrators: [],
    series: [{ name: "Orchard Cycle", index: "2" }], category: "Ebooks - Fantasy", filetype: "epub",
    size_bytes: 2_400_000, seeders: 31,
  },
  "900022": {
    title: "Saltwind", authors: ["Bram Okafor"], narrators: ["Rhea Vance"],
    series: [], category: "Audiobooks - Science Fiction", filetype: "m4b",
    size_bytes: 612_000_000, seeders: 12,
  },
};

function preview(call: FetchCall) {
  const input = String((call.body as { value?: string })?.value ?? "");
  const r = rows[input];
  return {
    kind: "link",
    input,
    status: "ready",
    message: "",
    torrent_id: input,
    vip: false,
    freeleech: false,
    personal_freeleech: false,
    my_snatched: false,
    owned_in: [],
    in_flight: false,
    policy_tier: "normal",
    policy_grab: true,
    wedge_eligible: true,
    grab_id: null,
    cover_url: null,
    info_hash: null,
    ...r,
  };
}

export const MANUAL_GRAB_CARRIED = "900021\n900022";

export const manualGrabRoutes = {
  "GET /v1/grabs/budget": { budget_used: 38, budget_cap: 200 },
  "GET /v1/mam/economy/config": economyConfig,
  "POST /v1/manual-grab/preview": preview,
  // One spendable wedge for two eligible rows: "short of wedges".
  "GET /v1/manual-grab/wedges": { wedges: 3, reserved: 2, spendable: 1 },
};
