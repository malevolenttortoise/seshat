// SettingsPage v3 — comprehensive, well-described, two-column layout.
import { useEffect, useState, type ReactNode } from "react";
import { Btn } from "../components/Btn";
import { Spin } from "../components/Spin";
import { api } from "../api";
import { useTheme } from "../theme";

type S = Record<string, unknown>;

// ── Shared settings components ──────────────────────────────────

function SSection({ title, desc, defaultOpen = true, children }: {
  title: string; desc?: string; defaultOpen?: boolean; children: ReactNode;
}) {
  const t = useTheme();
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div style={{ background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 12, marginBottom: 16 }}>
      <div onClick={() => setOpen(!open)} style={{
        display: "flex", alignItems: "center", gap: 10, padding: "16px 24px",
        cursor: "pointer", userSelect: "none",
      }}>
        <span style={{ transform: open ? "rotate(0)" : "rotate(-90deg)", transition: "transform 0.2s", fontSize: 12, color: t.textDim }}>▼</span>
        <div style={{ flex: 1 }}>
          <span style={{ fontSize: 14, fontWeight: 600, color: t.text, textTransform: "uppercase", letterSpacing: "0.04em" }}>{title}</span>
          {desc && <span style={{ fontSize: 12, color: t.textDim, marginLeft: 12 }}>{desc}</span>}
        </div>
      </div>
      {open && <div style={{ padding: "0 24px 20px" }}>{children}</div>}
    </div>
  );
}

function SF({ label, desc, example, children, warn, wide }: {
  label: string; desc?: string; example?: string; children: ReactNode; warn?: string; wide?: boolean;
}) {
  const t = useTheme();
  return (
    <div style={{
      display: "grid",
      gridTemplateColumns: wide ? "1fr" : "minmax(0, 1fr) minmax(180px, 300px)",
      alignItems: "center", padding: "14px 0", borderBottom: `1px solid ${t.borderL}`, gap: "6px 16px",
    }}>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 14, fontWeight: 600, color: t.text }}>{label}</div>
        {desc && <div style={{ fontSize: 12, color: t.textDim, marginTop: 3, lineHeight: 1.5 }}>{desc}</div>}
        {example && <div style={{ fontSize: 11, color: t.accent, marginTop: 2, fontStyle: "italic" }}>{example}</div>}
        {warn && <div style={{ fontSize: 11, color: t.warn, marginTop: 3 }}>⚠ {warn}</div>}
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
  // 64-char hex (32 random bytes). Used for the AthenaScout shared
  // API key — the user clicks Generate, copies the visible value,
  // then Save. After saving it becomes write-only like the other
  // credentials.
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
            style={{ padding: "6px 10px", background: t.inp, border: `1px solid ${t.border}`, borderRadius: 6, color: t.text2, fontSize: 13, width: 200, outline: "none", fontFamily: canGenerate ? "ui-monospace, SFMono-Regular, Consolas, monospace" : undefined }} />
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

function DataSection() {
  const t = useTheme();
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { api.get<Record<string, number>>("/v1/data/counts").then(setCounts).catch(() => {}); }, []);
  async function clear(target: string, dangerous = false) {
    const label = target.replace(/_/g, " ");
    if (dangerous) { const typed = prompt(`This will permanently delete all ${label}.\nType "${target}" to confirm:`); if (typed !== target) return; }
    else if (!confirm(`Clear all ${label}? This can be re-populated from future announces.`)) return;
    setBusy(true);
    try { const r = await api.post<{ rows_deleted: number }>(`/v1/data/clear/${target}`, dangerous ? { confirm: target } : {}); setMsg(`Cleared ${r.rows_deleted} ${label} rows`); const fresh = await api.get<Record<string, number>>("/v1/data/counts"); setCounts(fresh); }
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
      <DataRow target="tentative_torrents" label="Tentative torrents" desc="Captures from unknown authors. Safe — rebuilds from IRC announces." count={counts.tentative_torrents ?? 0} />
      <DataRow target="book_review_queue" label="Pending reviews" desc="Downloaded books awaiting approval. Books stay in staging." count={counts.book_review_queue ?? 0} />
      <DataRow target="ignored_torrents_seen" label="Ignored history" desc="Weekly ignored-author audit trail. Rebuilds each week." count={counts.ignored_torrents_seen ?? 0} />
      <DataRow target="announces" label="Announce log" desc="IRC announce audit trail. Only affects log history." count={counts.announces ?? 0} />
      <DataRow target="calibre_additions" label="Calibre additions" desc="Digest reporting counter. Only affects summaries." count={counts.calibre_additions ?? 0} />
      <DataRow target="authors_allowed" label="Allowed authors" desc="Your curated allow list. ⚠ Clearing removes ALL — you'll need to re-import." count={counts.authors_allowed ?? 0} dangerous />
      <DataRow target="authors_ignored" label="Ignored authors" desc="Authors you've decided to skip. ⚠ They'll reappear as 'new' after clearing." count={counts.authors_ignored ?? 0} dangerous />
    </>
  );
}

const HOURS_24 = Array.from({ length: 24 }, (_, i) => i);
function fmt12(h: number): string { const ampm = h >= 12 ? "PM" : "AM"; const h12 = h === 0 ? 12 : h > 12 ? h - 12 : h; return `${h12}:00 ${ampm}`; }

// ── Main Settings Page ─────────────────────────────────────────

export default function SettingsPage() {
  const t = useTheme();
  const [s, setS] = useState<S | null>(null);
  const [creds, setCreds] = useState<CredItem[]>([]);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [editingUploaders, setEditingUploaders] = useState(false);
  const [uploadersText, setUploadersText] = useState("");
  const [use12h, setUse12h] = useState(false);
  const [testingNtfy, setTestingNtfy] = useState(false);
  const [ntfyResult, setNtfyResult] = useState<string | null>(null);
  // Build SHA — baked into the image at Docker build time via the
  // GIT_SHA build-arg. Standalone/dev runs return "unknown".
  const [buildSha, setBuildSha] = useState("");

  useEffect(() => { api.get<S>("/v1/settings").then(setS).catch(e => setMsg(`Error loading settings: ${e}`)); }, []);
  const loadCreds = () => api.get<{ items: CredItem[] }>("/v1/credentials").then(r => setCreds(r.items)).catch(() => {});
  useEffect(() => { loadCreds(); }, []);
  useEffect(() => { api.get<{ short_sha: string }>("/version").then(r => setBuildSha(r.short_sha || "")).catch(() => {}); }, []);

  if (!s) return <div style={{ display: "flex", justifyContent: "center", padding: 40 }}><Spin /></div>;

  const upd = (k: string, v: unknown) => setS(o => o ? { ...o, [k]: v } : o);
  const ist = { padding: "7px 12px", background: t.inp, border: `1px solid ${t.border}`, borderRadius: 6, color: t.text2, fontSize: 13, outline: "none" } as const;
  // Number inputs: wider box so the value text and spinner arrows don't overlap.
  const nist = { ...ist, width: 90, textAlign: "center" as const } as const;

  const save = async () => {
    setSaving(true); setMsg("");
    try { await api.patch("/v1/settings", s); setMsg("Settings saved!"); const fresh = await api.get<S>("/v1/settings"); setS(fresh); setTimeout(() => setMsg(""), 3000); }
    catch { setMsg("Error saving"); }
    finally { setSaving(false); }
  };

  const testNtfy = async () => {
    setTestingNtfy(true); setNtfyResult(null);
    try { const r = await api.post<{ ok: boolean; message: string }>("/v1/mam/test-notification"); setNtfyResult(r.ok ? "✓ Test sent!" : `✗ ${r.message}`); }
    catch (e) { setNtfyResult(`✗ ${e}`); } finally { setTestingNtfy(false); setTimeout(() => setNtfyResult(null), 5000); }
  };

  const uploaders = ((s.excluded_uploaders as string[]) ?? []);
  const mamCreds = creds.filter(c => ["mam_session_id", "mam_irc_password"].includes(c.key));
  const qbitCreds = creds.filter(c => c.key === "qbit_password");
  const apiCreds = creds.filter(c => c.key === "hardcover_api_key");
  return (
    <div style={{ paddingBottom: 40 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 24, flexWrap: "wrap", gap: 12 }}>
        <h1 style={{ fontSize: 26, fontWeight: 700, color: t.text, margin: 0 }}>Settings</h1>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          {msg && <span style={{ fontSize: 13, fontWeight: 600, color: msg.startsWith("Error") ? t.err : t.ok }}>{msg}</span>}
          <Btn variant="primary" onClick={save} disabled={saving}>{saving ? <Spin size={14} /> : "Save Settings"}</Btn>
        </div>
      </div>

      <div className="settings-grid" style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 16, alignItems: "start" }}>

      {/* Left column — Pipeline */}
      <div>
      <SSection title="Pipeline" desc="Master controls for each stage">
        <SF label="IRC Listener" desc="Connects to MAM's #announce channel and processes every new torrent through the filter gate." example="Disabling pauses all automatic grabbing. Manual injects still work.">
          <STog on={(s.mam_irc_enabled as boolean) ?? true} onToggle={() => upd("mam_irc_enabled", !(s.mam_irc_enabled ?? true))} label />
        </SF>
        <SF label="Download Client Watcher" desc="Polls the download client every 60s to detect completed downloads, reconcile the snatch budget ledger, and drain the pending queue." example="Disabling stops all post-download pipeline processing.">
          <STog on={(s.pipeline_qbit_watcher_enabled as boolean) ?? true} onToggle={() => upd("pipeline_qbit_watcher_enabled", !(s.pipeline_qbit_watcher_enabled ?? true))} label />
        </SF>
        <SF label="Auto-Train Authors" desc="When a book is grabbed because one co-author matched, the other co-authors are automatically added to your allow list.">
          <STog on={(s.pipeline_auto_train_enabled as boolean) ?? true} onToggle={() => upd("pipeline_auto_train_enabled", !(s.pipeline_auto_train_enabled ?? true))} label />
        </SF>
        <SF label="Dry Run" desc="Filter + policy run normally but no .torrent files are fetched and nothing is submitted to the download client. The announce log still records what WOULD have happened." warn={s.dry_run ? "Active — no torrents will be downloaded" : undefined}>
          <STog on={!!s.dry_run} onToggle={() => upd("dry_run", !s.dry_run)} label />
        </SF>
      </SSection>

      <SSection title="Review & Enrichment" desc="Book approval workflow">
        <SF label="Manual Review Queue" desc="Every downloaded book enters a review queue for your approval before Calibre delivery. Rejecting deletes the staged copy; the seeding original is untouched." example="Disabling sends books straight to the sink without review.">
          <STog on={(s.review_queue_enabled as boolean) ?? true} onToggle={() => upd("review_queue_enabled", !(s.review_queue_enabled ?? true))} label />
        </SF>
        <SF label="Metadata Enrichment" desc="Scrapes Goodreads, Amazon, Hardcover, Kobo, IBDB, and Google Books for covers, descriptions, series info, ISBN, and page counts. MAM's own API runs first (free, highest confidence).">
          <STog on={!!s.metadata_enrichment_enabled} onToggle={() => upd("metadata_enrichment_enabled", !s.metadata_enrichment_enabled)} label />
        </SF>
        <SF label="Review Timeout" desc="Books in the review queue longer than this are auto-added to Calibre with basic metadata only." example="14 days = undecided books get imported after 2 weeks.">
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <input type="number" min={1} value={s.metadata_review_timeout_days as number ?? 14} onChange={e => upd("metadata_review_timeout_days", parseInt(e.target.value) || 14)} style={nist} />
            <span style={{ fontSize: 12, color: t.textDim }}>days</span>
          </div>
        </SF>
      </SSection>

      <SSection title="Grab Policy" desc="VIP, freeleech, and ratio protection">
        <SF label="Always Grab VIP" desc="VIP torrents are free downloads that don't count against your ratio. Bypasses all other policy checks." example="Enabled = any VIP torrent from an allowed author is grabbed immediately.">
          <STog on={(s.policy_vip_always_grab as boolean) ?? true} onToggle={() => upd("policy_vip_always_grab", !(s.policy_vip_always_grab ?? true))} label />
        </SF>
        <SF label="Free Only" desc="Only grab free torrents (VIP, global FL, personal FL, or wedge-applied). Non-free torrents are skipped." example="Protects your ratio during lean periods.">
          <STog on={!!s.policy_free_only} onToggle={() => upd("policy_free_only", !s.policy_free_only)} label />
        </SF>
        <SF label="Use Freeleech Wedges" desc="Spend a wedge to make a non-free torrent free. Wedges are NOT spent on torrents that are already free (VIP, FL, or temporary VIP while active).">
          <STog on={!!s.policy_use_wedge} onToggle={() => upd("policy_use_wedge", !s.policy_use_wedge)} label />
        </SF>
        <SF label="Ratio Floor" desc="Skip non-free torrents when your ratio drops below this value. 0 disables ratio protection." example="1.0 = stops grabbing non-free books when ratio approaches 1:1.">
          <input type="number" min={0} step={0.1} value={s.policy_ratio_floor as number ?? 0} onChange={e => upd("policy_ratio_floor", parseFloat(e.target.value) || 0)} style={nist} />
        </SF>
      </SSection>

      <SSection title="Notifications (ntfy)" desc="Push notifications">
        <SF label="ntfy Server URL" desc="URL of your ntfy server (public or self-hosted)." example='"https://ntfy.sh" or "http://10.0.10.20:8080"'>
          <input value={(s.ntfy_url as string) || ""} onChange={e => upd("ntfy_url", e.target.value)} placeholder="https://ntfy.sh" style={{ ...ist, width: 300, minWidth: 200 }} />
        </SF>
        <SF label="ntfy Topic" desc="Topic name to publish to. Subscribe in the ntfy app to receive.">
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <input value={(s.ntfy_topic as string) || "seshat"} onChange={e => upd("ntfy_topic", e.target.value)} style={{ ...ist, width: 140 }} />
            <Btn variant="ghost" onClick={testNtfy} disabled={testingNtfy}>{testingNtfy ? <Spin size={14} /> : "Test"}</Btn>
            {ntfyResult && <span style={{ fontSize: 11, color: ntfyResult.startsWith("✓") ? t.ok : t.err, fontWeight: 600 }}>{ntfyResult}</span>}
          </div>
        </SF>
        <SF label="Notification Types" desc="Choose which events trigger a push notification." wide>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px 32px", marginTop: 8 }}>
            <NCheck label="New book grabbed" field="notify_on_grab" s={s} upd={upd} />
            <NCheck label="Download completed" field="notify_on_download_complete" s={s} upd={upd} />
            <NCheck label="Pipeline errors" field="notify_on_pipeline_error" s={s} upd={upd} />
            <NCheck label="Daily — accepted books" field="notify_daily_accepted" s={s} upd={upd} />
            <NCheck label="Daily — tentative authors" field="notify_daily_tentative" s={s} upd={upd} />
            <NCheck label="Daily — ignored summary" field="notify_daily_ignored" s={s} upd={upd} />
            <NCheck label="Weekly digest" field="notify_weekly_digest" s={s} upd={upd} />
          </div>
        </SF>
        <SF label="Digest Hour" desc="When the daily digest fires. Weekly digest is always Sunday 23:30.">
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <select value={s.daily_digest_hour as number ?? 9} onChange={e => upd("daily_digest_hour", parseInt(e.target.value))}
              style={{ ...ist, width: use12h ? 120 : 70, cursor: "pointer", appearance: "auto" }}>
              {HOURS_24.map(h => <option key={h} value={h}>{use12h ? fmt12(h) : `${String(h).padStart(2, "0")}:00`}</option>)}
            </select>
            <button onClick={() => setUse12h(!use12h)} style={{ background: "none", border: "none", color: t.accent, cursor: "pointer", fontSize: 11, fontWeight: 600 }}>{use12h ? "24h" : "12h"}</button>
          </div>
        </SF>
      </SSection>

      <SSection title="Operational">
        <SF label="Verbose Logging" desc="Enable DEBUG-level output. Makes the log viewer much more detailed but noisier.">
          <STog on={!!s.verbose_logging} onToggle={() => upd("verbose_logging", !s.verbose_logging)} label />
        </SF>
      </SSection>
      </div>{/* end left column */}

      {/* Right column */}
      <div>
      <SSection title="Snatch Budget" desc="MAM active-snatches rate limiting">
        <SF label="Budget Cap" desc="Max active snatches. New grabs queue when full; oldest queue items rotate to the delayed folder." example="MAM default: 30 for new users, 200 for Power User+.">
          <input type="number" min={1} value={s.snatch_budget_cap as number ?? 200} onChange={e => upd("snatch_budget_cap", parseInt(e.target.value) || 200)} style={nist} />
        </SF>
        <SF label="Queue Max" desc="Pending queue size before FIFO eviction to the delayed torrents folder." example="200 = matches budget cap for max throughput.">
          <input type="number" min={1} value={s.snatch_queue_max as number ?? 200} onChange={e => upd("snatch_queue_max", parseInt(e.target.value) || 200)} style={nist} />
        </SF>
        <SF label="Excluded Uploaders" desc="MAM usernames whose uploads are never grabbed. Prevents downloading your own uploads." example="Add your MAM username here.">
          {editingUploaders ? (
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <textarea value={uploadersText} onChange={e => setUploadersText(e.target.value)} rows={2} placeholder="One per line" autoFocus
                style={{ ...ist, width: 180, resize: "vertical", fontFamily: "inherit" }} />
              <Btn variant="primary" onClick={() => { upd("excluded_uploaders", uploadersText.split("\n").map(s => s.trim()).filter(Boolean)); setEditingUploaders(false); }}>Done</Btn>
              <Btn variant="ghost" onClick={() => setEditingUploaders(false)}>Cancel</Btn>
            </div>
          ) : (
            <BadgeList items={uploaders} onEdit={() => { setUploadersText(uploaders.join("\n")); setEditingUploaders(true); }} onClear={() => upd("excluded_uploaders", [])} />
          )}
        </SF>
        <SF label="Delayed Torrents Path" desc="When the queue overflows, the oldest grab's .torrent is dumped here. Leave empty to disable FIFO rotation (new grabs are dropped instead)." example='e.g. "/delayed-torrents" — mount this path in docker-compose'>
          <input value={(s.delayed_torrents_path as string) || ""} onChange={e => upd("delayed_torrents_path", e.target.value)} placeholder="/delayed-torrents" style={{ ...ist, width: 220 }} />
        </SF>
      </SSection>

      <SSection title="MyAnonamouse" desc="IRC + session credentials">
        <SF label="IRC Nickname" desc="Seshat's nickname on MAM's IRC server (the name it connects as in #announce)." example='"Turtles81_seshat"'>
          <input value={(s.mam_irc_nick as string) || ""} onChange={e => upd("mam_irc_nick", e.target.value)} placeholder="YourNick_seshat" style={{ ...ist, width: 200 }} />
        </SF>
        <SF label="IRC Account" desc="Your MAM IRC account name used for SASL authentication. This is your main MAM username." example='"Turtles81"'>
          <input value={(s.mam_irc_account as string) || ""} onChange={e => upd("mam_irc_account", e.target.value)} placeholder="YourUsername" style={{ ...ist, width: 200 }} />
        </SF>
        {mamCreds.map(c => <CredField key={c.key} item={c} onSaved={loadCreds} desc={
          c.key === "mam_session_id" ? 'Your MAM session cookie. Get from MAM → Preferences → Security → Generate Session. Set "Allow Session to set Dynamic Seedbox IP" to No.'
          : "Password for SASL authentication to MAM's IRC server."
        } />)}
      </SSection>

      <SSection title="Download Client" desc="Torrent client connection">
        <SF label="Client Type" desc="Which torrent client Seshat should connect to. MAM supports qBittorrent, Transmission, Deluge, and rTorrent.">
          <select value={(s.download_client_type as string) || "qbittorrent"} onChange={e => upd("download_client_type", e.target.value)}
            style={{ ...ist, width: 180, cursor: "pointer", appearance: "auto" }}>
            <option value="qbittorrent">qBittorrent</option>
            <option value="transmission">Transmission</option>
            <option value="deluge">Deluge</option>
            <option value="rtorrent">rTorrent</option>
          </select>
        </SF>
        <SF label="WebUI URL" desc="Full URL to the download client's Web API, including port." example='e.g. "http://10.0.10.20:8180" for qBittorrent, "http://host:9091" for Transmission'>
          <input value={(s.qbit_url as string) || ""} onChange={e => upd("qbit_url", e.target.value)} placeholder="http://10.0.10.20:8180" style={{ ...ist, width: 260 }} />
        </SF>
        <SF label="Username" desc="WebUI login username for the download client.">
          <input value={(s.qbit_username as string) || ""} onChange={e => upd("qbit_username", e.target.value)} placeholder="admin" style={{ ...ist, width: 160 }} />
        </SF>
        {qbitCreds.map(c => <CredField key={c.key} item={c} onSaved={loadCreds} desc="WebUI login password for the download client." />)}
        <QbitTestButton />
        <SF label="Watch Category" desc="Torrent category that Seshat manages. All grabs receive this category; the budget watcher counts only torrents in this category." example='Default "[mam-reseed]" — the bracket convention keeps it visually distinct.'>
          <input value={(s.qbit_watch_category as string) || "[mam-reseed]"} onChange={e => upd("qbit_watch_category", e.target.value)} style={{ ...ist, width: 180 }} />
        </SF>
        <SF label="Download Path" desc="Base download directory as seen inside the download client's container. Subfolders are created under this path based on the folder structure setting." example='e.g. "/data/[mam-complete]"'>
          <input value={(s.qbit_download_path as string) || ""} onChange={e => upd("qbit_download_path", e.target.value)} placeholder="/data/[mam-complete]" style={{ ...ist, width: 260 }} />
        </SF>
        <SF label="Folder Structure" desc="How downloads are organized inside the download path. 'Author' creates subfolders by author name from the MAM announce.">
          <select value={(s.download_folder_structure as string) || "monthly"} onChange={e => upd("download_folder_structure", e.target.value)}
            style={{ ...ist, width: 190, cursor: "pointer", appearance: "auto" }}>
            <option value="monthly">[YYYY-MM] Monthly</option>
            <option value="yearly">[YYYY] Yearly</option>
            <option value="author">By Author</option>
            <option value="flat">Flat (no subfolders)</option>
          </select>
        </SF>
      </SSection>

      <SSection title="API Keys & Sink" desc="External services">
        {apiCreds.map(c => <CredField key={c.key} item={c} onSaved={loadCreds} desc="Bearer token from hardcover.app → Account → API. Enables richer series, ratings, and tag data." />)}
        <SF label="Default Sink" desc="Where approved books are delivered after review.">
          <select value={(s.default_sink as string) || "cwa"} onChange={e => upd("default_sink", e.target.value)}
            style={{ ...ist, width: 260, cursor: "pointer", appearance: "auto" }}>
            <option value="cwa">CWA — auto-import via ingest folder</option>
            <option value="calibre">Calibre — direct calibredb add</option>
            <option value="folder">Folder — copy to a directory</option>
            <option value="audiobookshelf">Audiobookshelf — library folder</option>
          </select>
        </SF>
        <SF label="CWA Web URL" desc="Calibre-Web Automated web interface URL. Shows a quick-launch button on the Dashboard." example='e.g. "http://10.0.10.20:8083"'>
          <input value={(s.cwa_web_url as string) || ""} onChange={e => upd("cwa_web_url", e.target.value)} placeholder="http://host:port" style={{ ...ist, width: 260 }} />
        </SF>
        <SF label="Calibre Web URL" desc="Calibre Content Server web interface URL. Shows a quick-launch button on the Dashboard." example='e.g. "http://10.0.10.20:8081"'>
          <input value={(s.calibre_web_url as string) || ""} onChange={e => upd("calibre_web_url", e.target.value)} placeholder="http://host:port" style={{ ...ist, width: 260 }} />
        </SF>
        <SF label="Emergency Export Path" desc="If the sink is unreachable after multiple retries, books are dumped here so they're never lost. Leave empty to keep retrying indefinitely." example='e.g. "/emergency-books" — mount this path in docker-compose'>
          <input value={(s.emergency_export_path as string) || ""} onChange={e => upd("emergency_export_path", e.target.value)} placeholder="/emergency-books" style={{ ...ist, width: 220 }} />
        </SF>
        <SF label="Sink Max Retries" desc="How many times to retry sink delivery before exporting to the emergency folder. Each retry happens on the review-timeout tick (daily)." example="3 = dump to emergency folder after 3 days of failures.">
          <input type="number" min={1} value={s.sink_max_retries as number ?? 3} onChange={e => upd("sink_max_retries", parseInt(e.target.value) || 3)} style={nist} />
        </SF>
      </SSection>

      <SSection title="Data Management" desc="Clear pipeline data" defaultOpen={false}>
        <p style={{ fontSize: 12, color: t.textDim, marginBottom: 12, lineHeight: 1.5 }}>
          Safe operations clear data that rebuilds from future announces. Dangerous operations (⚠) require typed confirmation and cannot be undone.
        </p>
        <DataSection />
      </SSection>
      </div>{/* end right/middle column */}

      {/* Right column — Discovery */}
      <div>
      <SSection title="Metadata Sources" desc="Which sources to scan for book metadata">
        <SF label="Goodreads" desc="HTML scraper for author pages. Richest data source for most fiction.">
          <STog on={(s.goodreads_enabled as boolean) ?? true} onToggle={() => upd("goodreads_enabled", !(s.goodreads_enabled ?? true))} label />
        </SF>
        <SF label="Hardcover" desc="GraphQL API. Requires API key (set in API Keys section). Modern book data with series info.">
          <STog on={(s.hardcover_enabled as boolean) ?? true} onToggle={() => upd("hardcover_enabled", !(s.hardcover_enabled ?? true))} label />
        </SF>
        <SF label="Kobo" desc="HTML scraper via cloudscraper. Good for ISBN and publication dates.">
          <STog on={(s.kobo_enabled as boolean) ?? true} onToggle={() => upd("kobo_enabled", !(s.kobo_enabled ?? true))} label />
        </SF>
        <SF label="Amazon" desc="Kindle Store scraper. Slower, but unique series and release data.">
          <STog on={(s.amazon_enabled as boolean) ?? false} onToggle={() => upd("amazon_enabled", !(s.amazon_enabled ?? false))} label />
        </SF>
        <SF label="IBDB" desc="Internet Book Database. Supplementary source for drama/theater.">
          <STog on={(s.ibdb_enabled as boolean) ?? false} onToggle={() => upd("ibdb_enabled", !(s.ibdb_enabled ?? false))} label />
        </SF>
        <SF label="Google Books" desc="REST API. Auto-disables on quota exhaustion (429s). Re-enable here after cooldown.">
          <STog on={(s.google_books_enabled as boolean) ?? false} onToggle={() => upd("google_books_enabled", !(s.google_books_enabled ?? false))} label />
        </SF>
      </SSection>

      <SSection title="Source Rate Limits" desc="Seconds between requests per source">
        <SF label="Goodreads" desc="Default 2s — gentle on scraping">
          <input type="number" min={0.5} step={0.5} value={s.rate_goodreads as number ?? 2} onChange={e => upd("rate_goodreads", parseFloat(e.target.value) || 2)} style={nist} />
        </SF>
        <SF label="Hardcover" desc="Default 1s — API, more tolerant">
          <input type="number" min={0.5} step={0.5} value={s.rate_hardcover as number ?? 1} onChange={e => upd("rate_hardcover", parseFloat(e.target.value) || 1)} style={nist} />
        </SF>
        <SF label="Kobo" desc="Default 3s — cloudscraper, be gentle">
          <input type="number" min={0.5} step={0.5} value={s.rate_kobo as number ?? 3} onChange={e => upd("rate_kobo", parseFloat(e.target.value) || 3)} style={nist} />
        </SF>
        <SF label="Amazon" desc="Default 2s">
          <input type="number" min={0.5} step={0.5} value={s.rate_amazon as number ?? 2} onChange={e => upd("rate_amazon", parseFloat(e.target.value) || 2)} style={nist} />
        </SF>
        <SF label="IBDB" desc="Default 1s">
          <input type="number" min={0.5} step={0.5} value={s.rate_ibdb as number ?? 1} onChange={e => upd("rate_ibdb", parseFloat(e.target.value) || 1)} style={nist} />
        </SF>
        <SF label="Google Books" desc="Default 1.5s">
          <input type="number" min={0.5} step={0.5} value={s.rate_google_books as number ?? 1.5} onChange={e => upd("rate_google_books", parseFloat(e.target.value) || 1.5)} style={nist} />
        </SF>
        <SF label="MAM Search" desc="Default 2s — rate limit for discovery MAM scans">
          <input type="number" min={0.5} step={0.5} value={s.rate_mam as number ?? 2} onChange={e => upd("rate_mam", parseFloat(e.target.value) || 2)} style={nist} />
        </SF>
      </SSection>

      <SSection title="Author Scanning" desc="Background metadata lookup">
        <SF label="Auto-scan Enabled" desc="Periodically scan all authors against enabled sources for new books.">
          <STog on={(s.author_scanning_enabled as boolean) ?? true} onToggle={() => upd("author_scanning_enabled", !(s.author_scanning_enabled ?? true))} label />
        </SF>
        <SF label="Owned Books Only" desc="Only enrich metadata on books already in Calibre — no new 'missing' discoveries.">
          <STog on={(s.author_scan_owned_only as boolean) ?? false} onToggle={() => upd("author_scan_owned_only", !(s.author_scan_owned_only ?? false))} label />
        </SF>
        <SF label="Exclude Audiobooks" desc="Filter out audiobook-only editions and narrator credits during scans.">
          <STog on={(s.exclude_audiobooks as boolean) ?? true} onToggle={() => upd("exclude_audiobooks", !(s.exclude_audiobooks ?? true))} label />
        </SF>
        <SF label="Lookup Interval (days)" desc="How often the scheduled author scan runs. 0 = manual only.">
          <input type="number" min={0} value={s.lookup_interval_days as number ?? 3} onChange={e => upd("lookup_interval_days", parseInt(e.target.value) || 3)} style={nist} />
        </SF>
      </SSection>

      <SSection title="Library Sync" desc="Calibre library discovery & sync">
        <SF label="Sync Interval (minutes)" desc="How often to check Calibre's metadata.db for changes. 0 = manual only.">
          <input type="number" min={0} value={s.library_sync_interval_minutes as number ?? 60} onChange={e => upd("library_sync_interval_minutes", parseInt(e.target.value) || 60)} style={nist} />
        </SF>
        <SF label="Languages" desc="Comma-separated language filter for source scans." wide>
          <input value={((s.languages as string[]) ?? []).join(", ")} onChange={e => upd("languages", e.target.value.split(",").map((x: string) => x.trim()).filter(Boolean))} placeholder="English" style={{ ...ist, width: "100%" }} />
        </SF>
        <SF label="Theme" desc="UI color scheme.">
          <select value={(s.theme as string) || "dark"} onChange={e => upd("theme", e.target.value)}
            style={{ ...ist, width: 140, cursor: "pointer", appearance: "auto" }}>
            <option value="dark">Dark</option>
            <option value="dim">Dim</option>
            <option value="light">Light</option>
          </select>
        </SF>
      </SSection>

      <SSection title="Discovery MAM" desc="MAM search for missing books">
        <SF label="MAM Search Enabled" desc="Enable searching MyAnonamouse for books your library is missing.">
          <STog on={(s.mam_enabled as boolean) ?? false} onToggle={() => upd("mam_enabled", !(s.mam_enabled ?? false))} label />
        </SF>
        <SF label="Auto-scan Enabled" desc="Periodically batch-scan unscanned books against MAM.">
          <STog on={(s.mam_scanning_enabled as boolean) ?? true} onToggle={() => upd("mam_scanning_enabled", !(s.mam_scanning_enabled ?? true))} label />
        </SF>
        <SF label="Scan Interval (minutes)" desc="How often the MAM batch scanner runs.">
          <input type="number" min={0} value={s.mam_scan_interval_minutes as number ?? 360} onChange={e => upd("mam_scan_interval_minutes", parseInt(e.target.value) || 360)} style={nist} />
        </SF>
      </SSection>

      <SSection title="Discovery Notifications" desc="ntfy events for the discovery domain">
        <NCheck label="Scan complete" field="ntfy_on_scan_complete" s={s} upd={upd} />
        <NCheck label="New books found" field="ntfy_on_new_books" s={s} upd={upd} />
        <NCheck label="MAM scan complete" field="ntfy_on_mam_complete" s={s} upd={upd} />
        <NCheck label="Sent to pipeline" field="ntfy_on_pipeline_sent" s={s} upd={upd} />
        <NCheck label="Library sync" field="ntfy_on_library_sync" s={s} upd={upd} />
      </SSection>
      </div>{/* end discovery column */}

      </div>{/* close settings-grid */}

      {/* Build SHA footer — proves which container build the user is
          actually running. Empty string falls through to nothing on
          standalone/dev runs where /api/version returns "unknown". */}
      {buildSha && (
        <div style={{ marginTop: 24, paddingTop: 12, borderTop: `1px solid ${t.borderL}`, fontSize: 11, color: t.textDim, textAlign: "center" }}>
          Build: <code style={{ color: t.text2 }}>{buildSha}</code>
        </div>
      )}
    </div>
  );
}

function QbitTestButton() {
  const t = useTheme();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  async function test() {
    setBusy(true); setResult(null);
    try {
      const r = await api.post<{ ok: boolean; message: string }>("/v1/mam/test-qbit");
      setResult(r.ok ? `✓ ${r.message}` : `✗ ${r.message}`);
    } catch (e) { setResult(`✗ ${e}`); }
    finally { setBusy(false); setTimeout(() => setResult(null), 8000); }
  }
  return (
    <SF label="Test Connection" desc="Attempt a login to verify URL, username, and password are correct." warn="Some clients (e.g. qBittorrent) ban the IP after repeated failed login attempts. Use sparingly.">
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <Btn variant="ghost" onClick={test} disabled={busy}>{busy ? <Spin size={14} /> : "Test"}</Btn>
        {result && <span style={{ fontSize: 11, color: result.startsWith("✓") ? t.ok : t.err, fontWeight: 600 }}>{result}</span>}
      </div>
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
