// Settings → Library Management: libraries, the active one, rescan, Calibre / CWA.
// Split out of SettingsPage (wave 5b S16, issue 23).
import { useEffect, useState } from "react";
import { api } from "../../api";
import { useTheme } from "../../theme";
import type { SettingsDraft as S, CredItem } from "../../hooks/useSettingsDraft";
import { Btn } from "../Btn";
import { Spin } from "../Spin";
import { CredField, SF } from "./fields";

// ── Library Management Section ────────────────────────────────

export function LibrarySection({ s, upd, ist, nist, cwaCreds, onCredSaved }: { s: S; upd: (k: string, v: unknown) => void; ist: any; nist: any; cwaCreds: CredItem[]; onCredSaved: () => void }) {
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
    <SF label="Calibre Library Path" desc="Container-local path to the Calibre library folder (contains metadata.db). Usually set via CALIBRE_LIBRARY_PATH env at startup.">
      <input value={(s.calibre_library_path as string) || ""} onChange={e => upd("calibre_library_path", e.target.value)} placeholder="/calibre" style={{ ...ist, width: 260 }} />
    </SF>
    <SF label="Calibre-Web URL" desc="Web UI for the Dashboard quick-launch link. Works for both stock Calibre-Web and Calibre-Web Automated (CWA) — CWA is a Calibre-Web fork so most users run one instance.">
      {/* Single input — writes to `cwa_web_url` (the preferred key).
          Historical `calibre_web_url` stays in DEFAULT_SETTINGS for
          back-compat but is no longer user-editable from the UI;
          the Dashboard falls back to it when cwa_web_url is empty
          so upgraded installs keep working without a migration. */}
      <input value={(s.cwa_web_url as string) || ""} onChange={e => upd("cwa_web_url", e.target.value)} placeholder="http://host:port" style={{ ...ist, width: 260 }} />
    </SF>
    <SF label="Calibre Content Server URL" desc="Calibre's built-in Content Server API endpoint for direct library access. Different from Calibre-Web above.">
      <input value={(s.calibre_url as string) || ""} onChange={e => upd("calibre_url", e.target.value)} placeholder="http://host:port" style={{ ...ist, width: 260 }} />
    </SF>

    {/* v2.3.5 CWA push-back. Slim users (no calibredb) need this to
        push Seshat metadata edits back to Calibre. Backend drives
        CWA's existing /admin/book/<id> form POST handler — needs a
        login + the password lives in the encrypted secret store. */}
    <div style={{ marginTop: 12, paddingTop: 12, borderTop: `1px solid ${t.borderL}` }}>
      <div style={{ fontSize: 13, fontWeight: 700, color: t.text2, marginBottom: 6 }}>
        Calibre push-back via CWA (slim image only)
      </div>
      <div style={{ fontSize: 12, color: t.textDim, marginBottom: 10 }}>
        When the slim image is in use, Seshat pushes metadata edits to
        Calibre by driving CWA's admin form. Leave blank if you run
        the full image — push-back uses calibredb directly there.
      </div>
      <SF label="CWA Base URL" desc="Same instance as Calibre-Web URL above; this is where Seshat POSTs the metadata edits.">
        <input value={(s.cwa_base_url as string) || ""} onChange={e => upd("cwa_base_url", e.target.value)} placeholder="http://cwa:8083" style={{ ...ist, width: 260 }} />
      </SF>
      <SF label="CWA Username" desc="A CWA user account with edit permissions. A dedicated 'seshat' account is recommended for clear audit-log attribution.">
        <input value={(s.cwa_username as string) || ""} onChange={e => upd("cwa_username", e.target.value)} placeholder="seshat" style={{ ...ist, width: 260 }} />
      </SF>
      {cwaCreds.map(c => <CredField key={c.key} item={c} onSaved={onCredSaved} desc="Password for the CWA user above. Stored encrypted." />)}
    </div>
  </>;
}
