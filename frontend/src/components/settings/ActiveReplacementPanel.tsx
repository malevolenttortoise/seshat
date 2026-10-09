// Settings → Active Replacement: per-library safety, auto-enact, opportunity counts.
// Split out of SettingsPage (wave 5b S16, issue 23).
import { useEffect, useState } from "react";
import { api } from "../../api";
import { useTheme } from "../../theme";
import type { SettingsDraft as S } from "../../hooks/useSettingsDraft";
import { useNavigation } from "../../providers/NavigationProvider";
import { Btn } from "../Btn";
import { SF, STog } from "./fields";

// ── v2.26.0 Active Replacement panel (Bundle A.2) ────────────

interface LibrarySafetyRow {
  slug: string;
  name: string;
  content_type: string;
  library_path: string;
  safety: "safe" | "overlap" | "unknown";
  enabled: boolean;
  effective: boolean;
  auto_enact: boolean;
  auto_enact_effective: boolean;
}

interface OpportunityCounts {
  detected: number;
  enacted: number;
  dismissed: number;
}

export function ActiveReplacementPanel({ s, upd }: {
  s: S; upd: (k: string, v: unknown) => void;
}) {
  const t = useTheme();
  const { nav } = useNavigation();
  const [safety, setSafety] = useState<LibrarySafetyRow[]>([]);
  const [counts, setCounts] = useState<OpportunityCounts | null>(null);

  const refresh = async () => {
    try {
      const [sf, cn] = await Promise.all([
        api.get<{ libraries: LibrarySafetyRow[] }>("/quality/library-safety"),
        api.get<OpportunityCounts>("/quality/replacement-opportunities/counts"),
      ]);
      setSafety(sf.libraries || []);
      setCounts(cn);
    } catch {
      /* swallow — empty state renders cleanly */
    }
  };

  useEffect(() => { refresh(); }, []);
  // Re-fetch when the user toggles a library so the `effective` badge
  // updates without a save round-trip. (The safety endpoint reads from
  // the persisted settings, so it won't actually reflect the toggle
  // until save() flushes — but counts + safety-class itself is stable.)
  useEffect(() => { refresh(); }, [s.active_replacement_enabled_by_slug]);

  const enabledMap =
    (s.active_replacement_enabled_by_slug as Record<string, boolean>) || {};
  const autoEnactMap =
    (s.active_replacement_auto_enact_by_slug as Record<string, boolean>) || {};

  // Track which library is mid-confirmation for the first-enable
  // auto_enact modal. null when no modal is open.
  const [autoEnactConfirm, setAutoEnactConfirm] = useState<string | null>(null);

  const toggle = (slug: string) => {
    const next = { ...enabledMap, [slug]: !enabledMap[slug] };
    upd("active_replacement_enabled_by_slug", next);
    // If we're turning the master OFF, also unset auto_enact for that
    // library — otherwise the bool sits orphaned in settings and could
    // re-activate if the operator flips master back on later. The
    // gate would still block it (compound check), but the UI shouldn't
    // show a stale `auto_enact: true` for a master-off library.
    if (!next[slug] && autoEnactMap[slug]) {
      const nextAuto = { ...autoEnactMap, [slug]: false };
      upd("active_replacement_auto_enact_by_slug", nextAuto);
    }
  };

  const toggleAutoEnact = (slug: string) => {
    const currentlyOn = !!autoEnactMap[slug];
    if (!currentlyOn) {
      // First-enable: open the confirmation modal. The actual flip
      // happens when the user confirms.
      setAutoEnactConfirm(slug);
      return;
    }
    // Disabling — no confirmation needed.
    const next = { ...autoEnactMap, [slug]: false };
    upd("active_replacement_auto_enact_by_slug", next);
  };

  const confirmAutoEnact = (slug: string) => {
    const next = { ...autoEnactMap, [slug]: true };
    upd("active_replacement_auto_enact_by_slug", next);
    setAutoEnactConfirm(null);
  };

  const badgeStyle = (safety: LibrarySafetyRow["safety"]): React.CSSProperties => {
    const palette: Record<typeof safety, { bg: string; fg: string; label: string }> = {
      safe:    { bg: t.bg3, fg: t.ok,   label: "SAFE" },
      overlap: { bg: t.bg3, fg: t.err,  label: "OVERLAP" },
      unknown: { bg: t.bg3, fg: t.warn, label: "UNKNOWN" },
    };
    const p = palette[safety];
    return {
      background: p.bg, color: p.fg,
      fontSize: 10, fontWeight: 700, letterSpacing: "0.04em",
      padding: "2px 6px", borderRadius: 4,
    };
  };

  return (
    <>
      <SF
        label="Detected Opportunities"
        desc="When a higher-quality torrent matches an owned book in a replacement-enabled library, an opportunity is logged here. Click View to see the queue + per-row Enact / Restore actions."
        wide
      >
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <div style={{ fontSize: 13, color: t.text2 }}>
            {counts
              ? <><b>{counts.detected}</b> detected · {counts.dismissed} dismissed · {counts.enacted} enacted</>
              : "loading…"}
          </div>
          <Btn variant="ghost" onClick={() => nav("replacement-opportunities")}>
            View
          </Btn>
          <Btn variant="ghost" onClick={refresh}>Refresh</Btn>
        </div>
      </SF>

      <SF
        label="Replaced-file retention"
        desc={`Days the owned file stays in .seshat-replaced/ after an enact before the hygiene sweeper purges it. Restore must run within this window. Default 30.`}
      >
        <input
          type="number" min={1} max={3650}
          value={(s.active_replacement_soft_delete_retention_days as number) ?? 30}
          onChange={e => upd(
            "active_replacement_soft_delete_retention_days",
            Math.max(1, Math.min(3650, parseInt(e.target.value) || 30)),
          )}
          style={{
            background: t.bg3, border: `1px solid ${t.border}`,
            color: t.text, padding: "6px 10px", borderRadius: 4,
            fontSize: 13, width: 90,
          }}
        />
      </SF>

      <SF
        label="Per-library Safety + Opt-in"
        desc="Replacement is off by default for every library. The first toggle enables detection + manual Enact buttons; the second toggle (Auto-enact) makes each detected opportunity fire immediately without operator action. Libraries whose path overlaps qBit's download path are hard-disabled."
        wide
      >
        {safety.length === 0 && (
          <div style={{ fontSize: 12, color: t.textDim }}>
            No libraries discovered yet.
          </div>
        )}
        <div style={{ display: "flex", flexDirection: "column", gap: 6, width: "100%" }}>
          {safety.map(lib => {
            const overlap = lib.safety === "overlap";
            const masterOn = !!enabledMap[lib.slug];
            const autoEnactOn = !!autoEnactMap[lib.slug];
            return (
              <div
                key={lib.slug}
                style={{
                  display: "flex", alignItems: "center", gap: 10,
                  padding: "8px 12px", background: t.bg3, borderRadius: 6,
                }}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                    <span style={{ fontSize: 13, fontWeight: 600, color: t.text }}>
                      {lib.name}
                    </span>
                    <span style={badgeStyle(lib.safety)}>
                      {lib.safety === "safe" ? "SAFE" : lib.safety === "overlap" ? "OVERLAP" : "UNKNOWN"}
                    </span>
                    <span style={{ fontSize: 11, color: t.textDim }}>
                      {lib.content_type}
                    </span>
                    {autoEnactOn && masterOn && !overlap && (
                      <span style={{
                        fontSize: 10, fontWeight: 700, color: t.warn,
                        background: t.bg3, border: `1px solid ${t.warn}55`,
                        padding: "2px 6px", borderRadius: 4,
                        letterSpacing: "0.04em",
                      }}>
                        AUTO-ENACT
                      </span>
                    )}
                  </div>
                  <div style={{ fontSize: 11, color: t.textDim, marginTop: 2 }}>
                    <code>{lib.library_path || "(no path)"}</code>
                    {overlap && (
                      <> · <span style={{ color: t.err }}>overlaps qBit download path; opt-in disabled</span></>
                    )}
                    {lib.safety === "unknown" && masterOn && (
                      <> · <span style={{ color: t.warn }}>path can't be verified; user attestation required</span></>
                    )}
                  </div>
                </div>
                <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    <span style={{ fontSize: 11, color: t.textDim }}>Detect + Enact</span>
                    <STog
                      on={masterOn}
                      onToggle={() => toggle(lib.slug)}
                      disabled={overlap}
                    />
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    <span style={{
                      fontSize: 11,
                      color: (overlap || !masterOn) ? t.textDim : t.text2,
                    }}>
                      Auto-enact
                    </span>
                    <STog
                      on={autoEnactOn}
                      onToggle={() => toggleAutoEnact(lib.slug)}
                      disabled={overlap || !masterOn}
                    />
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </SF>

      {autoEnactConfirm && (() => {
        const lib = safety.find(l => l.slug === autoEnactConfirm);
        if (!lib) {
          // Edge case: library disappeared from safety list between
          // open + render. Silently close the modal.
          setAutoEnactConfirm(null);
          return null;
        }
        return (
          <AutoEnactConfirmModal
            library={lib}
            onCancel={() => setAutoEnactConfirm(null)}
            onConfirm={() => confirmAutoEnact(autoEnactConfirm)}
          />
        );
      })()}
    </>
  );
}


function AutoEnactConfirmModal({ library, onCancel, onConfirm }: {
  library: LibrarySafetyRow; onCancel: () => void; onConfirm: () => void;
}) {
  const t = useTheme();
  return (
    <div
      style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)",
        zIndex: 200, display: "flex", alignItems: "center",
        justifyContent: "center", animation: "fadeOverlay 0.2s ease-out",
      }}
      onClick={onCancel}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: t.bg2, border: `1px solid ${t.border}`,
          borderRadius: 12, padding: 24, width: 540, maxWidth: "90vw",
          display: "flex", flexDirection: "column", gap: 14,
        }}
      >
        <h2 style={{ fontSize: 18, fontWeight: 700, color: t.text, margin: 0 }}>
          Enable auto-enact for <code>{library.slug}</code>?
        </h2>
        <div style={{
          padding: "10px 12px", background: t.warn + "1a",
          border: `1px solid ${t.warn}55`, borderRadius: 6, fontSize: 13,
          color: t.text, lineHeight: 1.5,
        }}>
          <b>Auto-enact is destructive.</b> Every newly-detected opportunity
          for this library will immediately:
          <ul style={{ marginTop: 8, marginBottom: 0, paddingLeft: 20 }}>
            <li>Move the lower-quality owned file to <code>.seshat-replaced/&lt;timestamp&gt;/</code></li>
            <li>Delete the library row via <code>calibredb remove</code> (or CWA admin API on slim images)</li>
            <li>Flip the opportunity status to <b>enacted</b></li>
          </ul>
          <br />
          Soft-deleted files are kept for the configured retention window
          (30 days by default). You can click <b>Restore</b> on any enacted
          row to reverse it within that window.
        </div>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <Btn variant="ghost" onClick={onCancel}>Cancel</Btn>
          <Btn onClick={onConfirm}>Enable auto-enact</Btn>
        </div>
      </div>
    </div>
  );
}
