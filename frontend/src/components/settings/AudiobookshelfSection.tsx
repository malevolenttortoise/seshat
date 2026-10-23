// Settings → Audiobookshelf: connection, library picker, sync options.
// Split out of SettingsPage (wave 5b S16, issue 23).
import { useState } from "react";
import { api } from "../../api";
import { useTheme } from "../../theme";
import type { SettingsDraft as S, CredItem } from "../../hooks/useSettingsDraft";
import { Btn } from "../Btn";
import { Spin } from "../Spin";
import { CredField, SF } from "./fields";

// ── Audiobookshelf Section ────────────────────────────────────

interface AbsLibrary { id: string; name: string; mediaType?: string; folders?: { fullPath: string }[]; lastUpdate?: number; }

export function AudiobookshelfSection({ s, upd, ist, nist, creds, onCredSaved }: {
  s: S; upd: (k: string, v: unknown) => void; ist: any; nist: any;
  creds: CredItem[]; onCredSaved: () => void;
}) {
  const t = useTheme();
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [libs, setLibs] = useState<AbsLibrary[] | null>(null);
  const [rebuilding, setRebuilding] = useState(false);
  const [rebuildResult, setRebuildResult] = useState<string | null>(null);
  const [showManualSinkId, setShowManualSinkId] = useState(false);

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

    <SF label="ABS URL" desc="Address Seshat talks to ABS at — used for both the backend REST API and the Dashboard's open-in-ABS link. Leave the advanced override below blank unless your browser hits ABS at a different hostname than the Seshat container does (rare: public DNS vs. Docker network name)." example="e.g. http://10.0.10.20:13378">
      <input
        value={url}
        onChange={e => {
          const v = e.target.value.trim();
          upd("abs_url", v);
          // Keep the web-URL mirror aligned by default so the
          // Dashboard quick-launch points at the same place the
          // backend uses. Advanced users with a split-hostname
          // setup override below to break the mirror.
          if (!s.abs_web_url || s.abs_web_url === url) {
            upd("abs_web_url", v);
          }
        }}
        placeholder="http://10.0.10.20:13378"
        style={{ ...ist, width: 280 }}
      />
    </SF>

    <SF label="Web URL Override" desc="Advanced — only set when the browser uses a different hostname than the Seshat container does (public DNS, reverse proxy, etc.). Leaving this blank mirrors ABS URL above.">
      <input
        value={(s.abs_web_url as string) || ""}
        onChange={e => upd("abs_web_url", e.target.value.trim())}
        placeholder="(leave blank to mirror ABS URL)"
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

    {/* Sink library target — the primary UX is "Use as sink" on a
        row in the library list above. This block is the status
        readout (which library is currently selected) plus a
        collapsed manual-paste fallback for users who skipped the
        test step or want to paste a UUID directly. */}
    <SF
      label="Sink Library Target"
      desc="Which ABS library the audiobook sink delivers into. Set via 'Use as sink' in the library list above; this row is the current status."
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        {(() => {
          const currentId = (s.abs_sink_library_id as string) || "";
          if (!currentId) {
            return <span style={{ fontSize: 12, color: t.textDim, fontStyle: "italic" }}>
              Not set — click "Use as sink" above after testing.
            </span>;
          }
          const matched = (libs || []).find(l => l.id === currentId);
          return (
            <span style={{ fontSize: 12, color: t.text2, display: "inline-flex", alignItems: "center", gap: 6 }}>
              <span style={{ color: t.ok, fontWeight: 700 }}>✓</span>
              {matched ? (
                <>
                  <span style={{ fontWeight: 600 }}>{matched.name}</span>
                  <span style={{ color: t.textDim, fontFamily: "monospace", fontSize: 11 }}>({currentId})</span>
                </>
              ) : (
                <span style={{ fontFamily: "monospace", fontSize: 11 }}>{currentId}</span>
              )}
              <button
                onClick={() => upd("abs_sink_library_id", "")}
                title="Clear selection"
                style={{
                  marginLeft: 4, fontSize: 11, padding: "1px 7px",
                  background: "transparent", color: t.textDim,
                  border: `1px solid ${t.borderL}`, borderRadius: 4, cursor: "pointer",
                }}
              >Clear</button>
            </span>
          );
        })()}
        <button
          onClick={() => setShowManualSinkId(v => !v)}
          style={{
            fontSize: 11, padding: "2px 8px",
            background: "transparent", color: t.textDim,
            border: `1px dashed ${t.border}`, borderRadius: 4, cursor: "pointer",
          }}
        >{showManualSinkId ? "Hide manual paste" : "Paste UUID manually"}</button>
      </div>
      {showManualSinkId && (
        <input
          value={(s.abs_sink_library_id as string) || ""}
          onChange={e => upd("abs_sink_library_id", e.target.value.trim())}
          placeholder="Paste ABS library UUID"
          style={{ ...ist, width: 280, fontFamily: "monospace", fontSize: 11, marginTop: 6 }}
        />
      )}
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
      label="ABS Sync Interval"
      desc="How often the scheduled library-sync loop checks Audiobookshelf for new audiobooks. 0 inherits the global Library Sync Interval (above in the Libraries section)."
    >
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <input type="number" min={0} value={s.abs_sync_interval_minutes as number ?? 0} onChange={e => upd("abs_sync_interval_minutes", parseInt(e.target.value) || 0)} style={nist} />
        <span style={{ fontSize: 13, color: t.textDim }}>min</span>
      </div>
    </SF>

    <SF
      label="Audible Region"
      desc="Controls which Audible TLD catalog searches hit. Audible also hydrates every hit through Audnexus internally using the same region code."
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

    {/* Note: the per-source Audible toggle previously lived here but
        migrated to the unified Metadata Sources panel (Discovery →
        Metadata Sources). Same for Goodreads, Hardcover, etc. — the
        panel is now the sole editor. Audnexus has no standalone row
        because it piggybacks on Audible's hydration; toggling Audible
        toggles the whole Audible+Audnexus chain. */}

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
