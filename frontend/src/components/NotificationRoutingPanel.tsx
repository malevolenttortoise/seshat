// NotificationRoutingPanel — Bundle B.2 (v2.28.0) settings surface.
//
// Renders the new ``notifications`` settings subtree:
//   * notifications.master_enabled  — global on/off (default true)
//   * notifications.quiet_hours     — window in which suppressible
//                                     events are silently dropped
//   * notifications.events.<name>   — per-event override of enabled
//                                     state, topic, and priority,
//                                     with wildcard support
//                                     (`grab.*`, `*`).
//
// Event catalogue is fetched from /api/v1/notifications/events so the
// table stays in lockstep with `app.notifications.events.REGISTRY`
// without duplicating constants on the frontend.
//
// State is buffered into the parent settings dict via the same `upd`
// helper the rest of the Settings page uses — Save lives at the
// SettingsPage level and PATCHes the whole settings tree at once.
import { useEffect, useState } from "react";
import { api } from "../api";
import { useTheme } from "../theme";

type S = Record<string, unknown>;

interface EventMeta {
  name: string;
  description: string;
  default_priority: number;
  default_tags: string[];
  suppressible_during_quiet_hours: boolean;
  legacy_setting_key: string | null;
  legacy_requires_master: boolean;
  legacy_default_enabled: boolean;
}

interface EventOverride {
  enabled?: boolean;
  topic?: string;
  priority?: number;
}

interface NotificationsConfig {
  master_enabled?: boolean;
  events?: Record<string, EventOverride>;
  quiet_hours?: {
    enabled?: boolean;
    start?: string;
    end?: string;
    timezone?: string;
  };
}

type EnabledChoice = "default" | "on" | "off";

function readEnabledChoice(ov: EventOverride | undefined): EnabledChoice {
  if (!ov || ov.enabled === undefined) return "default";
  return ov.enabled ? "on" : "off";
}

function applyEnabledChoice(
  ov: EventOverride | undefined,
  choice: EnabledChoice,
): EventOverride | undefined {
  const next = { ...(ov ?? {}) };
  if (choice === "default") delete next.enabled;
  else next.enabled = choice === "on";
  return Object.keys(next).length ? next : undefined;
}

function applyTopic(
  ov: EventOverride | undefined,
  topic: string,
): EventOverride | undefined {
  const next = { ...(ov ?? {}) };
  if (!topic) delete next.topic;
  else next.topic = topic;
  return Object.keys(next).length ? next : undefined;
}

function applyPriority(
  ov: EventOverride | undefined,
  priority: number | null,
): EventOverride | undefined {
  const next = { ...(ov ?? {}) };
  if (priority === null) delete next.priority;
  else next.priority = priority;
  return Object.keys(next).length ? next : undefined;
}

export function NotificationRoutingPanel({
  s, upd,
}: {
  s: S;
  upd: (k: string, v: unknown) => void;
}) {
  const t = useTheme();
  const [catalogue, setCatalogue] = useState<EventMeta[] | null>(null);
  const [tableOpen, setTableOpen] = useState(false);
  const [quietOpen, setQuietOpen] = useState(false);

  useEffect(() => {
    api.get<{ events: EventMeta[] }>("/v1/notifications/events")
      .then(r => setCatalogue(r.events))
      .catch(() => setCatalogue([]));
  }, []);

  const cfg: NotificationsConfig = (s.notifications as NotificationsConfig) || {};
  const masterEnabled = cfg.master_enabled !== false;
  const events = cfg.events || {};
  const qh = cfg.quiet_hours || {};

  const patch = (next: NotificationsConfig) => upd("notifications", next);

  const setMaster = (on: boolean) =>
    patch({ ...cfg, master_enabled: on });

  const setQuietField = (k: keyof NonNullable<NotificationsConfig["quiet_hours"]>, v: unknown) =>
    patch({ ...cfg, quiet_hours: { ...qh, [k]: v } });

  const setEventOverride = (name: string, ov: EventOverride | undefined) => {
    const nextEvents = { ...events };
    if (ov === undefined) delete nextEvents[name];
    else nextEvents[name] = ov;
    patch({ ...cfg, events: nextEvents });
  };

  const ist = {
    padding: "5px 8px",
    background: t.inp,
    border: `1px solid ${t.border}`,
    borderRadius: 4,
    color: t.text2,
    fontSize: 12,
    outline: "none",
  } as const;

  return (
    <div style={{
      border: `1px solid ${t.border}`,
      borderRadius: 8,
      padding: 16,
      marginTop: 16,
      background: t.bg2,
    }}>
      <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 4, color: t.text }}>
        Advanced Routing &amp; Quiet Hours
      </div>
      <div style={{ fontSize: 12, color: t.textDim, marginBottom: 12, maxWidth: 720 }}>
        Per-event topic routing and priority overrides — supports wildcards
        (e.g. <code>grab.*</code>, <code>*</code>) under <em>Per-event Overrides</em>.
        When an event has no override here, the legacy toggles above remain authoritative.
        Quiet hours silently drop routine successes; errors and warnings always fire through.
      </div>

      {/* Master enable */}
      <label style={{
        display: "flex", alignItems: "center", gap: 8, marginBottom: 12,
        cursor: "pointer", fontSize: 13, color: t.text2,
      }}>
        <input
          type="checkbox"
          checked={masterEnabled}
          onChange={e => setMaster(e.target.checked)}
        />
        <span><strong>Master enabled</strong> — uncheck to mute every notification this bus produces.</span>
      </label>

      {/* Quiet hours */}
      <details
        open={quietOpen}
        onToggle={e => setQuietOpen((e.target as HTMLDetailsElement).open)}
        style={{ marginBottom: 12 }}
      >
        <summary style={{ cursor: "pointer", fontSize: 13, color: t.text2, fontWeight: 600 }}>
          Quiet hours {qh.enabled ? `(${qh.start || "?"} → ${qh.end || "?"})` : "(off)"}
        </summary>
        <div style={{ marginTop: 10, paddingLeft: 14, display: "grid", rowGap: 8 }}>
          <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 13, color: t.text2 }}>
            <input
              type="checkbox"
              checked={!!qh.enabled}
              onChange={e => setQuietField("enabled", e.target.checked)}
            />
            Enable quiet hours
          </label>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "center", fontSize: 12, color: t.textDim }}>
            <label>
              Start{" "}
              <input
                type="time"
                value={qh.start || "23:00"}
                onChange={e => setQuietField("start", e.target.value)}
                style={ist}
              />
            </label>
            <label>
              End{" "}
              <input
                type="time"
                value={qh.end || "07:00"}
                onChange={e => setQuietField("end", e.target.value)}
                style={ist}
              />
            </label>
            <label>
              Timezone{" "}
              <input
                type="text"
                placeholder="(system local)"
                value={qh.timezone || ""}
                onChange={e => setQuietField("timezone", e.target.value)}
                style={{ ...ist, width: 200 }}
              />
            </label>
          </div>
          <div style={{ fontSize: 11, color: t.textDim }}>
            Overnight windows (e.g. 23:00 → 07:00) are supported. Timezone uses
            IANA names (<code>America/New_York</code>); leave blank for system local.
          </div>
        </div>
      </details>

      {/* Per-event overrides */}
      <details
        open={tableOpen}
        onToggle={e => setTableOpen((e.target as HTMLDetailsElement).open)}
      >
        <summary style={{ cursor: "pointer", fontSize: 13, color: t.text2, fontWeight: 600 }}>
          Per-event Overrides ({Object.keys(events).length} configured)
        </summary>
        <div style={{ marginTop: 10 }}>
          {catalogue === null ? (
            <div style={{ fontSize: 12, color: t.textDim }}>Loading…</div>
          ) : catalogue.length === 0 ? (
            <div style={{ fontSize: 12, color: t.err }}>Failed to load event catalogue.</div>
          ) : (
            <>
              <WildcardRows events={events} setEventOverride={setEventOverride} ist={ist} />
              <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse", marginTop: 14 }}>
                <thead>
                  <tr style={{ color: t.textDim }}>
                    <th style={{ textAlign: "left", padding: "6px 4px", borderBottom: `1px solid ${t.border}` }}>Event</th>
                    <th style={{ textAlign: "left", padding: "6px 4px", borderBottom: `1px solid ${t.border}` }}>Enabled</th>
                    <th style={{ textAlign: "left", padding: "6px 4px", borderBottom: `1px solid ${t.border}` }}>Topic Override</th>
                    <th style={{ textAlign: "left", padding: "6px 4px", borderBottom: `1px solid ${t.border}` }}>Priority</th>
                  </tr>
                </thead>
                <tbody>
                  {catalogue.map(meta => {
                    const ov = events[meta.name];
                    return (
                      <tr key={meta.name}>
                        <td style={{ padding: "6px 4px", borderBottom: `1px solid ${t.border}`, color: t.text2 }}>
                          <div style={{ fontFamily: "monospace" }}>{meta.name}</div>
                          <div style={{ fontSize: 11, color: t.textDim }}>{meta.description}</div>
                          {!meta.suppressible_during_quiet_hours && (
                            <div style={{ fontSize: 10, color: t.warn, marginTop: 2 }}>
                              ⚠ Bypasses quiet hours
                            </div>
                          )}
                        </td>
                        <td style={{ padding: "6px 4px", borderBottom: `1px solid ${t.border}` }}>
                          <select
                            value={readEnabledChoice(ov)}
                            onChange={e => setEventOverride(
                              meta.name,
                              applyEnabledChoice(ov, e.target.value as EnabledChoice),
                            )}
                            style={ist}
                          >
                            <option value="default">Default</option>
                            <option value="on">On</option>
                            <option value="off">Off</option>
                          </select>
                        </td>
                        <td style={{ padding: "6px 4px", borderBottom: `1px solid ${t.border}` }}>
                          <input
                            type="text"
                            placeholder="(default ntfy_topic)"
                            value={ov?.topic ?? ""}
                            onChange={e => setEventOverride(
                              meta.name,
                              applyTopic(ov, e.target.value),
                            )}
                            style={{ ...ist, width: 180 }}
                          />
                        </td>
                        <td style={{ padding: "6px 4px", borderBottom: `1px solid ${t.border}` }}>
                          <select
                            value={ov?.priority === undefined ? "" : String(ov.priority)}
                            onChange={e => setEventOverride(
                              meta.name,
                              applyPriority(ov, e.target.value === "" ? null : parseInt(e.target.value)),
                            )}
                            style={ist}
                          >
                            <option value="">{`Default (${meta.default_priority})`}</option>
                            <option value="1">1</option>
                            <option value="2">2</option>
                            <option value="3">3</option>
                            <option value="4">4</option>
                            <option value="5">5</option>
                          </select>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </>
          )}
        </div>
      </details>
    </div>
  );
}


// ─── Wildcard rows ───────────────────────────────────────────
//
// The user can also configure overrides keyed by wildcards
// (`grab.*`, `*`). These don't appear in the registry-driven table
// since their patterns are arbitrary; this little editor sits above
// the table and lists / removes / adds them.
function WildcardRows({
  events, setEventOverride, ist,
}: {
  events: Record<string, EventOverride>;
  setEventOverride: (name: string, ov: EventOverride | undefined) => void;
  ist: React.CSSProperties;
}) {
  const t = useTheme();
  const [newPattern, setNewPattern] = useState("");
  const wildcardKeys = Object.keys(events).filter(k => k === "*" || k.endsWith(".*"));

  const addPattern = () => {
    const p = newPattern.trim();
    if (!p || events[p]) { setNewPattern(""); return; }
    if (p !== "*" && !p.endsWith(".*")) return;
    setEventOverride(p, {});
    setNewPattern("");
  };

  return (
    <div style={{ border: `1px solid ${t.border}`, borderRadius: 6, padding: 8 }}>
      <div style={{ fontSize: 11, color: t.textDim, marginBottom: 6 }}>
        Wildcard rules: <code>grab.*</code>, <code>discovery.*</code>, <code>*</code> as fallback.
        Exact event overrides below always win over wildcards; longest prefix wins among wildcards.
      </div>
      {wildcardKeys.length === 0 && (
        <div style={{ fontSize: 12, color: t.textDim, fontStyle: "italic" }}>No wildcard rules yet.</div>
      )}
      {wildcardKeys.map(key => {
        const ov = events[key];
        return (
          <div key={key} style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6, flexWrap: "wrap" }}>
            <span style={{ fontFamily: "monospace", fontSize: 12, color: t.text2, minWidth: 120 }}>{key}</span>
            <select
              value={readEnabledChoice(ov)}
              onChange={e => setEventOverride(key, applyEnabledChoice(ov, e.target.value as EnabledChoice))}
              style={ist}
            >
              <option value="default">Default</option>
              <option value="on">On</option>
              <option value="off">Off</option>
            </select>
            <input
              type="text"
              placeholder="topic override"
              value={ov?.topic ?? ""}
              onChange={e => setEventOverride(key, applyTopic(ov, e.target.value))}
              style={{ ...ist, width: 160 }}
            />
            <select
              value={ov?.priority === undefined ? "" : String(ov.priority)}
              onChange={e => setEventOverride(key, applyPriority(ov, e.target.value === "" ? null : parseInt(e.target.value)))}
              style={ist}
            >
              <option value="">Priority: default</option>
              <option value="1">1</option>
              <option value="2">2</option>
              <option value="3">3</option>
              <option value="4">4</option>
              <option value="5">5</option>
            </select>
            <button
              onClick={() => setEventOverride(key, undefined)}
              style={{ fontSize: 11, padding: "3px 8px", background: "transparent", color: t.err, border: `1px solid ${t.border}`, borderRadius: 4, cursor: "pointer" }}
            >
              Remove
            </button>
          </div>
        );
      })}
      <div style={{ display: "flex", gap: 6, alignItems: "center", marginTop: 8 }}>
        <input
          type="text"
          placeholder="e.g. grab.* or *"
          value={newPattern}
          onChange={e => setNewPattern(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter") addPattern(); }}
          style={{ ...ist, width: 180 }}
        />
        <button
          onClick={addPattern}
          style={{ fontSize: 12, padding: "5px 10px", background: t.accent, color: t.bg, border: "none", borderRadius: 4, cursor: "pointer", fontWeight: 600 }}
        >
          Add pattern
        </button>
      </div>
    </div>
  );
}
