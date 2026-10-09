// The Settings page's field primitives, shared by the page and the
// panels split out of it (wave 5b S16): the in-page search and the
// current section (each SF hides itself when the search doesn't match
// its label, description or section), SF, STog and CredField.
import { createContext, useContext, useState, type ReactNode } from "react";
import { api } from "../../api";
import { useTheme } from "../../theme";
import type { CredItem } from "../../hooks/useSettingsDraft";
import { Btn } from "../Btn";
import { Spin } from "../Spin";

// v2.15.0 #B — Settings page search. When the user types a query in
// the in-page search box, every SF reads the query from this context
// and self-hides if its label/desc doesn't match. Sections whose
// `section === "X"` conditional would normally skip rendering also
// render during search mode so matching SFs in other sections can
// surface without the user having to click each section first.
export const SettingsSearchContext = createContext<string>("");
export function useSettingsSearch(): string { return useContext(SettingsSearchContext); }

export function matchSearch(needle: string, haystacks: (string | null | undefined)[]): boolean {
  if (!needle) return true;
  const n = needle.toLowerCase();
  for (const h of haystacks) {
    if (h && h.toLowerCase().includes(n)) return true;
  }
  return false;
}

export interface SettingsSection {
  id: string;
  label: string;
  group: string;
  // v2.15.1 — extra match terms for the in-page search. When the
  // user types one of these, every SF in this section matches even
  // if its own label/desc doesn't. Lets users find "ntfy" → land
  // on Notifications, "qbit" → Download Client, etc.
  keywords?: string[];
}

// v2.15.1 — each section's content is wrapped in SectionScope which
// publishes the section's id/label/keywords via CurrentSectionContext.
// SF reads both that + the search query and decides whether to render.
export const CurrentSectionContext = createContext<SettingsSection | null>(null);
export function useCurrentSection(): SettingsSection | null { return useContext(CurrentSectionContext); }

// ── Shared field components ───────────────────────────────────

export function SF({ label, desc, example, children, warn, wide, searchAlso }: {
  label: string; desc?: string; example?: string; children: ReactNode; warn?: string; wide?: boolean;
  // v2.15.1 — extra match strings. Call sites that want the search
  // to find this SF by the underlying settings key or its current
  // value pass them here. Default omitted; match is still permissive
  // since section name/keywords also count.
  searchAlso?: (string | undefined | null)[];
}) {
  const t = useTheme();
  const search = useSettingsSearch();
  const sect = useCurrentSection();
  if (search) {
    const haystacks = [
      label, desc, example,
      sect?.label,
      ...(sect?.keywords ?? []),
      ...(searchAlso ?? []),
    ];
    if (!matchSearch(search, haystacks)) return null;
  }
  return (
    <div style={{
      display: "grid",
      gridTemplateColumns: wide ? "1fr" : "minmax(0, 1fr) minmax(180px, 320px)",
      alignItems: "center", padding: "14px 0", borderBottom: `1px solid ${t.borderL}`, gap: "6px 16px",
    }}>
      <div style={{ minWidth: 0 }}>
        {search && sect && (
          <div style={{
            fontSize: 10, fontWeight: 700, color: t.tf,
            textTransform: "uppercase", letterSpacing: "0.05em",
            marginBottom: 4,
          }}>
            {sect.label}
          </div>
        )}
        <div style={{ fontSize: 15, fontWeight: 600, color: t.text }}>{label}</div>
        {desc && <div style={{ fontSize: 13, color: t.textDim, marginTop: 3, lineHeight: 1.5 }}>{desc}</div>}
        {example && <div style={{ fontSize: 12, color: t.accent, marginTop: 2, fontStyle: "italic" }}>{example}</div>}
        {warn && <div style={{ fontSize: 12, color: t.warn, marginTop: 3 }}>⚠ {warn}</div>}
      </div>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", minHeight: 32 }}>{children}</div>
    </div>
  );
}

export function STog({ on, onToggle, disabled, label }: { on: boolean; onToggle: () => void; disabled?: boolean; label?: boolean }) {
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

export function CredField({ item, desc, onSaved, canGenerate, clearable, clearConfirm }: { item: CredItem; desc?: string; onSaved: () => void; canGenerate?: boolean; clearable?: boolean; clearConfirm?: string }) {
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
  async function clear() {
    if (!confirm(clearConfirm || `Clear ${item.label}?`)) return;
    setBusy(true);
    try { await api.del(`/v1/credentials/${item.key}`); onSaved(); }
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
          {clearable && <Btn variant="ghost" onClick={clear} disabled={busy}>Clear</Btn>}
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
