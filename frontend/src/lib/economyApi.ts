// Typed wrappers over /api/v1/mam/economy/*.
//
// One file per feature area keeps the MamPage component legible
// (inline fetch calls drown out the JSX) and gives the BufferInsufficient
// banner + the BookSidebar checkboxes a single import site.

import { api } from "../api";

export interface EconomyConfig {
  mam_economy_vip_enabled: boolean;
  mam_economy_vip_interval_hours: number;
  mam_economy_vip_min_bonus: number;
  mam_economy_vip_weeks: number | "max";
  mam_economy_upload_enabled: boolean;
  mam_economy_upload_interval_hours: number;
  mam_economy_upload_ratio_trigger: boolean;
  mam_economy_upload_ratio_floor: number;
  mam_economy_upload_ratio_chunk_gb: number;
  mam_economy_upload_buffer_trigger: boolean;
  mam_economy_upload_buffer_floor_gb: number;
  mam_economy_upload_buffer_chunk_gb: number;
  mam_economy_upload_bonus_trigger: boolean;
  mam_economy_upload_bonus_ceiling: number;
  mam_economy_buffer_gate_enabled: boolean;
  mam_economy_buffer_gate_safety_margin_gb: number;
  mam_economy_manual_wedge_offer_enabled: boolean;
  mam_economy_fl_wedge_offer_enabled: boolean;
  mam_economy_intro_dismissed: boolean;
  mam_economy_dry_run: boolean;
  // Read-only timestamps.
  mam_economy_last_vip_buy_at: number;
  mam_economy_last_upload_buy_at: number;
}

export interface BuyResponse {
  ok: boolean;
  message: string;
  new_seedbonus: number | null;
  cost_points: number | null;
  amount: string | null;
}

export interface AuditRow {
  id: number;
  occurred_at: string;
  action: "vip" | "upload" | "personal_fl" | "buffer_gate_block" | "wedge";
  trigger: "scheduled" | "manual" | "irc_autograb" | "user_grab";
  outcome: string;
  mode: string | null;
  amount: string | null;
  torrent_id: string | null;
  tier: string | null;
  message: string | null;
  cost_points: number | null;
  user_bonus_after: number | null;
}

export interface PreflightResponse {
  size_gb: number;
  buffer_gb: number;
  safety_margin_gb: number;
  sufficient: boolean;
  shortfall_gb: number;
  recommended_buy_gb: number;
  recommended_buy_cost_bp: number;
}

const BASE = "/v1/mam/economy";

export const economyApi = {
  getConfig: () => api.get<EconomyConfig>(`${BASE}/config`),
  putConfig: (updates: Partial<EconomyConfig>) =>
    api.put<EconomyConfig>(`${BASE}/config`, updates),

  vipBuy: (weeks: number | "max") =>
    api.post<BuyResponse>(`${BASE}/vip/buy`, { weeks }),
  uploadBuy: (body: { gb: number } | { mode: "max_affordable" }) =>
    api.post<BuyResponse>(`${BASE}/upload/buy`, body),
  personalFlBuy: (torrent_id: string) =>
    api.post<BuyResponse>(`${BASE}/personal-fl/buy`, { torrent_id }),

  audit: (params: { limit?: number; action?: AuditRow["action"] } = {}) => {
    const q = new URLSearchParams();
    if (params.limit !== undefined) q.set("limit", String(params.limit));
    if (params.action) q.set("action", params.action);
    const qs = q.toString();
    return api.get<AuditRow[]>(`${BASE}/audit${qs ? `?${qs}` : ""}`);
  },

  preflight: (torrent_id: string) =>
    api.post<PreflightResponse>(`${BASE}/preflight`, { torrent_id }),
};

// ─── Formatters ───────────────────────────────────────────

export function formatBp(bp: number | null | undefined): string {
  if (bp === null || bp === undefined) return "—";
  return `${Math.round(bp).toLocaleString()} BP`;
}

export function formatOutcome(outcome: string): {
  label: string;
  tone: "ok" | "warn" | "err" | "dim";
} {
  if (outcome === "success") return { label: "Success", tone: "ok" };
  if (outcome === "failure") return { label: "Failed", tone: "err" };
  if (outcome === "buffer_gate_block")
    return { label: "Buffer blocked", tone: "warn" };
  if (outcome.startsWith("skip_"))
    return { label: outcome.replace("skip_", "Skipped: ").replace(/_/g, " "), tone: "dim" };
  return { label: outcome, tone: "dim" };
}

export function formatAction(action: AuditRow["action"]): string {
  switch (action) {
    case "vip":
      return "VIP";
    case "upload":
      return "Upload";
    case "personal_fl":
      return "Personal FL";
    case "buffer_gate_block":
      return "Buffer gate";
    case "wedge":
      return "Wedge";
  }
}

export function formatRelativeTime(iso: string): string {
  try {
    const dt = new Date(iso.replace(" ", "T") + "Z").getTime();
    const diff = (Date.now() - dt) / 1000;
    if (diff < 60) return `${Math.round(diff)}s ago`;
    if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
    if (diff < 86400) return `${(diff / 3600).toFixed(1)}h ago`;
    return `${(diff / 86400).toFixed(1)}d ago`;
  } catch {
    return iso;
  }
}
