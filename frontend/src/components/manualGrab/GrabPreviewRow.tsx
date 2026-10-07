// One row of a Manual Grab review list: the preview of a MAM torrent,
// the user's tick, the per-row personal-FL tick, the "Download again?"
// confirm for torrents MAM says you already snatched (D13), and the
// outcome once Grab all has run. Used by both shells (`compact` for
// mobile) and meant for reuse by Proactive Search.
import type { CSSProperties } from "react";
import { useTheme } from "../../theme";
import { fmtBytes } from "../../lib/format";
import { Spin } from "../Spin";
import { BLOCKING_STATUSES, isFree, type GrabEntry, type JobRowStatus } from "./types";

export interface GrabPreviewRowProps {
  entry: GrabEntry;
  compact?: boolean;
  onTick: (on: boolean) => void;
  onBuyFl: (on: boolean) => void;
  onConfirmSnatched: () => void;
  onCancelConfirm: () => void;
}

export function GrabPreviewRow({
  entry, compact, onTick, onBuyFl, onConfirmSnatched, onCancelConfirm,
}: GrabPreviewRowProps) {
  const t = useTheme();
  const p = entry.preview;
  const blocked = !!p && BLOCKING_STATUSES.has(p.status);
  const done = !!entry.result && entry.result.status !== "pending";
  const tickDisabled = !p || blocked || !!entry.result;
  const coverW = compact ? 40 : 48;

  const warnStatuses = ["owned", "in_flight_sibling", "snatched_on_mam", "policy_skip"];
  const statusColor = !p
    ? t.td
    : blocked
      ? t.redt
      : warnStatuses.includes(p.status)
        ? t.ylwt
        : t.td;

  const resultColor: Record<JobRowStatus, string> = {
    pending: t.td, working: t.td, submitted: t.grnt, queued: t.cyant,
    refused: t.ylwt, failed: t.redt,
  };
  const resultLabel: Record<JobRowStatus, string> = {
    pending: "Waiting", working: "Grabbing…", submitted: "Grabbed",
    queued: "Queued", refused: "Not grabbed", failed: "Failed",
  };

  const badge = (label: string, color: string): CSSProperties & { label: string } => ({
    label, fontSize: 10, fontWeight: 700, padding: "1px 6px", borderRadius: 4,
    color, border: `1px solid ${color}`, letterSpacing: 0.3,
  });
  const badges = p
    ? [
        p.vip && badge("VIP", t.pur),
        p.freeleech && badge("FL", t.grn),
        p.personal_freeleech && badge("PERSONAL FL", t.grn),
      ].filter(Boolean) as (CSSProperties & { label: string })[]
    : [];

  const meta = p
    ? [
        p.filetype && p.filetype.toUpperCase(),
        p.size_bytes != null && fmtBytes(p.size_bytes),
        p.seeders != null && `${p.seeders} seeders`,
        !compact && p.category,
      ].filter(Boolean).join(" · ")
    : "";

  const showFl = !!p && !blocked && !entry.result && !isFree(p);

  return (
    <div
      style={{
        display: "flex",
        gap: compact ? 10 : 12,
        padding: compact ? "10px 0" : "12px 4px",
        borderTop: `1px solid ${t.borderL}`,
        opacity: blocked ? 0.75 : 1,
      }}
    >
      <input
        type="checkbox"
        aria-label="Grab this torrent"
        checked={entry.ticked}
        disabled={tickDisabled}
        onChange={(e) => onTick(e.target.checked)}
        style={{ marginTop: 4, width: 18, height: 18, accentColor: t.accent, flexShrink: 0 }}
      />
      <div
        style={{
          width: coverW, height: Math.round(coverW * 1.45), flexShrink: 0,
          borderRadius: 4, background: t.bg4, overflow: "hidden",
          display: "flex", alignItems: "center", justifyContent: "center",
        }}
      >
        {!p ? (
          <Spin size={16} />
        ) : p.cover_url ? (
          <img src={p.cover_url} alt="" style={{ width: "100%", height: "100%", objectFit: "cover" }} />
        ) : (
          <span style={{ fontSize: 18, color: t.tg }}>📕</span>
        )}
      </div>

      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 3 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
          {p && p.torrent_id && p.title ? (
            <a
              href={`https://www.myanonamouse.net/t/${p.torrent_id}`}
              target="_blank"
              rel="noreferrer"
              style={{ fontSize: compact ? 14 : 15, fontWeight: 600, color: t.text, textDecoration: "none" }}
            >
              {p.title}
            </a>
          ) : (
            <span style={{ fontSize: 14, color: t.td, wordBreak: "break-all" }}>
              {p ? entry.input : `Looking up ${entry.input}…`}
            </span>
          )}
          {badges.map(({ label, ...style }) => (
            <span key={label} style={style}>{label}</span>
          ))}
        </div>

        {entry.kind === "file" && p && p.title && (
          <div style={{ fontSize: 11, color: t.tf, wordBreak: "break-all" }}>📎 {entry.input}</div>
        )}
        {p && p.authors.length > 0 && (
          <div style={{ fontSize: 13, color: t.text2 }}>
            {p.authors.join(", ")}
            {p.narrators.length > 0 && (
              <span style={{ color: t.td }}> · read by {p.narrators.join(", ")}</span>
            )}
          </div>
        )}
        {p && p.series.length > 0 && (
          <div style={{ fontSize: 12, color: t.td }}>
            {p.series.map((s) => (s.index ? `${s.name} #${s.index}` : s.name)).join(" · ")}
          </div>
        )}
        {meta && <div style={{ fontSize: 12, color: t.td }}>{meta}</div>}

        {p && p.status !== "ready" && p.message && !entry.result && (
          <div style={{ fontSize: 12, color: statusColor }}>
            {p.status === "snatched_on_mam" ? "⚠ " : ""}
            {p.message}
          </div>
        )}
        {p && p.status === "snatched_on_mam" && entry.kind === "link" && !entry.result && (
          <div style={{ fontSize: 12, color: t.td }}>
            Have the files? Use Reingest from disk. Have the .torrent? Drop it here instead.
          </div>
        )}

        {entry.confirming && (
          <div
            style={{
              marginTop: 4, padding: "8px 10px", borderRadius: 6,
              background: t.ylwb, border: `1px solid ${t.ylwt}`,
              display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8,
            }}
          >
            <span style={{ fontSize: 12, color: t.text, flex: "1 1 220px" }}>
              Download it from MAM again? MAM counts every download; this is a second one.
            </span>
            <button onClick={onConfirmSnatched} style={smallBtn(t.ylwt, t.bg)}>Download again</button>
            <button onClick={onCancelConfirm} style={smallBtn(t.bg4, t.text2, t.border)}>Cancel</button>
          </div>
        )}

        {showFl && (
          <label style={{ fontSize: 12, color: t.text2, display: "flex", alignItems: "center", gap: 6, marginTop: 2 }}>
            <input
              type="checkbox"
              checked={entry.buyFl}
              onChange={(e) => onBuyFl(e.target.checked)}
              style={{ accentColor: t.accent }}
            />
            Buy personal FL (50k BP)
          </label>
        )}

        {entry.result && (
          <div style={{ fontSize: 12, color: resultColor[entry.result.status], display: "flex", gap: 6, alignItems: "center" }}>
            {entry.result.status === "working" && <Spin size={12} />}
            <strong>{resultLabel[entry.result.status]}</strong>
            {done && entry.result.message && <span style={{ color: t.td }}>· {entry.result.message}</span>}
            {entry.result.personal_fl_bought && <span style={{ color: t.grnt }}>· personal FL bought</span>}
          </div>
        )}
      </div>
    </div>
  );
}

function smallBtn(bg: string, fg: string, border?: string): CSSProperties {
  return {
    padding: "4px 10px", fontSize: 12, fontWeight: 600, borderRadius: 5,
    background: bg, color: fg, border: `1px solid ${border ?? bg}`, cursor: "pointer",
  };
}
