// Mobile-native settings page.
//
// The desktop page has 15 sections in a 220px sidebar; mobile
// stacks them all as collapsed sections in a single scrollable
// page with a sticky save bar at the bottom.
//
// Coverage strategy:
//   - All simple toggles + numeric fields + text fields: full edit
//   - Credentials (MAM, qBit, Hardcover, ABS): masked w/ change flow
//   - Notifications: ntfy URL + topic + per-event toggles
//   - Library management, ABS picker, format priorities, metadata
//     source reordering: simplified read-only views with a "manage
//     on desktop" hint — these flows have UX needs (drag-reorder,
//     multi-step wizards) that don't translate cleanly to a phone.
import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import { useTheme } from "../theme";
import {
  MobileBtn,
  MobileSection,
  MobileBackButton,
  MobileBadge,
} from "../components/mobile";

type S = Record<string, unknown>;

interface CredItem {
  key: string;
  label: string;
  configured: boolean;
}

// ─── Reusable mobile field components ───────────────────────────

function MobileTog({
  on,
  onToggle,
}: {
  on: boolean;
  onToggle: () => void;
}) {
  const t = useTheme();
  return (
    <button
      onClick={onToggle}
      style={{
        width: 50,
        height: 28,
        borderRadius: 14,
        background: on ? t.ok : t.bg4,
        border: "none",
        padding: 3,
        cursor: "pointer",
        flexShrink: 0,
      }}
    >
      <div
        style={{
          width: 22,
          height: 22,
          borderRadius: "50%",
          background: "#fff",
          transform: on ? "translateX(22px)" : "translateX(0)",
          transition: "transform 0.2s",
        }}
      />
    </button>
  );
}

function FieldRow({
  label,
  desc,
  warn,
  children,
}: {
  label: string;
  desc?: string;
  warn?: string;
  children: ReactNode;
}) {
  const t = useTheme();
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 8,
        padding: "12px 0",
        borderBottom: `1px solid ${t.borderL}`,
      }}
    >
      <div>
        <div style={{ fontSize: 14, fontWeight: 600, color: t.text }}>
          {label}
        </div>
        {desc && (
          <div style={{ fontSize: 12, color: t.td, marginTop: 2, lineHeight: 1.4 }}>
            {desc}
          </div>
        )}
        {warn && (
          <div style={{ fontSize: 12, color: t.warn, marginTop: 2 }}>
            ⚠ {warn}
          </div>
        )}
      </div>
      <div>{children}</div>
    </div>
  );
}

function ToggleRow({
  label,
  desc,
  warn,
  on,
  onToggle,
}: {
  label: string;
  desc?: string;
  warn?: string;
  on: boolean;
  onToggle: () => void;
}) {
  const t = useTheme();
  return (
    <div
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 12,
        padding: "12px 0",
        borderBottom: `1px solid ${t.borderL}`,
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 14, fontWeight: 600, color: t.text }}>
          {label}
        </div>
        {desc && (
          <div style={{ fontSize: 12, color: t.td, marginTop: 2, lineHeight: 1.4 }}>
            {desc}
          </div>
        )}
        {warn && (
          <div style={{ fontSize: 12, color: t.warn, marginTop: 2 }}>
            ⚠ {warn}
          </div>
        )}
      </div>
      <MobileTog on={on} onToggle={onToggle} />
    </div>
  );
}

function MobileTextInput({
  value,
  onChange,
  placeholder,
  type = "text",
  rows,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
  rows?: number;
}) {
  const t = useTheme();
  const sharedStyle: React.CSSProperties = {
    width: "100%",
    minHeight: 44,
    padding: "10px 12px",
    background: t.inp,
    color: t.text,
    border: `1px solid ${t.border}`,
    borderRadius: 8,
    fontSize: 16,
    fontFamily: "inherit",
    outline: "none",
  };
  if (rows && rows > 1) {
    return (
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        rows={rows}
        style={{ ...sharedStyle, resize: "vertical" }}
      />
    );
  }
  return (
    <input
      type={type}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      style={sharedStyle}
    />
  );
}

function MobileNumberInput({
  value,
  onChange,
  min,
  suffix,
}: {
  value: number;
  onChange: (v: number) => void;
  min?: number;
  suffix?: string;
}) {
  const t = useTheme();
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <input
        type="number"
        min={min}
        value={value}
        onChange={(e) => onChange(parseInt(e.target.value) || 0)}
        style={{
          width: 100,
          minHeight: 44,
          padding: "0 12px",
          background: t.inp,
          color: t.text,
          border: `1px solid ${t.border}`,
          borderRadius: 8,
          fontSize: 16,
        }}
      />
      {suffix && <span style={{ fontSize: 13, color: t.td }}>{suffix}</span>}
    </div>
  );
}

function MobileSelect({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { v: string; label: string }[];
}) {
  const t = useTheme();
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      style={{
        minHeight: 44,
        padding: "0 12px",
        background: t.inp,
        color: t.text,
        border: `1px solid ${t.border}`,
        borderRadius: 8,
        fontSize: 16,
        width: "100%",
      }}
    >
      {options.map((o) => (
        <option key={o.v} value={o.v}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

function MobileCredField({
  item,
  desc,
  onSaved,
  canGenerate,
  clearable,
  clearConfirm,
}: {
  item: CredItem;
  desc?: string;
  onSaved: () => void;
  canGenerate?: boolean;
  clearable?: boolean;
  clearConfirm?: string;
}) {
  const t = useTheme();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);

  const save = async () => {
    if (!value.trim()) return;
    setBusy(true);
    try {
      await api.post(`/v1/credentials/${item.key}`, { value: value.trim() });
      setEditing(false);
      setValue("");
      onSaved();
    } catch {
      /* ignore */
    } finally {
      setBusy(false);
    }
  };

  const clear = async () => {
    if (!confirm(clearConfirm || `Clear ${item.label}?`)) return;
    setBusy(true);
    try {
      await api.del(`/v1/credentials/${item.key}`);
      onSaved();
    } catch {
      /* ignore */
    } finally {
      setBusy(false);
    }
  };

  const generate = () => {
    const bytes = new Uint8Array(32);
    crypto.getRandomValues(bytes);
    setValue(
      Array.from(bytes)
        .map((b) => b.toString(16).padStart(2, "0"))
        .join(""),
    );
  };

  return (
    <FieldRow label={item.label} desc={desc}>
      {item.configured && !editing ? (
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span
            style={{
              flex: 1,
              fontSize: 14,
              color: t.td,
              letterSpacing: "3px",
            }}
          >
            ••••••••
          </span>
          <MobileBtn
            variant="ghost"
            onClick={() => {
              setEditing(true);
              setValue("");
            }}
            style={{ minHeight: 36, fontSize: 13 }}
          >
            Change
          </MobileBtn>
          {clearable && (
            <MobileBtn
              variant="ghost"
              onClick={clear}
              disabled={busy}
              style={{ minHeight: 36, fontSize: 13 }}
            >
              Clear
            </MobileBtn>
          )}
        </div>
      ) : editing ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <input
            type={canGenerate ? "text" : "password"}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={`Enter ${item.label}…`}
            autoFocus
            style={{
              minHeight: 44,
              padding: "0 12px",
              background: t.inp,
              color: t.text,
              border: `1px solid ${t.border}`,
              borderRadius: 8,
              fontSize: 16,
            }}
          />
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {canGenerate && (
              <MobileBtn
                variant="ghost"
                onClick={generate}
                style={{ minHeight: 36, fontSize: 13 }}
              >
                Generate
              </MobileBtn>
            )}
            <MobileBtn
              variant="primary"
              onClick={save}
              disabled={busy || !value.trim()}
              style={{ minHeight: 36, fontSize: 13 }}
            >
              {busy ? "Saving…" : "Save"}
            </MobileBtn>
            <MobileBtn
              variant="ghost"
              onClick={() => {
                setEditing(false);
                setValue("");
              }}
              style={{ minHeight: 36, fontSize: 13 }}
            >
              Cancel
            </MobileBtn>
          </div>
        </div>
      ) : (
        <MobileBtn
          variant="primary"
          onClick={() => {
            setEditing(true);
            setValue("");
          }}
          style={{ minHeight: 36, fontSize: 13 }}
        >
          Set
        </MobileBtn>
      )}
    </FieldRow>
  );
}

// ─── Page ───────────────────────────────────────────────────────

export default function MobileSettingsPage() {
  const t = useTheme();
  const [s, setS] = useState<S | null>(null);
  const [creds, setCreds] = useState<CredItem[]>([]);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [testNtfyResult, setTestNtfyResult] = useState<string | null>(null);
  const [testQbitResult, setTestQbitResult] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<S>("/v1/settings")
      .then(setS)
      .catch((e) => setMsg(`Error: ${e}`));
  }, []);

  const loadCreds = () => {
    api
      .get<{ items: CredItem[] }>("/v1/credentials")
      .then((r) => setCreds(r.items))
      .catch(() => {});
  };
  useEffect(() => {
    loadCreds();
  }, []);

  if (!s) {
    return (
      <div style={{ padding: 40, textAlign: "center", color: t.tg }}>
        Loading…
      </div>
    );
  }

  const upd = (k: string, v: unknown) =>
    setS((o) => (o ? { ...o, [k]: v } : o));

  const save = async () => {
    setSaving(true);
    setMsg("");
    try {
      await api.patch("/v1/settings", s);
      setMsg("Saved!");
      const fresh = await api.get<S>("/v1/settings");
      setS(fresh);
      setTimeout(() => setMsg(""), 3000);
    } catch {
      setMsg("Error saving");
    } finally {
      setSaving(false);
    }
  };

  const testQbit = async () => {
    setTestQbitResult(null);
    try {
      const r = await api.post<{
        ok: boolean;
        version?: string | null;
        error_class?: string;
        error?: string;
      }>("/api/qbittorrent/test");
      if (r.ok) {
        setTestQbitResult(`✓ Connected (${r.version || "version unknown"})`);
      } else {
        setTestQbitResult(`✗ ${r.error_class || "error"}: ${r.error || ""}`);
      }
    } catch (e) {
      setTestQbitResult(`✗ ${e}`);
    }
    setTimeout(() => setTestQbitResult(null), 12000);
  };

  const testNtfy = async () => {
    setTestNtfyResult(null);
    try {
      const r = await api.post<{ ok: boolean; message: string }>(
        "/v1/mam/test-notification",
      );
      setTestNtfyResult(r.ok ? "✓ Sent!" : `✗ ${r.message}`);
    } catch (e) {
      setTestNtfyResult(`✗ ${e}`);
    }
    setTimeout(() => setTestNtfyResult(null), 5000);
  };

  const grabMode = s.policy_vip_only ? "vip" : s.policy_free_only ? "free" : "any";
  const setGrabMode = (m: string) => {
    upd("policy_vip_only", m === "vip");
    upd("policy_free_only", m === "free");
  };

  const mamCreds = creds.filter((c) =>
    ["mam_session_id", "mam_irc_password"].includes(c.key),
  );
  const qbitCreds = creds.filter((c) => c.key === "qbit_password");
  const apiCreds = creds.filter((c) => c.key === "hardcover_api_key");
  const absCreds = creds.filter((c) => c.key === "abs_api_key");

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 12,
        paddingBottom: 80,
      }}
    >
      <MobileBackButton to="dashboard" label="Dashboard" />

      <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: t.text }}>
        Settings
      </h1>

      {/* ─── Pipeline ─────────────────────────────────────────── */}
      <MobileSection title="Pipeline" defaultOpen={false}>
        <ToggleRow
          label="IRC Listener"
          desc="Connect to MAM's #announce channel and process every new torrent through the filter gate."
          on={(s.mam_irc_enabled as boolean) ?? true}
          onToggle={() => upd("mam_irc_enabled", !(s.mam_irc_enabled ?? true))}
        />
        <ToggleRow
          label="Auto-Train Authors"
          desc="Co-authors of grabbed books auto-add to the allow list."
          on={(s.pipeline_auto_train_enabled as boolean) ?? true}
          onToggle={() =>
            upd(
              "pipeline_auto_train_enabled",
              !(s.pipeline_auto_train_enabled ?? true),
            )
          }
        />
        <ToggleRow
          label="Dry Run"
          desc="Filter + policy run normally but nothing is downloaded."
          warn={s.dry_run ? "Active — no torrents will download" : undefined}
          on={!!s.dry_run}
          onToggle={() => upd("dry_run", !s.dry_run)}
        />
      </MobileSection>

      {/* ─── Review ──────────────────────────────────────────── */}
      <MobileSection title="Review & Enrichment" defaultOpen={false}>
        <ToggleRow
          label="Manual Review Queue"
          desc="Each downloaded book waits for your approval before Calibre delivery."
          on={(s.review_queue_enabled as boolean) ?? true}
          onToggle={() =>
            upd("review_queue_enabled", !(s.review_queue_enabled ?? true))
          }
        />
        <ToggleRow
          label="Metadata Enrichment"
          desc="Scrape sources for covers, descriptions, series, ISBN."
          on={!!s.metadata_enrichment_enabled}
          onToggle={() =>
            upd("metadata_enrichment_enabled", !s.metadata_enrichment_enabled)
          }
        />
        <FieldRow
          label="Review Timeout"
          desc="Books undecided this long auto-add to Calibre."
        >
          <MobileNumberInput
            value={(s.metadata_review_timeout_days as number) ?? 14}
            min={1}
            suffix="days"
            onChange={(v) => upd("metadata_review_timeout_days", v)}
          />
        </FieldRow>
      </MobileSection>

      {/* ─── Grab Policy ─────────────────────────────────────── */}
      <MobileSection title="Grab Policy" defaultOpen={false}>
        <FieldRow
          label="Grab Mode"
          desc="Any = follow rules; Free = only freeleech-eligible; VIP = only VIP-flagged"
        >
          <MobileSelect
            value={grabMode}
            onChange={setGrabMode}
            options={[
              { v: "any", label: "Any" },
              { v: "free", label: "Free only" },
              { v: "vip", label: "VIP only" },
            ]}
          />
        </FieldRow>
        <ToggleRow
          label="Always grab VIP"
          desc="VIP bypasses other checks."
          on={(s.policy_vip_always_grab as boolean) ?? true}
          onToggle={() =>
            upd(
              "policy_vip_always_grab",
              !(s.policy_vip_always_grab ?? true),
            )
          }
        />
        <ToggleRow
          label="Use freeleech wedges"
          desc="Spend a wedge to make non-free free."
          on={!!s.policy_use_wedge}
          onToggle={() => upd("policy_use_wedge", !s.policy_use_wedge)}
        />
        <FieldRow label="Ratio floor" desc="Skip grabs that would push ratio below this.">
          <MobileNumberInput
            value={(s.policy_ratio_floor as number) ?? 0}
            min={0}
            onChange={(v) => upd("policy_ratio_floor", v)}
          />
        </FieldRow>
        <FieldRow
          label="Min wedges reserved"
          desc="Don't dip below this many wedges."
        >
          <MobileNumberInput
            value={(s.policy_min_wedges_reserved as number) ?? 0}
            min={0}
            onChange={(v) => upd("policy_min_wedges_reserved", v)}
          />
        </FieldRow>
      </MobileSection>

      {/* ─── Snatch Budget ───────────────────────────────────── */}
      <MobileSection title="Snatch Budget" defaultOpen={false}>
        <FieldRow label="Budget cap" desc="Max concurrent grabs that count toward budget.">
          <MobileNumberInput
            value={(s.snatch_budget_cap as number) ?? 5}
            min={1}
            onChange={(v) => upd("snatch_budget_cap", v)}
          />
        </FieldRow>
        <FieldRow label="Queue max" desc="Max torrents queued before FIFO rotation.">
          <MobileNumberInput
            value={(s.snatch_queue_max as number) ?? 10}
            min={1}
            onChange={(v) => upd("snatch_queue_max", v)}
          />
        </FieldRow>
        <FieldRow label="Delayed torrents path">
          <MobileTextInput
            value={(s.snatch_delayed_torrents_path as string) ?? ""}
            onChange={(v) => upd("snatch_delayed_torrents_path", v)}
            placeholder="/data/delayed"
          />
        </FieldRow>
      </MobileSection>

      {/* ─── MAM ─────────────────────────────────────────────── */}
      <MobileSection title="MyAnonamouse" defaultOpen={false}>
        <FieldRow label="IRC nickname">
          <MobileTextInput
            value={(s.mam_irc_nickname as string) ?? ""}
            onChange={(v) => upd("mam_irc_nickname", v)}
          />
        </FieldRow>
        <FieldRow label="IRC account">
          <MobileTextInput
            value={(s.mam_irc_account as string) ?? ""}
            onChange={(v) => upd("mam_irc_account", v)}
          />
        </FieldRow>
        {mamCreds.map((c) => {
          const desc =
            c.key === "mam_session_id"
              ? "Browser cookie value (for the website API)"
              : "IRC SASL password (for #announce)";
          return (
            <MobileCredField
              key={c.key}
              item={c}
              onSaved={loadCreds}
              desc={desc}
            />
          );
        })}
      </MobileSection>

      {/* ─── Download Client ─────────────────────────────────── */}
      <MobileSection title="Download Client" defaultOpen={false}>
        <FieldRow label="Client URL">
          <MobileTextInput
            value={(s.qbit_url as string) ?? ""}
            onChange={(v) => upd("qbit_url", v)}
            placeholder="http://qbittorrent:8080"
          />
        </FieldRow>
        <FieldRow label="Username">
          <MobileTextInput
            value={(s.qbit_username as string) ?? ""}
            onChange={(v) => upd("qbit_username", v)}
          />
        </FieldRow>
        {qbitCreds.map((c) => (
          <MobileCredField
            key={c.key}
            item={c}
            onSaved={loadCreds}
            desc="WebUI login password."
          />
        ))}
        <FieldRow label="Watch category">
          <MobileTextInput
            value={(s.qbit_category as string) ?? ""}
            onChange={(v) => upd("qbit_category", v)}
          />
        </FieldRow>
        <FieldRow label="Download path">
          <MobileTextInput
            value={(s.qbit_download_path as string) ?? ""}
            onChange={(v) => upd("qbit_download_path", v)}
          />
        </FieldRow>
        <FieldRow
          label="Test connection"
          warn="Some clients ban IPs after repeated failures."
        >
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <MobileBtn
              variant="ghost"
              onClick={testQbit}
              style={{ minHeight: 36, fontSize: 13 }}
            >
              Test
            </MobileBtn>
            {testQbitResult && (
              <span
                style={{
                  fontSize: 12,
                  color: testQbitResult.startsWith("✓") ? t.ok : t.err,
                }}
              >
                {testQbitResult}
              </span>
            )}
          </div>
        </FieldRow>
      </MobileSection>

      {/* ─── Notifications ───────────────────────────────────── */}
      <MobileSection title="Notifications" defaultOpen={false}>
        <FieldRow label="ntfy endpoint">
          <MobileTextInput
            value={(s.ntfy_endpoint as string) ?? ""}
            onChange={(v) => upd("ntfy_endpoint", v)}
            placeholder="https://ntfy.sh"
          />
        </FieldRow>
        <FieldRow label="ntfy topic">
          <MobileTextInput
            value={(s.ntfy_topic as string) ?? ""}
            onChange={(v) => upd("ntfy_topic", v)}
          />
        </FieldRow>
        <FieldRow label="Test ntfy">
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <MobileBtn
              variant="ghost"
              onClick={testNtfy}
              style={{ minHeight: 36, fontSize: 13 }}
            >
              Send test
            </MobileBtn>
            {testNtfyResult && (
              <span
                style={{
                  fontSize: 12,
                  color: testNtfyResult.startsWith("✓") ? t.ok : t.err,
                }}
              >
                {testNtfyResult}
              </span>
            )}
          </div>
        </FieldRow>
        <ToggleRow
          label="Notify on grab"
          on={!!s.notify_on_grab}
          onToggle={() => upd("notify_on_grab", !s.notify_on_grab)}
        />
        <ToggleRow
          label="Notify on download"
          on={!!s.notify_on_download}
          onToggle={() => upd("notify_on_download", !s.notify_on_download)}
        />
        <ToggleRow
          label="Notify on error"
          on={!!s.notify_on_error}
          onToggle={() => upd("notify_on_error", !s.notify_on_error)}
        />
        <ToggleRow
          label="Daily digest"
          on={!!s.notify_daily_digest}
          onToggle={() => upd("notify_daily_digest", !s.notify_daily_digest)}
        />
        <ToggleRow
          label="Weekly digest"
          on={!!s.notify_weekly_digest}
          onToggle={() =>
            upd("notify_weekly_digest", !s.notify_weekly_digest)
          }
        />
        {/* v2.21.0 Phase I — metadata cache worker notifications.
            Mobile parity for the desktop Notifications group. */}
        <ToggleRow
          label="Cache worker errors"
          desc="Stall, cache-write failure, tick crash."
          on={!!s.notify_on_metadata_cache_error}
          onToggle={() =>
            upd(
              "notify_on_metadata_cache_error",
              !s.notify_on_metadata_cache_error,
            )
          }
        />
        <ToggleRow
          label="Cache worker warnings"
          desc="Top-tier cooldown escalation, permanent author failure."
          on={!!s.notify_on_metadata_cache_warning}
          onToggle={() =>
            upd(
              "notify_on_metadata_cache_warning",
              !s.notify_on_metadata_cache_warning,
            )
          }
        />
        <ToggleRow
          label="Cache daily summary"
          desc="Once-per-day digest of today's scans + blocks."
          on={!!s.notify_on_metadata_cache_daily_summary}
          onToggle={() =>
            upd(
              "notify_on_metadata_cache_daily_summary",
              !s.notify_on_metadata_cache_daily_summary,
            )
          }
        />
        <ToggleRow
          label="Cache new books"
          desc="Per-author celebration when the worker discovers a new ASIN."
          on={!!s.notify_on_metadata_cache_new_book}
          onToggle={() =>
            upd(
              "notify_on_metadata_cache_new_book",
              !s.notify_on_metadata_cache_new_book,
            )
          }
        />
      </MobileSection>

      {/* ─── Metadata Sources ───────────────────────────────── */}
      <MobileSection title="Metadata Sources" defaultOpen={false}>
        {apiCreds.map((c) => (
          <MobileCredField
            key={c.key}
            item={c}
            onSaved={loadCreds}
            desc="Bearer token from hardcover.app → Account → API."
          />
        ))}
        <div
          style={{
            padding: "12px",
            background: t.bg3,
            border: `1px dashed ${t.border}`,
            borderRadius: 8,
            fontSize: 13,
            color: t.td,
            marginTop: 8,
          }}
        >
          Per-source enable/disable, rate limits, and priority
          reordering live on desktop. Tap{" "}
          <MobileBadge>Settings → Metadata Sources</MobileBadge> there for
          full control.
        </div>
      </MobileSection>

      {/* ─── Author Scanning ────────────────────────────────── */}
      <MobileSection title="Author Scanning" defaultOpen={false}>
        <ToggleRow
          label="Auto-scan enabled"
          on={!!s.author_autoscan_enabled}
          onToggle={() =>
            upd("author_autoscan_enabled", !s.author_autoscan_enabled)
          }
        />
        <ToggleRow
          label="Owned books only"
          desc="Skip authors with no owned books."
          on={!!s.author_autoscan_owned_only}
          onToggle={() =>
            upd(
              "author_autoscan_owned_only",
              !s.author_autoscan_owned_only,
            )
          }
        />
        <FieldRow label="Lookup interval (days)">
          <MobileNumberInput
            value={(s.author_lookup_interval_days as number) ?? 7}
            min={1}
            suffix="days"
            onChange={(v) => upd("author_lookup_interval_days", v)}
          />
        </FieldRow>
      </MobileSection>

      {/* ─── Library ─────────────────────────────────────────── */}
      <MobileSection title="Library Management" defaultOpen={false}>
        <FieldRow label="Sync interval (minutes)">
          <MobileNumberInput
            value={(s.library_sync_interval_minutes as number) ?? 60}
            min={1}
            suffix="min"
            onChange={(v) => upd("library_sync_interval_minutes", v)}
          />
        </FieldRow>
        <FieldRow label="Languages" desc="Comma-separated language codes.">
          <MobileTextInput
            value={(s.library_languages as string) ?? "en"}
            onChange={(v) => upd("library_languages", v)}
            placeholder="en, es, fr"
          />
        </FieldRow>
        <FieldRow label="Calibre library path">
          <MobileTextInput
            value={(s.calibre_library_path as string) ?? ""}
            onChange={(v) => upd("calibre_library_path", v)}
          />
        </FieldRow>
        <FieldRow label="Calibre-Web URL">
          <MobileTextInput
            value={(s.calibre_web_url as string) ?? ""}
            onChange={(v) => upd("calibre_web_url", v)}
          />
        </FieldRow>
        <div
          style={{
            padding: "12px",
            background: t.bg3,
            border: `1px dashed ${t.border}`,
            borderRadius: 8,
            fontSize: 13,
            color: t.td,
            marginTop: 8,
          }}
        >
          Multi-library setup (rescan, switch active, add new) is
          desktop-only.
        </div>
      </MobileSection>

      {/* ─── Audiobookshelf ─────────────────────────────────── */}
      <MobileSection title="Audiobookshelf" defaultOpen={false}>
        <FieldRow label="ABS URL">
          <MobileTextInput
            value={(s.abs_url as string) ?? ""}
            onChange={(v) => upd("abs_url", v)}
            placeholder="http://audiobookshelf:13378"
          />
        </FieldRow>
        <FieldRow label="Web URL override (for browser links)">
          <MobileTextInput
            value={(s.abs_web_url as string) ?? ""}
            onChange={(v) => upd("abs_web_url", v)}
          />
        </FieldRow>
        {absCreds.map((c) => (
          <MobileCredField
            key={c.key}
            item={c}
            onSaved={loadCreds}
            desc="ABS user API token."
          />
        ))}
        <FieldRow label="Audiobook sink path">
          <MobileTextInput
            value={(s.audiobook_sink_path as string) ?? ""}
            onChange={(v) => upd("audiobook_sink_path", v)}
          />
        </FieldRow>
        <FieldRow label="Audible region">
          <MobileSelect
            value={(s.audible_region as string) ?? "us"}
            onChange={(v) => upd("audible_region", v)}
            options={[
              { v: "us", label: "United States" },
              { v: "uk", label: "United Kingdom" },
              { v: "de", label: "Germany" },
              { v: "fr", label: "France" },
              { v: "ca", label: "Canada" },
              { v: "au", label: "Australia" },
              { v: "jp", label: "Japan" },
            ]}
          />
        </FieldRow>
        <div
          style={{
            padding: "12px",
            background: t.bg3,
            border: `1px dashed ${t.border}`,
            borderRadius: 8,
            fontSize: 13,
            color: t.td,
            marginTop: 8,
          }}
        >
          Library picker, test connection, and sink target setup live
          on desktop.
        </div>
      </MobileSection>

      {/* ─── Discovery MAM ──────────────────────────────────── */}
      <MobileSection title="Discovery MAM" defaultOpen={false}>
        <ToggleRow
          label="MAM search enabled"
          on={!!s.mam_search_enabled}
          onToggle={() => upd("mam_search_enabled", !s.mam_search_enabled)}
        />
        <ToggleRow
          label="Scheduled auto-scan"
          on={!!s.mam_autoscan_enabled}
          onToggle={() =>
            upd("mam_autoscan_enabled", !s.mam_autoscan_enabled)
          }
        />
        <FieldRow label="Auto-scan interval (minutes)">
          <MobileNumberInput
            value={(s.mam_autoscan_interval_minutes as number) ?? 60}
            min={1}
            suffix="min"
            onChange={(v) => upd("mam_autoscan_interval_minutes", v)}
          />
        </FieldRow>
        <div
          style={{
            padding: "12px",
            background: t.bg3,
            border: `1px dashed ${t.border}`,
            borderRadius: 8,
            fontSize: 13,
            color: t.td,
            marginTop: 8,
          }}
        >
          Format priorities (drag-reorder) live on desktop.
        </div>
      </MobileSection>

      {/* ─── Operational ────────────────────────────────────── */}
      <MobileSection title="Operational" defaultOpen={false}>
        <ToggleRow
          label="Verbose logging"
          desc="DEBUG-level logs in /v1/logs."
          on={!!s.verbose_logging}
          onToggle={() => upd("verbose_logging", !s.verbose_logging)}
        />
        <ToggleRow
          label="MAM debug match"
          desc="Exposes /api/v1/mam/debug-match for diagnosing scoring."
          on={!!s.mam_debug_match_enabled}
          onToggle={() => upd("mam_debug_match_enabled", !s.mam_debug_match_enabled)}
        />
      </MobileSection>

      {/* ─── Sticky save bar ────────────────────────────────── */}
      <div
        style={{
          position: "fixed",
          left: 0,
          right: 0,
          bottom: 0,
          padding: 12,
          paddingBottom: "max(12px, env(safe-area-inset-bottom))",
          background: t.bg2,
          borderTop: `1px solid ${t.border}`,
          display: "flex",
          gap: 8,
          alignItems: "center",
          zIndex: 50,
        }}
      >
        <span style={{ flex: 1, fontSize: 13, color: msg.startsWith("Error") ? t.err : t.ok, fontWeight: 600 }}>
          {msg}
        </span>
        <MobileBtn
          variant="primary"
          primary
          onClick={save}
          disabled={saving}
        >
          {saving ? "Saving…" : "Save settings"}
        </MobileBtn>
      </div>
    </div>
  );
}
