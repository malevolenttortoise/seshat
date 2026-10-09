// Whether MAM is configured and on (`GET /discovery/mam/status` →
// `enabled`), read once on mount; false until it answers or if it fails.
//
// The author-detail and Books pages (desktop + phone) and BookSidebar
// each carried this one-shot fetch (wave 5b S7).
import { useEffect, useState } from "react";
import { api } from "../api";
import type { MamStatusResponse } from "../types";

export function useMamEnabled(): boolean {
  const [mamOn, setMamOn] = useState(false);
  useEffect(() => {
    api
      .get<MamStatusResponse>("/discovery/mam/status")
      .then((r) => setMamOn(!!r.enabled))
      .catch(() => {});
  }, []);
  return mamOn;
}
