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
        MAM status
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
            title="Cookie health"
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

          <Section
            title="Emergency cookie paste"
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
