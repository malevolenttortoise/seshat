// SettingsPage v4 — sidebar navigation, one section at a time.
import { useEffect, useState, type ReactNode } from "react";
import { Btn } from "../components/Btn";
import { Spin } from "../components/Spin";
import { MetadataSourcesPanel } from "../components/MetadataSourcesPanel";
import { api } from "../api";
import { useTheme } from "../theme";

type S = Record<string, unknown>;

// ── Shared field components ───────────────────────────────────

function SF({ label, desc, example, children, warn, wide }: {
  label: string; desc?: string; example?: string; children: ReactNode; warn?: string; wide?: boolean;
}) {
  const t = useTheme();
  return (
    <div style={{
      display: "grid",
      gridTemplateColumns: wide ? "1fr" : "minmax(0, 1fr) minmax(180px, 320px)",
      alignItems: "center", padding: "14px 0", borderBottom: `1px solid ${t.borderL}`, gap: "6px 16px",
    }}>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 15, fontWeight: 600, color: t.text }}>{label}</div>
        {desc && <div style={{ fontSize: 13, color: t.textDim, marginTop: 3, lineHeight: 1.5 }}>{desc}</div>}
        {example && <div style={{ fontSize: 12, color: t.accent, marginTop: 2, fontStyle: "italic" }}>{example}</div>}
        {warn && <div style={{ fontSize: 12, color: t.warn, marginTop: 3 }}>⚠ {warn}</div>}
      </div>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", minHeight: 32 }}>{children}</div>
    </div>
  );
}

function STog({ on, onToggle, disabled, label }: { on: boolean; onToggle: () => void; disabled?: boolean; label?: boolean }) {
  const t = useTheme();
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      {label && <span style={{ fontSize: 12, color: on ? t.ok : t.textDim, fontWeight: 600 }}>{on ? "ON" : "OFF"}</span>}
      <div onClick={disabled ? undefined : onToggle} style={{
        width: 44, height: 24, borderRadius: 12, background: on ? t.ok : t.bg4,
        cursor: disabled ? "not-allowed" : "pointer", padding: 3,
        transition: "background 0.2s", opacity: disabled ? 0.5 : 1,
      }}>
        <div style={{ width: 18, height: 18, borderRadius: "50%", background: "#fff", transform: on ? "translateX(20px)" : "translateX(0)", transition: "transform 0.2s" }} />
      </div>
    </div>
  );
}

// Drag-reorder list for small ordered string sets (audiobook format
// priority is the first caller; others could follow). Uses native
// HTML5 DnD — no library — matching the Metadata Sources panel.
function FormatPriorityList({ value, onChange }: {
  value: string[];
  onChange: (next: string[]) => void;
}) {
  const t = useTheme();
  const [dragItem, setDragItem] = useState<string | null>(null);
  const [hoverItem, setHoverItem] = useState<string | null>(null);

  const move = (item: string, target: string) => {
    if (item === target) return;
    const next = value.filter(v => v !== item);
    const idx = next.indexOf(target);
    next.splice(idx, 0, item);
    onChange(next);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, width: 280 }}>
      {value.map((fmt, i) => {
        const isDragging = dragItem === fmt;
        const isHover = hoverItem === fmt;
        return (
          <div
            key={fmt}
            draggable
            onDragStart={(e) => { setDragItem(fmt); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", fmt); }}
            onDragOver={(e) => { if (dragItem && dragItem !== fmt) { e.preventDefault(); setHoverItem(fmt); } }}
            onDragLeave={() => setHoverItem(null)}
            onDrop={(e) => { e.preventDefault(); if (dragItem) move(dragItem, fmt); setDragItem(null); setHoverItem(null); }}
            onDragEnd={() => { setDragItem(null); setHoverItem(null); }}
            style={{
              display: "flex", alignItems: "center", gap: 10,
              padding: "8px 12px",
              background: t.bg3,
              border: `1px solid ${t.borderL}`,
              borderTop: isHover ? `2px solid ${t.accent}` : `1px solid ${t.borderL}`,
              borderRadius: 6,
              cursor: "grab",
              opacity: isDragging ? 0.4 : 1,
            }}
          >
            <span style={{ color: t.textDim, fontSize: 14, lineHeight: 1 }}>⋮⋮</span>
            <span style={{ fontSize: 12, color: t.textDim, fontWeight: 600, width: 16, textAlign: "right" }}>{i + 1}</span>
            <span style={{ fontSize: 14, color: t.text, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.4 }}>
              {fmt}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function BadgeList({ items, onEdit, onClear }: { items: string[]; onEdit: () => void; onClear: () => void }) {
  const t = useTheme();
  if (items.length === 0) return <Btn variant="ghost" onClick={onEdit}>Add</Btn>;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
      {items.map(u => (
        <span key={u} style={{ background: t.accent + "22", color: t.accent, padding: "3px 10px", borderRadius: 99, fontSize: 12, fontWeight: 600 }}>{u}</span>
      ))}
      <Btn variant="ghost" onClick={onEdit}>Edit</Btn>
      <Btn variant="danger" onClick={onClear}>Clear</Btn>
    </div>
  );
}

interface CredItem { key: string; label: string; configured: boolean; }

function CredField({ item, desc, onSaved, canGenerate }: { item: CredItem; desc?: string; onSaved: () => void; canGenerate?: boolean }) {
  const t = useTheme();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  async function save() {
    if (!value.trim()) return;
    setBusy(true);
    try { await api.post(`/v1/credentials/${item.key}`, { value: value.trim() }); setEditing(false); setValue(""); onSaved(); }
    catch { /* */ } finally { setBusy(false); }
  }
  function generate() {
    const bytes = new Uint8Array(32);
    crypto.getRandomValues(bytes);
    setValue(Array.from(bytes).map(b => b.toString(16).padStart(2, "0")).join(""));
  }
  return (
    <SF label={item.label} desc={desc || item.key}>
      {item.configured && !editing ? (
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 14, color: t.textDim, letterSpacing: "3px" }}>••••••••</span>
          <Btn variant="ghost" onClick={() => { setEditing(true); setValue(""); }}>Change</Btn>
        </div>
      ) : editing ? (
        <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
          <input type={canGenerate ? "text" : "password"} value={value} onChange={e => setValue(e.target.value)} placeholder={`Enter ${item.label}…`} autoFocus
            style={{ padding: "6px 10px", background: t.inp, border: `1px solid ${t.border}`, borderRadius: 6, color: t.text2, fontSize: 13, width: 200, outline: "none" }} />
          {canGenerate && <Btn variant="ghost" onClick={generate}>Generate</Btn>}
          <Btn variant="primary" onClick={save} disabled={busy || !value.trim()}>{busy ? <Spin size={14} /> : "Save"}</Btn>
          <Btn variant="ghost" onClick={() => { setEditing(false); setValue(""); }}>Cancel</Btn>
        </div>
      ) : (
        <Btn variant="primary" onClick={() => { setEditing(true); setValue(""); }}>Set</Btn>
      )}
    </SF>
  );
}

function NCheck({ label, field, s, upd }: { label: string; field: string; s: S; upd: (k: string, v: unknown) => void }) {
  const t = useTheme();
  const on = (s[field] as boolean) ?? true;
  return (
    <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: t.text2, cursor: "pointer" }}>
      <input type="checkbox" checked={on} onChange={() => upd(field, !on)} style={{ width: 16, height: 16, accentColor: t.ok, cursor: "pointer" }} />
      {label}
    </label>
  );
}

function DataSection() {
  const t = useTheme();
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { api.get<Record<string, number>>("/v1/data/counts").then(setCounts).catch(() => {}); }, []);
  async function clear(target: string, dangerous = false) {
    const label = target.replace(/_/g, " ");
    if (dangerous) { const typed = prompt(`This will permanently delete all ${label}.\nType "${target}" to confirm:`); if (typed !== target) return; }
    else if (!confirm(`Clear all ${label}?`)) return;
    setBusy(true);
    try { const r = await api.post<{ rows_deleted: number }>(`/v1/data/clear/${target}`, dangerous ? { confirm: target } : {}); setMsg(`Cleared ${r.rows_deleted} rows`); const fresh = await api.get<Record<string, number>>("/v1/data/counts"); setCounts(fresh); }
    catch (e) { setMsg(String(e)); } finally { setBusy(false); setTimeout(() => setMsg(""), 4000); }
  }
  const DataRow = ({ target, label, desc, count, dangerous }: { target: string; label: string; desc: string; count: number; dangerous?: boolean }) => (
    <SF label={`${label} (${count})`} desc={desc}>
      <Btn variant={dangerous ? "danger" : "ghost"} onClick={() => clear(target, dangerous)} disabled={busy || count === 0}>{dangerous ? "⚠ Clear" : "Clear"}</Btn>
    </SF>
  );
  return (
    <>
      {msg && <div style={{ fontSize: 12, color: t.ok, marginBottom: 8, fontWeight: 600 }}>✓ {msg}</div>}
      <DataRow target="tentative_torrents" label="Tentative torrents" desc="Captures from unknown authors." count={counts.tentative_torrents ?? 0} />
      <DataRow target="book_review_queue" label="Pending reviews" desc="Downloaded books awaiting approval." count={counts.book_review_queue ?? 0} />
      <DataRow target="ignored_torrents_seen" label="Ignored history" desc="Weekly ignored-author audit trail." count={counts.ignored_torrents_seen ?? 0} />
      <DataRow target="announces" label="Announce log" desc="IRC announce audit trail." count={counts.announces ?? 0} />
      <DataRow target="calibre_additions" label="Calibre additions" desc="Digest reporting counter." count={counts.calibre_additions ?? 0} />
      <DataRow target="authors_allowed" label="Allowed authors" desc="⚠ Clearing removes ALL." count={counts.authors_allowed ?? 0} dangerous />
      <DataRow target="authors_ignored" label="Ignored authors" desc="⚠ They'll reappear as 'new'." count={counts.authors_ignored ?? 0} dangerous />
    </>
  );
}

function QbitTestButton() {
  const t = useTheme();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  async function test() {
    setBusy(true); setResult(null);
    try { const r = await api.post<{ ok: boolean; message: string }>("/v1/mam/test-qbit"); setResult(r.ok ? `✓ ${r.message}` : `✗ ${r.message}`); }
    catch (e) { setResult(`✗ ${e}`); } finally { setBusy(false); setTimeout(() => setResult(null), 8000); }
  }
  return (
    <SF label="Test Connection" desc="Verify URL, username, and password." warn="Some clients ban IPs after repeated failures.">
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <Btn variant="ghost" onClick={test} disabled={busy}>{busy ? <Spin size={14} /> : "Test"}</Btn>
        {result && <span style={{ fontSize: 11, color: result.startsWith("✓") ? t.ok : t.err, fontWeight: 600 }}>{result}</span>}
      </div>
    </SF>
  );
}

const HOURS_24 = Array.from({ length: 24 }, (_, i) => i);
function fmt12(h: number): string { const ampm = h >= 12 ? "PM" : "AM"; const h12 = h === 0 ? 12 : h > 12 ? h - 12 : h; return `${h12}:00 ${ampm}`; }

// ── Section definitions ───────────────────────────────────────

const SECTIONS = [
  { id: "pipeline", label: "Pipeline", group: "Pipeline" },
  { id: "review", label: "Review & Enrichment", group: "Pipeline" },
  { id: "policy", label: "Grab Policy", group: "Pipeline" },
  { id: "budget", label: "Snatch Budget", group: "Pipeline" },
  { id: "mam", label: "MyAnonamouse", group: "Pipeline" },
  { id: "client", label: "Download Client", group: "Pipeline" },
  { id: "sinks", label: "Sinks & Delivery", group: "Pipeline" },
  { id: "notifications", label: "Notifications", group: "Pipeline" },
  { id: "sources", label: "Metadata Sources", group: "Discovery" },
  { id: "scanning", label: "Author Scanning", group: "Discovery" },
  { id: "library", label: "Library Management", group: "Discovery" },
  { id: "audiobookshelf", label: "Audiobookshelf", group: "Discovery" },
  { id: "discmam", label: "Discovery MAM", group: "Discovery" },
  { id: "operational", label: "Operational", group: "Shared" },
  { id: "data", label: "Data Management", group: "Shared" },
];

// ── Main Settings Page ────────────────────────────────────────

export default function SettingsPage() {
  const t = useTheme();
  const [s, setS] = useState<S | null>(null);
  const [creds, setCreds] = useState<CredItem[]>([]);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [section, setSection] = useState("pipeline");
  const [editingUploaders, setEditingUploaders] = useState(false);
  const [uploadersText, setUploadersText] = useState("");
  const [use12h, setUse12h] = useState(false);
  const [testingNtfy, setTestingNtfy] = useState(false);
  const [ntfyResult, setNtfyResult] = useState<string | null>(null);
  const [buildSha, setBuildSha] = useState("");

  useEffect(() => { api.get<S>("/v1/settings").then(setS).catch(e => setMsg(`Error: ${e}`)); }, []);
  const loadCreds = () => api.get<{ items: CredItem[] }>("/v1/credentials").then(r => setCreds(r.items)).catch(() => {});
  useEffect(() => { loadCreds(); }, []);
  useEffect(() => { api.get<{ short_sha: string }>("/version").then(r => setBuildSha(r.short_sha || "")).catch(() => {}); }, []);

  if (!s) return <div style={{ display: "flex", justifyContent: "center", padding: 40 }}><Spin /></div>;

  const upd = (k: string, v: unknown) => setS(o => o ? { ...o, [k]: v } : o);
  const ist = { padding: "7px 12px", background: t.inp, border: `1px solid ${t.border}`, borderRadius: 6, color: t.text2, fontSize: 13, outline: "none" } as const;
  const nist = { ...ist, width: 90, textAlign: "center" as const } as const;

  const save = async () => {
    setSaving(true); setMsg("");
    try { await api.patch("/v1/settings", s); setMsg("Saved!"); const fresh = await api.get<S>("/v1/settings"); setS(fresh); setTimeout(() => setMsg(""), 3000); }
    catch { setMsg("Error saving"); } finally { setSaving(false); }
  };

  const testNtfy = async () => {
    setTestingNtfy(true); setNtfyResult(null);
    try { const r = await api.post<{ ok: boolean; message: string }>("/v1/mam/test-notification"); setNtfyResult(r.ok ? "✓ Sent!" : `✗ ${r.message}`); }
    catch (e) { setNtfyResult(`✗ ${e}`); } finally { setTestingNtfy(false); setTimeout(() => setNtfyResult(null), 5000); }
  };

  const uploaders = ((s.excluded_uploaders as string[]) ?? []);
  const mamCreds = creds.filter(c => ["mam_session_id", "mam_irc_password"].includes(c.key));
  const qbitCreds = creds.filter(c => c.key === "qbit_password");
  const apiCreds = creds.filter(c => c.key === "hardcover_api_key");
  const absCreds = creds.filter(c => c.key === "abs_api_key");

  // Group sections for sidebar
  const groups = ["Pipeline", "Discovery", "Shared"];

  return (
    <div style={{ display: "flex", gap: 0, minHeight: "calc(100vh - 100px)" }}>

      {/* ── Sidebar ── */}
      <div style={{
        width: 220, flexShrink: 0, background: t.bg2, borderRight: `1px solid ${t.border}`,
        borderRadius: "12px 0 0 12px", padding: "16px 0", position: "sticky", top: 60, alignSelf: "flex-start",
      }}>
        {groups.map(g => (
          <div key={g}>
            <div style={{ fontSize: 11, fontWeight: 700, color: t.td, textTransform: "uppercase", letterSpacing: "0.06em", padding: "14px 20px 6px" }}>{g}</div>
            {SECTIONS.filter(sec => sec.group === g).map(sec => (
              <div key={sec.id} onClick={() => setSection(sec.id)} style={{
                padding: "9px 20px", fontSize: 14, cursor: "pointer",
                color: section === sec.id ? t.accent : t.text2,
                background: section === sec.id ? t.abg : "transparent",
                borderLeft: section === sec.id ? `3px solid ${t.accent}` : "3px solid transparent",
                fontWeight: section === sec.id ? 600 : 400,
              }}>{sec.label}</div>
            ))}
          </div>
        ))}
        {buildSha && (
          <div style={{ padding: "16px 20px 8px", fontSize: 10, color: t.tf }}>
            Build: <code style={{ color: t.td }}>{buildSha}</code>
          </div>
        )}
      </div>

      {/* ── Content Panel ── */}
      <div style={{ flex: 1, padding: "20px 32px", minWidth: 0 }}>
        {/* Header */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 20 }}>
          <h1 style={{ fontSize: 22, fontWeight: 700, color: t.text, margin: 0 }}>
            {SECTIONS.find(sec => sec.id === section)?.label || "Settings"}
          </h1>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            {msg && <span style={{ fontSize: 13, fontWeight: 600, color: msg.startsWith("Error") ? t.err : t.ok }}>{msg}</span>}
            {/* Sources section has its own Save button — top-level
                Save would PATCH the whole settings dict and could
                clobber any in-flight panel edits. */}
            {section !== "sources" && (
              <Btn variant="primary" onClick={save} disabled={saving}>{saving ? <Spin size={14} /> : "Save"}</Btn>
            )}
          </div>
        </div>

        {/* ── Section content ── */}

        {section === "pipeline" && <>
          <SF label="IRC Listener" desc="Connects to MAM's #announce channel and processes every new torrent through the filter gate.">
            <STog on={(s.mam_irc_enabled as boolean) ?? true} onToggle={() => upd("mam_irc_enabled", !(s.mam_irc_enabled ?? true))} label />
          </SF>
          <SF label="Download Client Watcher" desc="Polls the download client every 60s to detect completed downloads and reconcile the snatch budget.">
            <STog on={(s.pipeline_qbit_watcher_enabled as boolean) ?? true} onToggle={() => upd("pipeline_qbit_watcher_enabled", !(s.pipeline_qbit_watcher_enabled ?? true))} label />
          </SF>
          <SF label="Auto-Train Authors" desc="When a book is grabbed from a co-author, the other co-authors are automatically added to the allow list.">
            <STog on={(s.pipeline_auto_train_enabled as boolean) ?? true} onToggle={() => upd("pipeline_auto_train_enabled", !(s.pipeline_auto_train_enabled ?? true))} label />
          </SF>
          <SF label="Dry Run" desc="Filter + policy run normally but nothing is downloaded. The announce log records what would have happened." warn={s.dry_run ? "Active — no torrents will be downloaded" : undefined}>
            <STog on={!!s.dry_run} onToggle={() => upd("dry_run", !s.dry_run)} label />
          </SF>
        </>}

        {section === "review" && <>
          <SF label="Manual Review Queue" desc="Every downloaded book enters a review queue for your approval before Calibre delivery.">
            <STog on={(s.review_queue_enabled as boolean) ?? true} onToggle={() => upd("review_queue_enabled", !(s.review_queue_enabled ?? true))} label />
          </SF>
          <SF label="Metadata Enrichment" desc="Scrapes 7 sources for covers, descriptions, series info, ISBN, and page counts.">
            <STog on={!!s.metadata_enrichment_enabled} onToggle={() => upd("metadata_enrichment_enabled", !s.metadata_enrichment_enabled)} label />
          </SF>
          <SF label="Review Timeout" desc="Books undecided for this long are auto-added to Calibre.">
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <input type="number" min={1} value={s.metadata_review_timeout_days as number ?? 14} onChange={e => upd("metadata_review_timeout_days", parseInt(e.target.value) || 14)} style={nist} />
              <span style={{ fontSize: 12, color: t.textDim }}>days</span>
            </div>
          </SF>
        </>}

        {section === "policy" && <>
          <SF label="Always Grab VIP" desc="VIP torrents bypass all other policy checks.">
            <STog on={(s.policy_vip_always_grab as boolean) ?? true} onToggle={() => upd("policy_vip_always_grab", !(s.policy_vip_always_grab ?? true))} label />
          </SF>
          <SF label="Free Only" desc="Only grab free torrents (VIP, global FL, personal FL, or wedge-applied).">
            <STog on={!!s.policy_free_only} onToggle={() => upd("policy_free_only", !s.policy_free_only)} label />
          </SF>
          <SF label="Use Freeleech Wedges" desc="Spend a wedge to make a non-free torrent free.">
            <STog on={!!s.policy_use_wedge} onToggle={() => upd("policy_use_wedge", !s.policy_use_wedge)} label />
          </SF>
          <SF label="Ratio Floor" desc="Skip non-free torrents when your ratio drops below this. 0 = disabled.">
            <input type="number" min={0} step={0.1} value={s.policy_ratio_floor as number ?? 0} onChange={e => upd("policy_ratio_floor", parseFloat(e.target.value) || 0)} style={nist} />
          </SF>
        </>}

        {section === "budget" && <>
          <SF label="Budget Cap" desc="Max active snatches. New grabs queue when full.">
            <input type="number" min={1} value={s.snatch_budget_cap as number ?? 200} onChange={e => upd("snatch_budget_cap", parseInt(e.target.value) || 200)} style={nist} />
          </SF>
          <SF label="Queue Max" desc="Pending queue size before FIFO eviction to delayed folder.">
            <input type="number" min={1} value={s.snatch_queue_max as number ?? 200} onChange={e => upd("snatch_queue_max", parseInt(e.target.value) || 200)} style={nist} />
          </SF>
          <SF label="Excluded Uploaders" desc="MAM usernames whose uploads are never grabbed.">
            {editingUploaders ? (
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <textarea value={uploadersText} onChange={e => setUploadersText(e.target.value)} rows={2} placeholder="One per line" autoFocus style={{ ...ist, width: 180, resize: "vertical" }} />
                <Btn variant="primary" onClick={() => { upd("excluded_uploaders", uploadersText.split("\n").map((x: string) => x.trim()).filter(Boolean)); setEditingUploaders(false); }}>Done</Btn>
                <Btn variant="ghost" onClick={() => setEditingUploaders(false)}>Cancel</Btn>
              </div>
            ) : (
              <BadgeList items={uploaders} onEdit={() => { setUploadersText(uploaders.join("\n")); setEditingUploaders(true); }} onClear={() => upd("excluded_uploaders", [])} />
            )}
          </SF>
          <SF label="Delayed Torrents Path" desc="Overflow folder for FIFO-rotated grabs.">
            <input value={(s.delayed_torrents_path as string) || ""} onChange={e => upd("delayed_torrents_path", e.target.value)} placeholder="/delayed-torrents" style={{ ...ist, width: 220 }} />
          </SF>
        </>}

        {section === "mam" && <>
          <SF label="IRC Nickname" desc="Seshat's nickname on MAM's IRC server.">
            <input value={(s.mam_irc_nick as string) || ""} onChange={e => upd("mam_irc_nick", e.target.value)} placeholder="YourNick_seshat" style={{ ...ist, width: 200 }} />
          </SF>
          <SF label="IRC Account" desc="Your MAM username for SASL authentication.">
            <input value={(s.mam_irc_account as string) || ""} onChange={e => upd("mam_irc_account", e.target.value)} placeholder="YourUsername" style={{ ...ist, width: 200 }} />
          </SF>
          {mamCreds.map(c => <CredField key={c.key} item={c} onSaved={loadCreds} desc={
            c.key === "mam_session_id" ? 'MAM → Preferences → Security → Generate Session.'
            : "Password for SASL authentication."
          } />)}
        </>}

        {section === "client" && <>
          <SF label="Client Type" desc="Which torrent client to connect to.">
            <select value={(s.download_client_type as string) || "qbittorrent"} onChange={e => upd("download_client_type", e.target.value)}
              style={{ ...ist, width: 180, cursor: "pointer", appearance: "auto" }}>
              <option value="qbittorrent">qBittorrent</option>
              <option value="transmission">Transmission</option>
              <option value="deluge">Deluge</option>
              <option value="rtorrent">rTorrent</option>
            </select>
          </SF>
          <SF label="WebUI URL" desc="Full URL to the download client's Web API.">
            <input value={(s.qbit_url as string) || ""} onChange={e => upd("qbit_url", e.target.value)} placeholder="http://10.0.10.20:8180" style={{ ...ist, width: 260 }} />
          </SF>
          <SF label="Username" desc="WebUI login username.">
            <input value={(s.qbit_username as string) || ""} onChange={e => upd("qbit_username", e.target.value)} placeholder="admin" style={{ ...ist, width: 160 }} />
          </SF>
          {qbitCreds.map(c => <CredField key={c.key} item={c} onSaved={loadCreds} desc="WebUI login password." />)}
          <QbitTestButton />
          <SF label="Watch Category" desc="Torrent category that Seshat manages.">
            <input value={(s.qbit_watch_category as string) || "[mam-reseed]"} onChange={e => upd("qbit_watch_category", e.target.value)} style={{ ...ist, width: 180 }} />
          </SF>
          <SF label="Download Path" desc="Base download directory as seen by the download client.">
            <input value={(s.qbit_download_path as string) || ""} onChange={e => upd("qbit_download_path", e.target.value)} placeholder="/data/[mam-complete]" style={{ ...ist, width: 260 }} />
          </SF>
          <SF label="Folder Structure" desc="How downloads are organized inside the download path.">
            <select value={(s.download_folder_structure as string) || "monthly"} onChange={e => upd("download_folder_structure", e.target.value)}
              style={{ ...ist, width: 190, cursor: "pointer", appearance: "auto" }}>
              <option value="monthly">[YYYY-MM] Monthly</option>
              <option value="yearly">[YYYY] Yearly</option>
              <option value="author">By Author</option>
              <option value="flat">Flat (no subfolders)</option>
            </select>
          </SF>
        </>}

        {section === "sinks" && <>
          {apiCreds.map(c => <CredField key={c.key} item={c} onSaved={loadCreds} desc="Bearer token from hardcover.app → Account → API." />)}
          <SF label="Default Sink" desc="Where approved books are delivered after review.">
            <select value={(s.default_sink as string) || "cwa"} onChange={e => upd("default_sink", e.target.value)}
              style={{ ...ist, width: 260, cursor: "pointer", appearance: "auto" }}>
              <option value="cwa">CWA — auto-import via ingest folder</option>
              <option value="calibre">Calibre — direct calibredb add</option>
              <option value="folder">Folder — copy to a directory</option>
              <option value="audiobookshelf">Audiobookshelf — library folder</option>
            </select>
          </SF>
          <SF label="CWA Web URL" desc="Calibre-Web Automated web interface URL.">
            <input value={(s.cwa_web_url as string) || ""} onChange={e => upd("cwa_web_url", e.target.value)} placeholder="http://host:port" style={{ ...ist, width: 260 }} />
          </SF>
          <SF label="Calibre Web URL" desc="Calibre Content Server web interface URL.">
            <input value={(s.calibre_web_url as string) || ""} onChange={e => upd("calibre_web_url", e.target.value)} placeholder="http://host:port" style={{ ...ist, width: 260 }} />
          </SF>
          <SF label="Emergency Export Path" desc="Fallback folder when sink is unreachable.">
            <input value={(s.emergency_export_path as string) || ""} onChange={e => upd("emergency_export_path", e.target.value)} placeholder="/emergency-books" style={{ ...ist, width: 220 }} />
          </SF>
          <SF label="Sink Max Retries" desc="Retries before exporting to emergency folder.">
            <input type="number" min={1} value={s.sink_max_retries as number ?? 3} onChange={e => upd("sink_max_retries", parseInt(e.target.value) || 3)} style={nist} />
          </SF>
        </>}

        {section === "notifications" && <>
          <SF label="ntfy Server URL" desc="URL of your ntfy server.">
            <input value={(s.ntfy_url as string) || ""} onChange={e => upd("ntfy_url", e.target.value)} placeholder="https://ntfy.sh" style={{ ...ist, width: 300 }} />
          </SF>
          <SF label="ntfy Topic" desc="Topic name to publish to.">
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <input value={(s.ntfy_topic as string) || "seshat"} onChange={e => upd("ntfy_topic", e.target.value)} style={{ ...ist, width: 140 }} />
              <Btn variant="ghost" onClick={testNtfy} disabled={testingNtfy}>{testingNtfy ? <Spin size={14} /> : "Test"}</Btn>
              {ntfyResult && <span style={{ fontSize: 11, color: ntfyResult.startsWith("✓") ? t.ok : t.err, fontWeight: 600 }}>{ntfyResult}</span>}
            </div>
          </SF>
          <SF label="Pipeline Events" desc="Which pipeline events trigger a push notification." wide>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px 32px", marginTop: 8 }}>
              <NCheck label="New book grabbed" field="notify_on_grab" s={s} upd={upd} />
              <NCheck label="Download completed" field="notify_on_download_complete" s={s} upd={upd} />
              <NCheck label="Pipeline errors" field="notify_on_pipeline_error" s={s} upd={upd} />
              <NCheck label="Daily — accepted" field="notify_daily_accepted" s={s} upd={upd} />
              <NCheck label="Daily — tentative" field="notify_daily_tentative" s={s} upd={upd} />
              <NCheck label="Daily — ignored" field="notify_daily_ignored" s={s} upd={upd} />
              <NCheck label="Weekly digest" field="notify_weekly_digest" s={s} upd={upd} />
            </div>
          </SF>
          <SF label="Discovery Events" desc="Which discovery events trigger a push notification." wide>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px 32px", marginTop: 8 }}>
              <NCheck label="Source scan complete" field="ntfy_on_scan_complete" s={s} upd={upd} />
              <NCheck label="New books found" field="ntfy_on_new_books" s={s} upd={upd} />
              <NCheck label="MAM scan complete" field="ntfy_on_mam_complete" s={s} upd={upd} />
              <NCheck label="Sent to pipeline" field="ntfy_on_pipeline_sent" s={s} upd={upd} />
              <NCheck label="Library sync" field="ntfy_on_library_sync" s={s} upd={upd} />
              <NCheck label="MAM cookie rotated" field="ntfy_on_mam_cookie_rotated" s={s} upd={upd} />
            </div>
          </SF>
          <SF label="Digest Hour" desc="When the daily digest fires.">
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <select value={s.daily_digest_hour as number ?? 9} onChange={e => upd("daily_digest_hour", parseInt(e.target.value))}
                style={{ ...ist, width: use12h ? 120 : 70, cursor: "pointer", appearance: "auto" }}>
                {HOURS_24.map(h => <option key={h} value={h}>{use12h ? fmt12(h) : `${String(h).padStart(2, "0")}:00`}</option>)}
              </select>
              <button onClick={() => setUse12h(!use12h)} style={{ background: "none", border: "none", color: t.accent, cursor: "pointer", fontSize: 11, fontWeight: 600 }}>{use12h ? "24h" : "12h"}</button>
            </div>
          </SF>
        </>}

        {section === "sources" && <MetadataSourcesPanel />}

        {section === "scanning" && <>
          <SF label="Auto-scan Enabled" desc="Periodically scan all authors against enabled sources.">
            <STog on={(s.author_scanning_enabled as boolean) ?? true} onToggle={() => upd("author_scanning_enabled", !(s.author_scanning_enabled ?? true))} label />
          </SF>
          <SF label="Owned Books Only" desc="Only enrich metadata on books already in Calibre.">
            <STog on={(s.author_scan_owned_only as boolean) ?? false} onToggle={() => upd("author_scan_owned_only", !(s.author_scan_owned_only ?? false))} label />
          </SF>
          <SF label="Exclude Audiobooks" desc="Filter out audiobook-only editions during scans.">
            <STog on={(s.exclude_audiobooks as boolean) ?? true} onToggle={() => upd("exclude_audiobooks", !(s.exclude_audiobooks ?? true))} label />
          </SF>
          <SF label="Lookup Interval" desc="How often the scheduled author scan runs. 0 = manual only.">
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <input type="number" min={0} value={s.lookup_interval_days as number ?? 3} onChange={e => upd("lookup_interval_days", parseInt(e.target.value) || 3)} style={nist} />
              <span style={{ fontSize: 12, color: t.textDim }}>days</span>
            </div>
          </SF>
        </>}

        {section === "library" && <LibrarySection s={s} upd={upd} ist={ist} nist={nist} />}

        {section === "audiobookshelf" && <AudiobookshelfSection s={s} upd={upd} ist={ist} creds={absCreds} onCredSaved={loadCreds} />}

        {section === "discmam" && <DiscMamSection s={s} upd={upd} ist={ist} nist={nist} />}


        {section === "operational" && <>
          <SF label="Verbose Logging" desc="Enable DEBUG-level output.">
            <STog on={!!s.verbose_logging} onToggle={() => upd("verbose_logging", !s.verbose_logging)} label />
          </SF>
          <SF label="Theme" desc="Use the theme toggle in the navbar to switch between Dark, Dim, and Light.">
            <span style={{ fontSize: 13, color: t.textDim }}>Managed via navbar toggle</span>
          </SF>
        </>}

        {section === "data" && <>
          <p style={{ fontSize: 12, color: t.textDim, marginBottom: 12, lineHeight: 1.5 }}>
            Safe operations clear data that rebuilds from future announces. Dangerous operations (⚠) require typed confirmation.
          </p>
          <DataSection />
        </>}
      </div>
    </div>
  );
}

// ── Library Management Section ────────────────────────────────

function LibrarySection({ s, upd, ist, nist }: { s: S; upd: (k: string, v: unknown) => void; ist: any; nist: any }) {
  const t = useTheme();
  const [libs, setLibs] = useState<any[]>([]);
  const [rescanning, setRescanning] = useState(false);
  const [activeLib, setActiveLib] = useState("");

  useEffect(() => {
    api.get<{ libraries: any[]; active: string }>("/discovery/libraries").then(r => {
      setLibs(r.libraries || []);
      setActiveLib(r.active || "");
    }).catch(() => {});
  }, []);

  const rescan = async () => {
    setRescanning(true);
    try {
      const r = await api.post<{ libraries: any[] }>("/discovery/libraries/rescan");
      setLibs(r.libraries || []);
    } catch {} finally { setRescanning(false); }
  };

  const switchLib = async (slug: string) => {
    try {
      await api.post("/discovery/libraries/active", { slug });
      setActiveLib(slug);
    } catch {}
  };

  return <>
    {/* Discovered Libraries */}
    <div style={{ marginBottom: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
        <div style={{ fontSize: 14, fontWeight: 600, color: t.text }}>Discovered Libraries</div>
        <Btn variant="ghost" onClick={rescan} disabled={rescanning}>{rescanning ? <Spin size={14} /> : "Rescan"}</Btn>
      </div>
      {libs.length === 0 ? (
        <div style={{ fontSize: 13, color: t.textDim, fontStyle: "italic" }}>No libraries discovered. Check CALIBRE_PATH volume mount.</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {libs.map(lib => (
            <div key={lib.slug} onClick={() => switchLib(lib.slug)} style={{
              display: "flex", justifyContent: "space-between", alignItems: "center",
              padding: "10px 14px", borderRadius: 8, cursor: "pointer",
              background: lib.slug === activeLib ? t.abg : t.bg3,
              border: `1px solid ${lib.slug === activeLib ? t.accent : t.borderL}`,
            }}>
              <div>
                <div style={{ fontSize: 14, fontWeight: 600, color: lib.slug === activeLib ? t.accent : t.text }}>{lib.name}</div>
                <div style={{ fontSize: 11, color: t.textDim }}>{lib.display_name} · {lib.content_type} · {lib.slug}</div>
              </div>
              {lib.slug === activeLib && <span style={{ fontSize: 11, fontWeight: 600, color: t.ok }}>Active</span>}
            </div>
          ))}
        </div>
      )}
    </div>

    <SF label="Sync Interval" desc="How often to check Calibre's metadata.db for changes. 0 = manual only.">
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <input type="number" min={0} value={s.library_sync_interval_minutes as number ?? 60} onChange={e => upd("library_sync_interval_minutes", parseInt(e.target.value) || 60)} style={nist} />
        <span style={{ fontSize: 13, color: t.textDim }}>min</span>
      </div>
    </SF>
    <SF label="Languages" desc="Comma-separated language filter for source scans." wide>
      <input value={((s.languages as string[]) ?? []).join(", ")} onChange={e => upd("languages", e.target.value.split(",").map((x: string) => x.trim()).filter(Boolean))} placeholder="English" style={{ ...ist, width: "100%" }} />
    </SF>
    <SF label="Calibre Web URL" desc="Calibre-Web interface for dashboard quick-launch.">
      <input value={(s.calibre_web_url as string) || ""} onChange={e => upd("calibre_web_url", e.target.value)} placeholder="http://host:port" style={{ ...ist, width: 260 }} />
    </SF>
    <SF label="Calibre Content Server URL" desc="Calibre Content Server for direct library access.">
      <input value={(s.calibre_url as string) || ""} onChange={e => upd("calibre_url", e.target.value)} placeholder="http://host:port" style={{ ...ist, width: 260 }} />
    </SF>
  </>;
}

// ── Audiobookshelf Section ────────────────────────────────────

interface AbsLibrary { id: string; name: string; mediaType?: string; folders?: { fullPath: string }[]; lastUpdate?: number; }

function AudiobookshelfSection({ s, upd, ist, creds, onCredSaved }: {
  s: S; upd: (k: string, v: unknown) => void; ist: any;
  creds: CredItem[]; onCredSaved: () => void;
}) {
  const t = useTheme();
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [libs, setLibs] = useState<AbsLibrary[] | null>(null);
  const [rebuilding, setRebuilding] = useState(false);
  const [rebuildResult, setRebuildResult] = useState<string | null>(null);

  const apiKeyConfigured = creds.some(c => c.key === "abs_api_key" && c.configured);
  const url = (s.abs_url as string) || "";

  const testConnection = async () => {
    setTesting(true); setTestResult(null); setLibs(null);
    try {
      const r = await api.post<{ ok: boolean; libraries?: AbsLibrary[]; error?: string }>(
        "/discovery/audiobookshelf/test",
      );
      if (r.ok) {
        setLibs(r.libraries || []);
        setTestResult(`✓ Connected — found ${(r.libraries || []).length} library/libraries`);
      } else {
        setTestResult(`✗ ${r.error || "Connection failed"}`);
      }
    } catch (e: any) { setTestResult(`✗ ${e.message || String(e)}`); }
    finally { setTesting(false); setTimeout(() => setTestResult(null), 10000); }
  };

  const rebuildWorks = async () => {
    setRebuilding(true); setRebuildResult(null);
    try {
      const r = await api.post<{
        works_created: number; links_added: number;
        stale_auto_removed: number; orphans_pruned: number;
      }>("/v1/works/rebuild");
      setRebuildResult(
        `✓ ${r.links_added} links added, ${r.works_created} new works, ` +
        `${r.stale_auto_removed} stale cleared, ${r.orphans_pruned} orphans pruned`,
      );
    } catch (e: any) { setRebuildResult(`✗ ${e.message || String(e)}`); }
    finally { setRebuilding(false); setTimeout(() => setRebuildResult(null), 10000); }
  };

  return <>
    <p style={{ fontSize: 12, color: t.textDim, marginBottom: 12, lineHeight: 1.5 }}>
      Audiobookshelf pairs with your Calibre library as a second content source.
      Seshat discovers audiobooks via the ABS REST API, syncs them into a
      per-library discovery DB, and auto-links ebook ↔ audiobook pairs into
      cross-library "works".
    </p>

    <SF label="ABS Base URL" desc="Address Seshat's backend uses to talk to ABS's API. For most setups (ABS reachable at the same IP:port from both Seshat and your browser) this is the same as Web URL below." example="e.g. http://10.0.10.20:13378">
      <input
        value={url}
        onChange={e => upd("abs_url", e.target.value.trim())}
        placeholder="http://10.0.10.20:13378"
        style={{ ...ist, width: 280 }}
      />
    </SF>

    <SF label="ABS Web URL" desc="Address the dashboard opens in a new tab. Only differs from Base URL when the browser uses a different hostname than the Seshat container does (e.g. public DNS vs. Docker network name).">
      <input
        value={(s.abs_web_url as string) || ""}
        onChange={e => upd("abs_web_url", e.target.value.trim())}
        placeholder="http://10.0.10.20:13378"
        style={{ ...ist, width: 280 }}
      />
    </SF>

    {creds.map(c => (
      <CredField
        key={c.key}
        item={c}
        onSaved={onCredSaved}
        desc="Bearer token from ABS → Settings → Users → [your user] → API Token."
      />
    ))}

    <SF label="Test Connection" desc="Hits /api/libraries and lists discovered book libraries.">
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <Btn
          variant="ghost"
          onClick={testConnection}
          disabled={testing || !url || !apiKeyConfigured}
        >{testing ? <Spin size={14} /> : "Test"}</Btn>
        {testResult && <span style={{
          fontSize: 12, color: testResult.startsWith("✓") ? t.ok : t.err, fontWeight: 600,
        }}>{testResult}</span>}
        {!apiKeyConfigured && <span style={{ fontSize: 11, color: t.textDim }}>(set API token above)</span>}
      </div>
    </SF>

    {libs && libs.length > 0 && (
      <div style={{
        marginTop: 8, marginBottom: 8, padding: 12,
        background: t.bg3, borderRadius: 8, border: `1px solid ${t.borderL}`,
      }}>
        <div style={{ fontSize: 12, fontWeight: 600, color: t.text2, marginBottom: 6 }}>
          ABS Libraries
        </div>
        {libs.map(lib => (
          <div key={lib.id} style={{ fontSize: 12, padding: "4px 0", color: t.text2 }}>
            <span style={{ fontWeight: 600 }}>{lib.name}</span>
            <span style={{ color: t.textDim, marginLeft: 8 }}>
              {lib.mediaType} · {(lib.folders || []).map(f => f.fullPath).join(", ")}
            </span>
            <button
              onClick={() => upd("abs_sink_library_id", lib.id)}
              style={{
                marginLeft: 12, fontSize: 11, padding: "2px 8px",
                background: (s.abs_sink_library_id === lib.id) ? t.accent + "22" : t.bg2,
                color: (s.abs_sink_library_id === lib.id) ? t.accent : t.textDim,
                border: `1px solid ${(s.abs_sink_library_id === lib.id) ? t.accent : t.border}`,
                borderRadius: 4, cursor: "pointer", fontWeight: 600,
              }}
            >
              {(s.abs_sink_library_id === lib.id) ? "✓ Sink target" : "Use as sink"}
            </button>
          </div>
        ))}
      </div>
    )}

    <SF
      label="Audiobook Sink Path"
      desc="Container-local path where Seshat drops new audiobook files. Must match the folder ABS watches for its sink-target library (see Use-as-sink above)."
      example="e.g. /audiobooks (with a docker volume mount to /mnt/user/my-content/audiobooks on the host)"
    >
      <input
        value={(s.audiobookshelf_library_path as string) || ""}
        onChange={e => upd("audiobookshelf_library_path", e.target.value.trim())}
        placeholder="/audiobooks"
        style={{ ...ist, width: 280 }}
      />
    </SF>

    <SF
      label="Sink Library ID"
      desc="Which ABS library the audiobook sink delivers into. Click 'Use as sink' above after testing, or paste a library UUID."
    >
      <input
        value={(s.abs_sink_library_id as string) || ""}
        onChange={e => upd("abs_sink_library_id", e.target.value.trim())}
        placeholder="UUID from Test above"
        style={{ ...ist, width: 280, fontFamily: "monospace", fontSize: 11 }}
      />
    </SF>

    <SF
      label="Audiobook Tracking Mode"
      desc="Default for all authors — Works UI lets you override per-author. 'Both' treats owning either format as satisfied."
    >
      <select
        value={(s.audiobook_tracking_mode as string) || "both"}
        onChange={e => upd("audiobook_tracking_mode", e.target.value)}
        style={{ ...ist, width: 180, cursor: "pointer", appearance: "auto" }}
      >
        <option value="both">Both (either format satisfies)</option>
        <option value="ebook">Ebook only</option>
        <option value="audiobook">Audiobook only</option>
      </select>
    </SF>

    <SF
      label="Audiobook Format Priority"
      desc="When a torrent contains multiple audio formats (rare), the pipeline picks the primary file from the format listed earliest. Drag to reorder."
      example="m4b = chapterized single-file · m4a = single-file no chapters · mp3 = multi-part legacy"
      wide
    >
      <FormatPriorityList
        value={(s.audiobook_format_priority as string[]) || ["m4b", "m4a", "mp3"]}
        onChange={(next) => upd("audiobook_format_priority", next)}
      />
    </SF>

    <SF
      label="Audible Region"
      desc="Controls which Audible TLD catalog searches hit. Audnexus regional data follows the same code."
    >
      <select
        value={(s.audible_region as string) || "us"}
        onChange={e => upd("audible_region", e.target.value)}
        style={{ ...ist, width: 180, cursor: "pointer", appearance: "auto" }}
      >
        <option value="us">us — .com (default)</option>
        <option value="uk">uk — .co.uk</option>
        <option value="ca">ca — .ca</option>
        <option value="au">au — .com.au</option>
        <option value="de">de — .de</option>
        <option value="fr">fr — .fr</option>
        <option value="it">it — .it</option>
        <option value="es">es — .es</option>
        <option value="jp">jp — .co.jp</option>
        <option value="in">in — .in</option>
      </select>
    </SF>

    <SF
      label="Discovery Audible Source"
      desc="Use Audible + Audnexus when scanning audiobook libraries for an author's catalog."
    >
      <STog
        on={(s.audible_enabled as boolean) ?? true}
        onToggle={() => upd("audible_enabled", !(s.audible_enabled ?? true))}
        label
      />
    </SF>

    <SF
      label="Rebuild Cross-Library Links"
      desc="Re-run the matcher across every discovered library. Safe at any time — manual links are preserved."
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <Btn variant="ghost" onClick={rebuildWorks} disabled={rebuilding}>
          {rebuilding ? <Spin size={14} /> : "Rebuild"}
        </Btn>
        {rebuildResult && <span style={{
          fontSize: 12, color: rebuildResult.startsWith("✓") ? t.ok : t.err, fontWeight: 600,
        }}>{rebuildResult}</span>}
      </div>
    </SF>
  </>;
}

// ── Discovery MAM Section ─────────────────────────────────────

function DiscMamSection({ s, upd, ist, nist }: { s: S; upd: (k: string, v: unknown) => void; ist: any; nist: any }) {
  const t = useTheme();
  const [validating, setValidating] = useState(false);
  const [valResult, setValResult] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<any>(null);

  const validate = async () => {
    setValidating(true); setValResult(null);
    try {
      const r = await api.post<{ ok: boolean; message?: string; error?: string }>("/discovery/mam/validate");
      setValResult(r.ok ? "✓ Connection valid" : `✗ ${r.error || r.message || "Failed"}`);
    } catch (e: any) { setValResult(`✗ ${e.message || e}`); }
    finally { setValidating(false); setTimeout(() => setValResult(null), 8000); }
  };

  const testScan = async () => {
    setTesting(true); setTestResult(null);
    try {
      const r = await api.post("/discovery/mam/test-scan");
      setTestResult(r);
    } catch (e: any) { setTestResult({ error: e.message || String(e) }); }
    finally { setTesting(false); }
  };

  return <>
    <SF label="MAM Search Enabled" desc="Enable searching MyAnonamouse for books your library is missing.">
      <STog on={(s.mam_enabled as boolean) ?? false} onToggle={() => upd("mam_enabled", !(s.mam_enabled ?? false))} label />
    </SF>
    <SF label="Validate Connection" desc="Test that the MAM session cookie is valid and can search.">
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <Btn variant="ghost" onClick={validate} disabled={validating}>{validating ? <Spin size={14} /> : "Validate"}</Btn>
        {valResult && <span style={{ fontSize: 12, color: valResult.startsWith("✓") ? t.ok : t.err, fontWeight: 600 }}>{valResult}</span>}
      </div>
    </SF>
    <SF label="Auto-scan Enabled" desc="Periodically batch-scan unscanned books against MAM.">
      <STog on={(s.mam_scanning_enabled as boolean) ?? true} onToggle={() => upd("mam_scanning_enabled", !(s.mam_scanning_enabled ?? true))} label />
    </SF>
    <SF label="Scan Interval" desc="How often the MAM batch scanner runs.">
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <input type="number" min={0} value={s.mam_scan_interval_minutes as number ?? 360} onChange={e => upd("mam_scan_interval_minutes", parseInt(e.target.value) || 360)} style={nist} />
        <span style={{ fontSize: 13, color: t.textDim }}>min</span>
      </div>
    </SF>
    <SF label="Format Priority" desc="Drag formats to reorder. Highest priority format is preferred when multiple are available." wide>
      <FormatPriority formats={(s.mam_format_priority as string[]) ?? ["epub", "azw", "azw3", "pdf", "djvu", "azw4"]} onChange={(v: string[]) => upd("mam_format_priority", v)} />
    </SF>
    <SF label="Test Scan" desc="Run a quick test scan on 10 books to verify MAM integration.">
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <Btn variant="ghost" onClick={testScan} disabled={testing}>{testing ? <Spin size={14} /> : "Run Test (10 books)"}</Btn>
      </div>
    </SF>
    {testResult && (
      <div style={{ padding: "10px 0", fontSize: 13 }}>
        {testResult.error ? (
          <div style={{ color: t.err }}>{testResult.error}</div>
        ) : (
          <div style={{ display: "flex", gap: 16, color: t.text2 }}>
            <span>Scanned: <b>{testResult.scanned ?? 0}</b></span>
            <span style={{ color: t.ok }}>Found: <b>{testResult.found ?? 0}</b></span>
            <span style={{ color: t.ylw }}>Possible: <b>{testResult.possible ?? 0}</b></span>
            <span style={{ color: t.err }}>Not found: <b>{testResult.not_found ?? 0}</b></span>
          </div>
        )}
      </div>
    )}
  </>;
}

// ── Format Priority (reorderable) ─────────────────────────────

function FormatPriority({ formats, onChange }: { formats: string[]; onChange: (v: string[]) => void }) {
  const t = useTheme();
  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= formats.length) return;
    const next = [...formats];
    [next[i], next[j]] = [next[j], next[i]];
    onChange(next);
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 4 }}>
      {formats.map((fmt, i) => (
        <div key={fmt} style={{
          display: "flex", alignItems: "center", gap: 8,
          padding: "6px 12px", borderRadius: 6,
          background: i === 0 ? t.abg : t.bg3,
          border: `1px solid ${i === 0 ? t.abr : t.borderL}`,
        }}>
          <span style={{ fontSize: 12, fontWeight: 700, color: i === 0 ? t.accent : t.td, width: 20 }}>{i + 1}.</span>
          <span style={{ fontSize: 14, fontWeight: 500, color: i === 0 ? t.accent : t.text2, flex: 1 }}>{fmt}</span>
          <button onClick={() => move(i, -1)} disabled={i === 0} style={{
            background: "none", border: "none", cursor: i === 0 ? "default" : "pointer",
            color: i === 0 ? t.tg : t.td, fontSize: 14, padding: "0 4px", opacity: i === 0 ? 0.3 : 1,
          }}>▲</button>
          <button onClick={() => move(i, 1)} disabled={i === formats.length - 1} style={{
            background: "none", border: "none", cursor: i === formats.length - 1 ? "default" : "pointer",
            color: i === formats.length - 1 ? t.tg : t.td, fontSize: 14, padding: "0 4px", opacity: i === formats.length - 1 ? 0.3 : 1,
          }}>▼</button>
        </div>
      ))}
    </div>
  );
}

