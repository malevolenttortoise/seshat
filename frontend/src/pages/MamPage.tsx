// MamPage — MAM account snapshot and emergency cookie management.
//
// Stat cards for ratio / wedges / seedbonus / class, plus the
// validation status and an "emergency cookie paste" form for the
// case where Seshat's auto-rotation didn't catch a MAM-side
// expiry. The paste form posts the new cookie to /api/v1/mam/cookie
// which validates immediately and reports back yes/no.
//
// Status load is non-blocking: the GET endpoint never raises on a
// stale or missing cookie — it returns a payload with `error` set
// so we render an actionable banner instead of a generic crash.
import { useEffect, useState } from "react";
import { Btn } from "../components/Btn";
import { Section } from "../components/Section";
import { Spin } from "../components/Spin";
import { useVisibleInterval } from "../hooks/useVisibleInterval";
import { api } from "../api";
import { fmtBytes } from "../lib/format";
import {
  economyApi,
  formatAction,
  formatBp,
  formatOutcome,
  formatRelativeTime,
  type AuditRow,
  type EconomyConfig,
} from "../lib/economyApi";
import { toast } from "../lib/toast";
import { useTheme } from "../theme";

interface MamStatus {
  cookie_configured: boolean;
  cookie_age_seconds: number | null;
  last_validated_at: string | null;
  validation_ok: boolean;
  username: string | null;
  uid: number | null;
  classname: string | null;
  ratio: number | null;
  wedges: number | null;
  seedbonus: number | null;
  uploaded_bytes: number | null;
  downloaded_bytes: number | null;
  error: string | null;
}

interface ValidateResponse {
  ok: boolean;
  message: string;
}

function formatAge(seconds: number | null): string {
  if (seconds === null || seconds === undefined) return "never";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h ago`;
  return `${(seconds / 86400).toFixed(1)}d ago`;
}

export default function MamPage() {
  const theme = useTheme();
  const [status, setStatus] = useState<MamStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [cookieInput, setCookieInput] = useState("");

  async function load() {
    try {
      const r = await api.get<MamStatus>("/v1/mam/status");
      setStatus(r);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => { load(); }, []);
  useVisibleInterval(load, 60_000);

  async function refresh() {
    setBusy(true);
    setActionMessage(null);
    try {
      const r = await api.post<MamStatus>("/v1/mam/refresh");
      setStatus(r);
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function validate() {
    setBusy(true);
    setActionMessage(null);
    try {
      const r = await api.post<ValidateResponse>("/v1/mam/validate");
      setActionMessage(r.message || (r.ok ? "Validated." : "Validation failed."));
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function pasteCookie() {
    if (!cookieInput.trim()) return;
    setBusy(true);
    setActionMessage(null);
    try {
      const r = await api.post<ValidateResponse>(
        "/v1/mam/cookie",
        { cookie: cookieInput.trim() },
      );
      setActionMessage(
        r.ok
          ? "Cookie accepted and validated."
          : `Cookie saved but validation failed: ${r.message}`,
      );
      setCookieInput("");
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto" }}>
      <h1
        style={{
          fontSize: 24,
          fontWeight: 700,
          color: theme.text,
          marginBottom: 4,
        }}
      >
        MAM Status
      </h1>
      <p style={{ fontSize: 14, color: theme.textDim, marginBottom: 20 }}>
        Account snapshot and cookie health. Refresh to bypass the 5-minute
        cache; validate to re-run the IP registration + session probe.
      </p>

      {error && (
        <Banner tone="err">
          {error}
        </Banner>
      )}
      {actionMessage && (
        <Banner tone={status?.validation_ok ? "ok" : "warn"}>
          {actionMessage}
        </Banner>
      )}
      {status?.error && (
        <Banner tone="warn">
          {status.error}
        </Banner>
      )}

      {status === null ? (
        <div style={{ display: "flex", justifyContent: "center", padding: 40 }}>
          <Spin />
        </div>
      ) : (
        <>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
              gap: 12,
              marginBottom: 16,
            }}
          >
            <StatCard
              label="Ratio"
              value={status.ratio !== null ? status.ratio.toFixed(2) : "—"}
              tone={
                status.ratio === null
                  ? "dim"
                  : status.ratio >= 1
                    ? "ok"
                    : "warn"
              }
            />
            <StatCard
              label="Wedges"
              value={status.wedges ?? "—"}
            />
            <StatCard
              label="Seedbonus"
              value={
                status.seedbonus !== null
                  ? status.seedbonus.toLocaleString()
                  : "—"
              }
            />
            <StatCard
              label="Class"
              value={status.classname || "—"}
              tone="dim"
            />
          </div>

          <Section title="Account">
            <KV label="Username">{status.username || "—"}</KV>
            <KV label="User ID">{status.uid ?? "—"}</KV>
            <KV label="Uploaded">{fmtBytes(status.uploaded_bytes)}</KV>
            <KV label="Downloaded">{fmtBytes(status.downloaded_bytes)}</KV>
          </Section>

          <Section
            title="Cookie Health"
            subtitle="Seshat auto-rotates the MAM cookie on every API call. Validate to re-run the explicit check."
            right={
              <div style={{ display: "flex", gap: 8 }}>
                <Btn variant="ghost" onClick={refresh} disabled={busy}>
                  {busy ? <Spin size={14} /> : "Refresh"}
                </Btn>
                <Btn variant="primary" onClick={validate} disabled={busy}>
                  Validate
                </Btn>
              </div>
            }
          >
            <KV label="Configured">
              {status.cookie_configured ? (
                <Badge tone="ok">YES</Badge>
              ) : (
                <Badge tone="err">NO</Badge>
              )}
            </KV>
            <KV label="Last validated">
              {status.last_validated_at || "never"}
              {status.cookie_age_seconds !== null && (
                <span style={{ color: theme.textDim, marginLeft: 8 }}>
                  ({formatAge(status.cookie_age_seconds)})
                </span>
              )}
            </KV>
            <KV label="Validation OK">
              {status.validation_ok ? (
                <Badge tone="ok">YES</Badge>
              ) : (
                <Badge tone="warn">NO</Badge>
              )}
            </KV>
          </Section>

          <EconomySections />

          <Section
            title="Emergency Cookie Paste"
            subtitle="If MAM expired your session and auto-rotation didn't catch it, paste a fresh mam_id cookie value here. The dispatcher picks it up immediately."
          >
            <textarea
              value={cookieInput}
              onChange={(e) => setCookieInput(e.target.value)}
              placeholder="Paste the raw mam_id cookie value (no name= prefix)…"
              rows={4}
              style={{
                width: "100%",
                padding: "10px 12px",
                borderRadius: 8,
                border: `1px solid ${theme.border}`,
                background: theme.inp,
                color: theme.text,
                fontSize: 12,
                fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace",
                resize: "vertical",
                outline: "none",
              }}
            />
            <div
              style={{
                marginTop: 10,
                display: "flex",
                justifyContent: "flex-end",
                gap: 8,
              }}
            >
              <Btn
                variant="ghost"
                onClick={() => setCookieInput("")}
                disabled={busy}
              >
                Clear
              </Btn>
              <Btn
                variant="primary"
                onClick={pasteCookie}
                disabled={busy || !cookieInput.trim()}
              >
                {busy ? <Spin size={14} /> : "Save & validate"}
              </Btn>
            </div>
          </Section>
        </>
      )}
    </div>
  );
}

function StatCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: number | string;
  tone?: "ok" | "warn" | "dim";
}) {
  const theme = useTheme();
  const color =
    tone === "ok"
      ? theme.ok
      : tone === "warn"
        ? theme.warn
        : tone === "dim"
          ? theme.textDim
          : theme.text;
  return (
    <div
      style={{
        background: theme.bg2,
        border: `1px solid ${theme.borderL}`,
        borderRadius: 12,
        padding: 16,
      }}
    >
      <div
        style={{
          fontSize: 12,
          color: theme.textDim,
          textTransform: "uppercase",
          letterSpacing: 0.4,
          fontWeight: 600,
        }}
      >
        {label}
      </div>
      <div
        style={{
          marginTop: 8,
          fontSize: 26,
          fontWeight: 700,
          color,
        }}
      >
        {value}
      </div>
    </div>
  );
}

function KV({ label, children }: { label: string; children: React.ReactNode }) {
  const theme = useTheme();
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "space-between",
        gap: 16,
        padding: "8px 0",
        borderBottom: `1px solid ${theme.borderL}`,
        fontSize: 13,
      }}
    >
      <span style={{ color: theme.textDim, fontWeight: 600 }}>{label}</span>
      <span style={{ color: theme.text2, textAlign: "right" }}>{children}</span>
    </div>
  );
}

function Badge({
  tone,
  children,
}: {
  tone: "ok" | "warn" | "err";
  children: React.ReactNode;
}) {
  const theme = useTheme();
  const color =
    tone === "ok" ? theme.ok : tone === "warn" ? theme.warn : theme.err;
  return (
    <span
      style={{
        fontSize: 11,
        padding: "3px 10px",
        borderRadius: 99,
        background: color + "22",
        color,
        fontWeight: 700,
      }}
    >
      {children}
    </span>
  );
}

function Banner({
  tone,
  children,
}: {
  tone: "ok" | "warn" | "err";
  children: React.ReactNode;
}) {
  const theme = useTheme();
  const color =
    tone === "ok" ? theme.ok : tone === "warn" ? theme.warn : theme.err;
  return (
    <div
      style={{
        background: color + "22",
        border: `1px solid ${color}55`,
        color,
        padding: "10px 14px",
        borderRadius: 8,
        fontSize: 13,
        marginBottom: 16,
      }}
    >
      {children}
    </div>
  );
}


// ─── MAM economy (auto-buy) ──────────────────────────────────
//
// Three sections bundled into one component so MamPage stays
// readable: VIP auto-buy, upload auto-buy, auto-buy history.
// Config changes go through PUT /config's key whitelist, so a
// rogue field can't corrupt unrelated settings.

function EconomySections() {
  const theme = useTheme();
  const [config, setConfig] = useState<EconomyConfig | null>(null);
  const [audit, setAudit] = useState<AuditRow[] | null>(null);
  const [configBusy, setConfigBusy] = useState(false);

  async function loadAll() {
    try {
      const [cfg, rows] = await Promise.all([
        economyApi.getConfig(),
        economyApi.audit({ limit: 20 }),
      ]);
      setConfig(cfg);
      setAudit(rows);
    } catch {
      /* non-blocking — render with nulls */
    }
  }

  useEffect(() => {
    loadAll();
  }, []);

  async function patchConfig(patch: Partial<EconomyConfig>) {
    if (!config) return;
    setConfigBusy(true);
    try {
      const next = await economyApi.putConfig(patch);
      setConfig(next);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setConfigBusy(false);
    }
  }

  async function dismissIntro() {
    await patchConfig({ mam_economy_intro_dismissed: true });
  }

  async function vipBuyNow(weeks: number | "max") {
    try {
      const r = await economyApi.vipBuy(weeks);
      if (r.ok) {
        toast.success(
          `VIP buy OK — new seedbonus ${r.new_seedbonus?.toLocaleString() ?? "?"}`,
        );
      } else {
        toast.error(`VIP buy failed: ${r.message}`);
      }
      await loadAll();
    } catch (e) {
      toast.error(String(e));
    }
  }

  async function uploadBuyNow(body: { gb: number } | { mode: "max_affordable" }) {
    try {
      const r = await economyApi.uploadBuy(body);
      if (r.ok) {
        toast.success(
          `Upload buy OK — new seedbonus ${r.new_seedbonus?.toLocaleString() ?? "?"}`,
        );
      } else {
        toast.error(`Upload buy failed: ${r.message}`);
      }
      await loadAll();
    } catch (e) {
      toast.error(String(e));
    }
  }

  if (config === null) {
    return (
      <Section
        title="Auto-buy"
        subtitle="Loading economy configuration…"
      >
        <Spin size={16} />
      </Section>
    );
  }

  const lastVipAgo = relativeSince(config.mam_economy_last_vip_buy_at);
  const lastUploadAgo = relativeSince(config.mam_economy_last_upload_buy_at);

  return (
    <>
      {config.mam_economy_dry_run && (
        <div
          style={{
            background: theme.warn + "28",
            border: `1px solid ${theme.warn}`,
            borderRadius: 10,
            padding: "10px 14px",
            marginBottom: 16,
            fontSize: 13,
            color: theme.warn,
            fontWeight: 600,
          }}
        >
          Dry-run mode is ON — bonus-point buys are simulated
          (no BP spent, no MAM round-trips). Audit rows are tagged
          <code style={{ margin: "0 4px" }}>[DRY RUN]</code>.
          Scheduler timestamps are NOT bumped, so you can flip this
          off later without leaving a phantom lockout.
        </div>
      )}

      {!config.mam_economy_intro_dismissed && (
        <div
          style={{
            background: theme.bg2,
            border: `1px solid ${theme.borderL}`,
            borderRadius: 10,
            padding: "12px 14px",
            marginBottom: 16,
            fontSize: 13,
            color: theme.text2,
          }}
        >
          <div style={{ fontWeight: 700, color: theme.text, marginBottom: 4 }}>
            Auto-buy is off
          </div>
          <div style={{ color: theme.textDim, lineHeight: 1.5 }}>
            The MAM economy features (VIP auto-buy, upload-credit
            auto-buy, buffer gate, personal-FL offers on grabs) are
            all disabled by default. Enable each one individually in
            the sections below.
          </div>
          <div
            style={{
              marginTop: 8,
              display: "flex",
              justifyContent: "flex-end",
            }}
          >
            <Btn variant="ghost" onClick={dismissIntro} disabled={configBusy}>
              Got it
            </Btn>
          </div>
        </div>
      )}

      {/* VIP auto-buy */}
      <Section
        title="Auto-buy: VIP"
        subtitle="Spend bonus points on VIP time automatically at a fixed interval."
        right={
          <div style={{ display: "flex", gap: 6 }}>
            <Btn
              variant="ghost"
              onClick={() => vipBuyNow(config.mam_economy_vip_weeks)}
              disabled={configBusy}
            >
              Buy now
            </Btn>
          </div>
        }
      >
        <KV label="Enabled">
          <Toggle
            on={config.mam_economy_vip_enabled}
            disabled={configBusy}
            onChange={(v) => patchConfig({ mam_economy_vip_enabled: v })}
          />
        </KV>
        <KV label="Interval (hours)">
          <NumInput
            value={config.mam_economy_vip_interval_hours}
            disabled={configBusy}
            onCommit={(v) =>
              patchConfig({ mam_economy_vip_interval_hours: v })
            }
            min={1}
            step={1}
          />
        </KV>
        <KV label="Weeks per buy">
          <select
            value={String(config.mam_economy_vip_weeks)}
            disabled={configBusy}
            onChange={(e) =>
              patchConfig({
                mam_economy_vip_weeks:
                  e.target.value === "max"
                    ? "max"
                    : Number(e.target.value),
              })
            }
            style={selectStyle(theme)}
          >
            <option value="4">4 (5,000 BP)</option>
            <option value="8">8 (10,000 BP)</option>
            <option value="12">12 (15,000 BP)</option>
            <option value="max">max</option>
          </select>
        </KV>
        <KV label="Skip if seedbonus below">
          <NumInput
            value={config.mam_economy_vip_min_bonus}
            disabled={configBusy}
            onCommit={(v) => patchConfig({ mam_economy_vip_min_bonus: v })}
            min={0}
            step={500}
          />
        </KV>
        <KV label="Last bought">{lastVipAgo}</KV>
      </Section>

      {/* Upload-credit auto-buy */}
      <Section
        title="Auto-buy: Upload credit"
        subtitle="Three independent triggers — ratio, buffer, bonus excess. The first to fire wins each interval tick."
        right={
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {/* MAM rejects programmatic upload buys under 50 GB
                with a log-spam error — preset buttons start at the
                floor. Chunk minimums are also enforced at config
                PUT time on the backend. */}
            {[50, 100, 250, 500].map((gb) => (
              <Btn
                key={gb}
                variant="ghost"
                onClick={() => uploadBuyNow({ gb })}
                disabled={configBusy}
              >
                Buy {gb} GB
              </Btn>
            ))}
            <Btn
              variant="primary"
              onClick={() => uploadBuyNow({ mode: "max_affordable" })}
              disabled={configBusy}
            >
              Max affordable
            </Btn>
          </div>
        }
      >
        <KV label="Enabled">
          <Toggle
            on={config.mam_economy_upload_enabled}
            disabled={configBusy}
            onChange={(v) => patchConfig({ mam_economy_upload_enabled: v })}
          />
        </KV>
        <KV label="Interval (hours)">
          <NumInput
            value={config.mam_economy_upload_interval_hours}
            disabled={configBusy}
            onCommit={(v) =>
              patchConfig({ mam_economy_upload_interval_hours: v })
            }
            min={1}
            step={1}
          />
        </KV>
        <KV label="Last bought">{lastUploadAgo}</KV>

        <div style={{ marginTop: 14 }}>
          <SubHeader>Triggers</SubHeader>
        </div>

        {/* Ratio trigger */}
        <TriggerRow
          label="Ratio"
          enabled={config.mam_economy_upload_ratio_trigger}
          onToggle={(v) =>
            patchConfig({ mam_economy_upload_ratio_trigger: v })
          }
          disabled={configBusy}
          threshold={{
            label: "Buy if ratio <",
            value: config.mam_economy_upload_ratio_floor,
            step: 0.1,
            onCommit: (v) =>
              patchConfig({ mam_economy_upload_ratio_floor: v }),
          }}
          chunk={{
            label: "Buy (GB)",
            value: config.mam_economy_upload_ratio_chunk_gb,
            step: 1,
            onCommit: (v) =>
              patchConfig({ mam_economy_upload_ratio_chunk_gb: v }),
          }}
        />
        {/* Buffer trigger */}
        <TriggerRow
          label="Buffer"
          enabled={config.mam_economy_upload_buffer_trigger}
          onToggle={(v) =>
            patchConfig({ mam_economy_upload_buffer_trigger: v })
          }
          disabled={configBusy}
          threshold={{
            label: "Buy if buffer < (GB)",
            value: config.mam_economy_upload_buffer_floor_gb,
            step: 1,
            onCommit: (v) =>
              patchConfig({ mam_economy_upload_buffer_floor_gb: v }),
          }}
          chunk={{
            label: "Buy (GB)",
            value: config.mam_economy_upload_buffer_chunk_gb,
            step: 1,
            onCommit: (v) =>
              patchConfig({ mam_economy_upload_buffer_chunk_gb: v }),
          }}
        />
        {/* Bonus trigger */}
        <TriggerRow
          label="Bonus excess"
          enabled={config.mam_economy_upload_bonus_trigger}
          onToggle={(v) =>
            patchConfig({ mam_economy_upload_bonus_trigger: v })
          }
          disabled={configBusy}
          threshold={{
            label: "Spend excess above (BP)",
            value: config.mam_economy_upload_bonus_ceiling,
            step: 500,
            onCommit: (v) =>
              patchConfig({ mam_economy_upload_bonus_ceiling: v }),
          }}
        />

        <div style={{ marginTop: 14 }}>
          <SubHeader>Buffer gate (pre-download)</SubHeader>
        </div>
        <KV label="Enabled">
          <Toggle
            on={config.mam_economy_buffer_gate_enabled}
            disabled={configBusy}
            onChange={(v) =>
              patchConfig({ mam_economy_buffer_gate_enabled: v })
            }
          />
        </KV>
        <KV label="Safety margin (GB)">
          <NumInput
            value={config.mam_economy_buffer_gate_safety_margin_gb}
            disabled={configBusy}
            onCommit={(v) =>
              patchConfig({ mam_economy_buffer_gate_safety_margin_gb: v })
            }
            min={0}
            step={1}
          />
        </KV>

        <div style={{ marginTop: 14 }}>
          <SubHeader>Per-grab offers</SubHeader>
        </div>
        <KV label="Show &quot;use wedge&quot; checkbox on manual grabs">
          <Toggle
            on={config.mam_economy_manual_wedge_offer_enabled}
            disabled={configBusy}
            onChange={(v) =>
              patchConfig({ mam_economy_manual_wedge_offer_enabled: v })
            }
          />
        </KV>
        <KV label="Show &quot;buy personal FL (50k BP)&quot; checkbox on manual grabs">
          <Toggle
            on={config.mam_economy_fl_wedge_offer_enabled}
            disabled={configBusy}
            onChange={(v) =>
              patchConfig({ mam_economy_fl_wedge_offer_enabled: v })
            }
          />
        </KV>

        <div style={{ marginTop: 14 }}>
          <SubHeader>Operator / testing</SubHeader>
        </div>
        <KV label="Dry-run mode (simulate buys, spend no BP)">
          <Toggle
            on={config.mam_economy_dry_run}
            disabled={configBusy}
            onChange={(v) => patchConfig({ mam_economy_dry_run: v })}
          />
        </KV>
      </Section>

      {/* Audit history */}
      <Section
        title="Auto-buy history"
        subtitle="Most recent 20 rows. Skips are dim-toned; successes are green; failures red."
      >
        {audit === null ? (
          <Spin size={14} />
        ) : audit.length === 0 ? (
          <div style={{ color: theme.textDim, fontSize: 13 }}>
            No auto-buy activity yet.
          </div>
        ) : (
          <table
            style={{
              width: "100%",
              borderCollapse: "collapse",
              fontSize: 12,
            }}
          >
            <thead>
              <tr style={{ color: theme.textDim, textAlign: "left" }}>
                <th style={thStyle(theme)}>When</th>
                <th style={thStyle(theme)}>Action</th>
                <th style={thStyle(theme)}>Trigger</th>
                <th style={thStyle(theme)}>Outcome</th>
                <th style={{ ...thStyle(theme), textAlign: "right" }}>
                  Amount
                </th>
                <th style={{ ...thStyle(theme), textAlign: "right" }}>
                  Cost
                </th>
                <th style={thStyle(theme)}>Note</th>
              </tr>
            </thead>
            <tbody>
              {audit.map((row) => {
                const ox = formatOutcome(row.outcome);
                const tone =
                  ox.tone === "ok"
                    ? theme.ok
                    : ox.tone === "warn"
                      ? theme.warn
                      : ox.tone === "err"
                        ? theme.err
                        : theme.textDim;
                return (
                  <tr
                    key={row.id}
                    style={{
                      borderTop: `1px solid ${theme.borderL}`,
                      color: theme.text2,
                    }}
                  >
                    <td style={tdStyle}>
                      {formatRelativeTime(row.occurred_at)}
                    </td>
                    <td style={tdStyle}>{formatAction(row.action)}</td>
                    <td style={tdStyle}>{row.trigger}</td>
                    <td style={{ ...tdStyle, color: tone, fontWeight: 600 }}>
                      {ox.label}
                    </td>
                    <td style={{ ...tdStyle, textAlign: "right" }}>
                      {row.amount ?? "—"}
                    </td>
                    <td style={{ ...tdStyle, textAlign: "right" }}>
                      {formatBp(row.cost_points)}
                    </td>
                    <td style={{ ...tdStyle, color: theme.textDim }}>
                      {row.message ?? ""}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Section>
    </>
  );
}


// ─── Small styling helpers for EconomySections ─────────────

function selectStyle(theme: ReturnType<typeof useTheme>) {
  return {
    padding: "4px 8px",
    borderRadius: 6,
    border: `1px solid ${theme.border}`,
    background: theme.inp,
    color: theme.text,
    fontSize: 12,
  } as const;
}

function thStyle(theme: ReturnType<typeof useTheme>) {
  return {
    padding: "6px 8px",
    fontWeight: 600,
    borderBottom: `1px solid ${theme.border}`,
  } as const;
}

const tdStyle: React.CSSProperties = {
  padding: "6px 8px",
  verticalAlign: "middle",
};

function SubHeader({ children }: { children: React.ReactNode }) {
  const theme = useTheme();
  return (
    <div
      style={{
        fontSize: 11,
        color: theme.textDim,
        textTransform: "uppercase",
        letterSpacing: 0.4,
        fontWeight: 700,
        paddingBottom: 4,
        borderBottom: `1px solid ${theme.borderL}`,
      }}
    >
      {children}
    </div>
  );
}

function Toggle({
  on,
  onChange,
  disabled,
}: {
  on: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  const theme = useTheme();
  return (
    <button
      type="button"
      onClick={() => !disabled && onChange(!on)}
      disabled={disabled}
      aria-pressed={on}
      style={{
        width: 44,
        height: 22,
        borderRadius: 99,
        border: `1px solid ${on ? theme.ok : theme.border}`,
        background: on ? theme.ok + "55" : theme.inp,
        position: "relative",
        cursor: disabled ? "not-allowed" : "pointer",
        padding: 0,
      }}
    >
      <span
        style={{
          position: "absolute",
          top: 2,
          left: on ? 22 : 2,
          width: 16,
          height: 16,
          borderRadius: 99,
          background: on ? theme.ok : theme.textDim,
          transition: "left 0.15s ease",
        }}
      />
    </button>
  );
}

function NumInput({
  value,
  onCommit,
  disabled,
  min,
  step,
}: {
  value: number;
  onCommit: (v: number) => void;
  disabled?: boolean;
  min?: number;
  step?: number;
}) {
  const theme = useTheme();
  const [local, setLocal] = useState(String(value));
  useEffect(() => {
    setLocal(String(value));
  }, [value]);
  return (
    <input
      type="number"
      value={local}
      min={min}
      step={step}
      disabled={disabled}
      onChange={(e) => setLocal(e.target.value)}
      onBlur={() => {
        const n = Number(local);
        if (!Number.isNaN(n) && n !== value) onCommit(n);
      }}
      style={{
        width: 100,
        padding: "4px 8px",
        borderRadius: 6,
        border: `1px solid ${theme.border}`,
        background: theme.inp,
        color: theme.text,
        fontSize: 12,
        textAlign: "right",
      }}
    />
  );
}

function TriggerRow({
  label,
  enabled,
  onToggle,
  disabled,
  threshold,
  chunk,
}: {
  label: string;
  enabled: boolean;
  onToggle: (v: boolean) => void;
  disabled?: boolean;
  threshold: {
    label: string;
    value: number;
    step: number;
    onCommit: (v: number) => void;
  };
  chunk?: {
    label: string;
    value: number;
    step: number;
    onCommit: (v: number) => void;
  };
}) {
  const theme = useTheme();
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 16,
        flexWrap: "wrap",
        padding: "10px 0",
        borderBottom: `1px solid ${theme.borderL}`,
        fontSize: 13,
      }}
    >
      <div style={{ width: 100, fontWeight: 600, color: theme.text2 }}>
        {label}
      </div>
      <Toggle on={enabled} onChange={onToggle} disabled={disabled} />
      <div
        style={{
          display: "flex",
          gap: 14,
          flexWrap: "wrap",
          opacity: enabled ? 1 : 0.55,
        }}
      >
        <label
          style={{
            display: "flex",
            gap: 6,
            alignItems: "center",
            color: theme.textDim,
          }}
        >
          <span>{threshold.label}</span>
          <NumInput
            value={threshold.value}
            onCommit={threshold.onCommit}
            disabled={disabled || !enabled}
            step={threshold.step}
          />
        </label>
        {chunk && (
          <label
            style={{
              display: "flex",
              gap: 6,
              alignItems: "center",
              color: theme.textDim,
            }}
          >
            <span>{chunk.label}</span>
            <NumInput
              value={chunk.value}
              onCommit={chunk.onCommit}
              disabled={disabled || !enabled}
              step={chunk.step}
            />
          </label>
        )}
      </div>
    </div>
  );
}

function relativeSince(ts: number): string {
  if (!ts || ts <= 0) return "never";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return `${Math.round(diff)}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${(diff / 3600).toFixed(1)}h ago`;
  return `${(diff / 86400).toFixed(1)}d ago`;
}
