// MetadataSourcesPanel — unified per-source configuration panel.
//
// Replaces the old scattered `*_enabled` toggles + `rate_*` sliders +
// drag-sortable priority list that lived across two separate Settings
// pages. One panel, two tabs (Ebook / Audiobook), each showing:
//
//   * the ordered priority list (rank = position)
//   * per-row: two checkboxes (Enrich / Scan) + rate-limit number
//   * MAM pinned at rank 1, locked
//
// Every available source always has a rank. Both toggles off = source
// doesn't run (the derivation layer filters it out at dispatcher-build
// time); no separate "disabled" bucket to reason about.
//
// Reorder UX: native HTML5 drag-and-drop. Rows grab by the handle,
// drop targets show an accent-colour top border while hovering.
//
// Everything is buffered client-side until the user clicks Save —
// PUT /v1/metadata-sources replaces the whole state atomically and
// rebuilds the dispatcher so changes apply live without a restart.
import { useEffect, useState } from "react";
import { Btn } from "./Btn";
import { Spin } from "./Spin";
import { api } from "../api";
import { useTheme } from "../theme";

interface SourceEntry {
  rate_limit: number;
  ebook_enrich: boolean;
  ebook_scan: boolean;
  audiobook_enrich: boolean;
  audiobook_scan: boolean;
  // v2.3.2: when checked, source-scan keeps DETAIL-fetching books
  // missing this source's URL even when other sources have URLs
  // for them. Defaults true on the primary tier (Goodreads /
  // Hardcover for ebook; Audible / Hardcover for audiobook).
  mandatory: boolean;
  // v2.11.0 Stage 5++: Amazon-specific config strings that drive
  // the server-side authorFilters API on /juvec. Null for every
  // other source.
  // `format` = ebook-tab filter; `audiobook_format` = the v2.11.1
  // addition for audiobook-tab Amazon scans (Audible / Audio CD /
  // Preloaded Digital Audio Player / MP3 CD).
  format?: string | null;
  audiobook_format?: string | null;
  language?: string | null;
  // v2.11.1 N5: Kobo-specific. Parallel detail-fetch worker count.
  // Effective request rate is ~concurrency/rate_limit. Null for
  // every other source.
  concurrency?: number | null;
}

// Amazon Author-Store format options (matches FILTER_TO_BINDING in
// app/discovery/sources/amazon_widget_parser.py). The internal value
// is what Amazon's /juvec API accepts; the display label is what the
// user sees in the dropdown.
const AMAZON_FORMAT_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "kindle", label: "Kindle" },
  { value: "paperback", label: "Paperback" },
  { value: "hardcover", label: "Hardcover" },
  { value: "mass_market", label: "Mass Market Paperback" },
  { value: "allFormats", label: "All Formats" },
];

// v2.11.1: audiobook format options. `audible_audiobook` matches
// the Audible-distributed digital audiobook (the dominant audio
// format on Amazon — most authors will want this); others are
// niche physical / hardware variants. Maps to the binding symbols
// in `app/discovery/sources/amazon_widget_parser.py:FILTER_TO_BINDING`.
const AMAZON_AUDIOBOOK_FORMAT_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "audible_audiobook", label: "Audible Audiobook" },
  { value: "audio_cd", label: "Audio CD" },
  { value: "mp3_cd", label: "MP3 CD" },
  { value: "preloaded_digital_audio", label: "Preloaded Digital Audio Player" },
  { value: "allFormats", label: "All Audio Formats" },
];

// Amazon Author-Store language options. The static list covers the
// most common languages Sanderson + other prolific authors expose;
// rarer languages are still selectable by typing into the input but
// these are the quick-pick set.
const AMAZON_LANGUAGE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "English", label: "English" },
  { value: "Spanish", label: "Spanish" },
  { value: "German", label: "German" },
  { value: "French", label: "French" },
  { value: "Italian", label: "Italian" },
  { value: "Portuguese", label: "Portuguese" },
  { value: "Japanese", label: "Japanese" },
  { value: "ChineseSimplified", label: "Chinese (Simplified)" },
  { value: "ChineseTraditional", label: "Chinese (Traditional)" },
  { value: "Russian", label: "Russian" },
  { value: "Polish", label: "Polish" },
  { value: "Turkish", label: "Turkish" },
  { value: "All Languages", label: "All Languages" },
];

interface PriorityLists {
  ebook: string[];
  audiobook: string[];
}

interface SourceMetadata {
  name: string;
  display: string;
  available_for: string[];
  mam_only?: boolean;
}

interface PanelState {
  sources: Record<string, SourceEntry>;
  priority: PriorityLists;
}

interface GetResponse {
  state: PanelState;
  known: SourceMetadata[];
  derived: {
    ebook_enrich: string[];
    ebook_scan: string[];
    audiobook_enrich: string[];
    audiobook_scan: string[];
  };
}

type Tab = "ebook" | "audiobook";

export function MetadataSourcesPanel() {
  const t = useTheme();
  const [loaded, setLoaded] = useState<GetResponse | null>(null);
  const [draft, setDraft] = useState<PanelState | null>(null);
  const [tab, setTab] = useState<Tab>("ebook");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const r = await api.get<GetResponse>("/v1/metadata-sources");
      setLoaded(r);
      setDraft(JSON.parse(JSON.stringify(r.state)) as PanelState);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => { load(); }, []);

  if (!loaded || !draft) {
    return <div style={{ display: "flex", justifyContent: "center", padding: 40 }}><Spin /></div>;
  }

  const dirty = JSON.stringify(draft) !== JSON.stringify(loaded.state);

  async function save() {
    if (!draft) return;
    setSaving(true);
    setError(null);
    setMsg(null);
    try {
      const r = await api.put<{ ok: boolean; dispatcher_rebuilt: boolean }>(
        "/v1/metadata-sources", draft,
      );
      if (r.dispatcher_rebuilt) {
        setMsg("Saved. Enricher rebuilt — changes live immediately.");
      } else {
        setMsg("Saved. Dispatcher rebuild failed; restart the container to apply.");
      }
      await load();
      setTimeout(() => setMsg(null), 5000);
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }

  function reset() {
    if (!loaded) return;
    setDraft(JSON.parse(JSON.stringify(loaded.state)) as PanelState);
    setMsg(null);
    setError(null);
  }

  // v2.11.1: POST /reset wipes the panel-managed settings + rebuilds
  // from `_DEFAULT_NEW_INSTALL_STATE`. Confirmation prompt because
  // it overwrites the user's customizations (priority order, rate
  // limits, format dropdowns, etc.) wholesale. Distinct from the
  // local `reset()` above (which just discards unsaved draft).
  const [resetting, setResetting] = useState(false);
  async function resetToDefaults() {
    if (!loaded) return;
    const ok = window.confirm(
      "Reset every Amazon / Hardcover / Open Library / etc. setting on this "
      + "panel to the v2.11.x ship-defaults? This overwrites your priority "
      + "order, Rate values, Mandatory toggles, and Amazon format / "
      + "language dropdowns. Cannot be undone."
    );
    if (!ok) return;
    setResetting(true);
    setError(null);
    setMsg(null);
    try {
      const r = await api.post<GetResponse>(
        "/v1/metadata-sources/reset", {},
      );
      setLoaded(r);
      setDraft(JSON.parse(JSON.stringify(r.state)) as PanelState);
      setMsg("Reset to ship-defaults. Discovery sources rebuilt — live.");
      setTimeout(() => setMsg(null), 5000);
    } catch (e) {
      setError(String(e));
    } finally {
      setResetting(false);
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{
        fontSize: 13, color: t.textDim, lineHeight: 1.6, margin: 0,
        padding: "10px 14px", background: t.bg3, borderRadius: 8,
        border: `1px solid ${t.borderL}`,
      }}>
        <div style={{ marginBottom: 4 }}>
          <strong style={{ color: t.text2 }}>Enrich</strong> — sources run after a book downloads, merging title / description / ISBN / cover / narrator / etc. into the review-queue metadata.
        </div>
        <div style={{ marginBottom: 4 }}>
          <strong style={{ color: t.text2 }}>Scan</strong> — sources run during library-side author scanning to find books you don't have yet.
        </div>
        <div>
          <strong style={{ color: t.text2 }}>Rate (s)</strong> — seconds to wait between requests to this source. Higher = gentler on the upstream, slower scans. Leave at default if unsure.
        </div>
        <div style={{ marginTop: 6, color: t.textDim }}>
          Priority is top-to-bottom; drag rows to reorder. MAM is always first and free — its row is locked.
        </div>
      </div>

      {error && <Banner tone="err">{error}</Banner>}
      {msg && <Banner tone="ok">{msg}</Banner>}

      <div style={{ display: "flex", gap: 4, borderBottom: `1px solid ${t.border}` }}>
        <TabBtn label="Ebook" active={tab === "ebook"} onClick={() => setTab("ebook")} />
        <TabBtn label="Audiobook" active={tab === "audiobook"} onClick={() => setTab("audiobook")} />
      </div>

      <SourceList
        tab={tab}
        draft={draft}
        setDraft={setDraft}
        known={loaded.known}
      />

      {/* Sticky save bar */}
      <div style={{
        position: "sticky", bottom: 12,
        display: "flex", justifyContent: "flex-end", gap: 10,
        background: t.bg + "ee", backdropFilter: "blur(8px)",
        padding: "12px 0", borderTop: `1px solid ${t.borderL}`, marginTop: 8,
      }}>
        <Btn
          variant="ghost"
          disabled={saving || resetting}
          onClick={resetToDefaults}
          title="Wipe all panel settings + reapply v2.11.x ship-defaults"
        >
          {resetting ? <Spin size={14} /> : "Reset to defaults"}
        </Btn>
        <span style={{ flex: 1 }} />
        <span style={{ fontSize: 13, color: t.textDim, alignSelf: "center" }}>
          {dirty ? "Unsaved changes" : "No unsaved changes"}
        </span>
        <Btn variant="ghost" disabled={!dirty || saving} onClick={reset}>
          Discard
        </Btn>
        <Btn variant="primary" disabled={!dirty || saving} onClick={save}>
          {saving ? <Spin size={14} /> : "Save"}
        </Btn>
      </div>
    </div>
  );
}

// ─── Tab button ────────────────────────────────────────────────

function TabBtn({ label, active, onClick }: {
  label: string; active: boolean; onClick: () => void;
}) {
  const t = useTheme();
  return (
    <button
      onClick={onClick}
      style={{
        padding: "10px 18px",
        fontSize: 14, fontWeight: 600,
        color: active ? t.accent : t.text2,
        background: "transparent",
        border: "none",
        borderBottom: active ? `2px solid ${t.accent}` : "2px solid transparent",
        cursor: "pointer",
        marginBottom: -1,
      }}
    >
      {label}
    </button>
  );
}

// ─── Source list ───────────────────────────────────────────────

function SourceList({ tab, draft, setDraft, known }: {
  tab: Tab;
  draft: PanelState;
  setDraft: (d: PanelState) => void;
  known: SourceMetadata[];
}) {
  const t = useTheme();

  const priority = draft.priority[tab] ?? [];
  const enrichKey = tab === "ebook" ? "ebook_enrich" : "audiobook_enrich";
  const scanKey = tab === "ebook" ? "ebook_scan" : "audiobook_scan";

  // Known sources available for this content type, in the priority
  // order. Any available source missing from the priority list gets
  // appended to the end so the user can rank it later.
  const availableNames = known
    .filter(k => k.available_for.includes(tab))
    .map(k => k.name);
  const ordered = [
    ...priority.filter(n => availableNames.includes(n)),
    ...availableNames.filter(n => !priority.includes(n)),
  ];

  function setToggle(name: string, key: keyof SourceEntry, value: boolean | number | string) {
    const entry = draft.sources[name];
    if (!entry) return;
    const next: SourceEntry = { ...entry, [key]: value };
    setDraft({ ...draft, sources: { ...draft.sources, [name]: next } });
  }

  function commitReorder(newOrder: string[]) {
    // MAM always rank 0 regardless of what the reorder produced.
    const withoutMam = newOrder.filter(n => n !== "mam");
    const withMam = ["mam", ...withoutMam];
    setDraft({ ...draft, priority: { ...draft.priority, [tab]: withMam } });
  }

  // Arrow-button reorder — up/down swap the row with its neighbor.
  // MAM is locked at rank 0; the arrow buttons are hidden on that
  // row and the surrounding rows' "up" / "down" are bounded so they
  // can't swap INTO position 0.
  function move(i: number, dir: -1 | 1) {
    const j = i + dir;
    if (j < 1 || j >= ordered.length) return;  // j < 1 keeps MAM (i=0) immovable
    if (ordered[i] === "mam" || ordered[j] === "mam") return;
    const next = [...ordered];
    [next[i], next[j]] = [next[j], next[i]];
    commitReorder(next);
  }

  return (
    <div>
      <div style={{
        display: "grid",
        gridTemplateColumns: "24px 24px 1fr 80px 80px 90px 110px",
        alignItems: "center",
        gap: "8px 12px",
        fontSize: 11, fontWeight: 700,
        color: t.textDim, textTransform: "uppercase", letterSpacing: 0.5,
        padding: "8px 4px",
        borderBottom: `1px solid ${t.borderL}`,
      }}>
        <span></span>
        <span style={{ textAlign: "right" }}>#</span>
        <span>Source</span>
        <span style={{ textAlign: "center" }}>Enrich</span>
        <span style={{ textAlign: "center" }}>Scan</span>
        <span
          style={{ textAlign: "center" }}
          title={
            "Keeps doing full-detail searches on this source until it " +
            "finds a match for every owned book — instead of " +
            "fast-pathing once any other source has a URL. Default on " +
            "for the primary tier (Goodreads / Hardcover / Audible)."
          }
        >Mandatory</span>
        <span style={{ textAlign: "center" }} title="Seconds to wait between requests (NOT queries per second)">Rate (s)</span>
      </div>

      {ordered.map((name, i) => {
        const meta = known.find(k => k.name === name);
        if (!meta) return null;
        const entry = draft.sources[name];
        if (!entry) return null;
        const locked = name === "mam";
        // Arrow buttons are bounded so they can't swap INTO slot 0
        // (MAM is pinned there). The "up" button on row 1 (first
        // non-MAM) is disabled because moving it up would collide
        // with MAM.
        const canUp = !locked && i > 1;
        const canDown = !locked && i < ordered.length - 1;
        // v2.11.1: Amazon's audiobook scan ships in this release, so
        // the Amazon extras sub-row also renders on the audiobook
        // tab — with the audiobook-specific format dropdown.
        const showAmazonExtras = name === "amazon";
        const showKoboExtras = name === "kobo" && tab === "ebook";
        const hasExtrasRow = showAmazonExtras || showKoboExtras;
        return (
          <div key={name}>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "24px 24px 1fr 80px 80px 90px 110px",
              alignItems: "center",
              gap: "8px 12px",
              padding: "8px 4px",
              borderBottom: hasExtrasRow ? "none" : `1px solid ${t.borderL}`,
              background: locked ? t.bg3 : "transparent",
            }}
          >
            {/* Up/down arrows — replaces the HTML5 drag-grip. MAM
                row shows no arrows since it's pinned. */}
            <div style={{
              display: "flex", flexDirection: "column",
              alignItems: "center", justifyContent: "center",
              lineHeight: 1,
            }}>
              {!locked && (
                <>
                  <button onClick={() => move(i, -1)} disabled={!canUp} style={{
                    background: "none", border: "none",
                    cursor: canUp ? "pointer" : "default",
                    color: canUp ? t.textDim : t.borderL,
                    fontSize: 11, padding: "0 2px",
                    opacity: canUp ? 1 : 0.4,
                  }}>▲</button>
                  <button onClick={() => move(i, 1)} disabled={!canDown} style={{
                    background: "none", border: "none",
                    cursor: canDown ? "pointer" : "default",
                    color: canDown ? t.textDim : t.borderL,
                    fontSize: 11, padding: "0 2px",
                    opacity: canDown ? 1 : 0.4,
                  }}>▼</button>
                </>
              )}
            </div>

            {/* Rank */}
            <span style={{ fontSize: 13, color: t.textDim, fontWeight: 600, textAlign: "right" }}>
              {i + 1}
            </span>

            {/* Name + badges */}
            <div style={{
              fontSize: 14, fontWeight: 600,
              color: locked ? t.textDim : t.text,
              display: "flex", alignItems: "center", gap: 8,
            }}>
              {meta.display}
              {locked && (
                <span style={{
                  fontSize: 10, fontWeight: 700, textTransform: "uppercase",
                  padding: "2px 7px", borderRadius: 99,
                  background: t.accent + "22", color: t.accent, letterSpacing: 0.4,
                }}>
                  Always first
                </span>
              )}
            </div>

            {/* Enrich — MAM shown as locked-checked since it's
                prepended per-call at enrich time whenever a torrent_id
                is available. */}
            <div style={{ display: "flex", justifyContent: "center" }} title={
              locked
                ? "MAM enriches every grab automatically via the announce torrent_id"
                : undefined
            }>
              <input
                type="checkbox"
                checked={locked ? true : Boolean(entry[enrichKey])}
                disabled={locked}
                onChange={e => !locked && setToggle(name, enrichKey, e.target.checked)}
                style={{ width: 18, height: 18, cursor: locked ? "not-allowed" : "pointer" }}
              />
            </div>

            {/* Scan */}
            <div style={{ display: "flex", justifyContent: "center" }}>
              <input
                type="checkbox"
                checked={Boolean(entry[scanKey])}
                disabled={locked}
                onChange={e => !locked && setToggle(name, scanKey, e.target.checked)}
                style={{ width: 18, height: 18, cursor: locked ? "not-allowed" : "pointer" }}
              />
            </div>

            {/* Mandatory — v2.3.2. Locked off for MAM (it's not part
                of the source-scan registry; mandatory has no effect
                there). For everyone else, governs whether
                `_lookup_author_inner` keeps DETAIL-fetching books
                missing this source's URL on every scan. */}
            <div style={{ display: "flex", justifyContent: "center" }} title={
              locked
                ? "MAM is not part of the source-scan registry; the mandatory flag has no effect."
                : undefined
            }>
              <input
                type="checkbox"
                checked={locked ? false : Boolean(entry.mandatory)}
                disabled={locked}
                onChange={e => !locked && setToggle(name, "mandatory", e.target.checked)}
                style={{ width: 18, height: 18, cursor: locked ? "not-allowed" : "pointer" }}
              />
            </div>

            {/* Rate limit */}
            <div style={{ display: "flex", justifyContent: "center" }}>
              <input
                type="number"
                min={0.1}
                max={100}
                step={0.5}
                value={Number(entry.rate_limit ?? 1)}
                onChange={e => setToggle(name, "rate_limit", parseFloat(e.target.value) || 1)}
                style={{
                  width: 70, padding: "4px 8px", textAlign: "center",
                  borderRadius: 6,
                  border: `1px solid ${t.border}`, background: t.inp,
                  color: t.text2, fontSize: 12, outline: "none",
                }}
              />
            </div>
          </div>
          {showAmazonExtras && (
            <AmazonExtrasRow
              entry={entry}
              tab={tab}
              onChange={(key, value) => setToggle(name, key, value)}
            />
          )}
          {showKoboExtras && (
            <KoboExtrasRow
              entry={entry}
              onChange={(key, value) => setToggle(name, key, value)}
            />
          )}
          </div>
        );
      })}
    </div>
  );
}

// ─── Amazon-specific sub-row ────────────────────────────────────
//
// Renders directly below the Amazon row in the Ebook tab. Lets the
// user pick which format + language Amazon's `/juvec` server-side
// filter API returns. These map onto `metadata_sources.amazon.format`
// and `.language` and round-trip through the same PUT /v1/metadata-
// sources endpoint as the rest of the panel.
//
// Why a sub-row instead of two more columns: format/language are
// Amazon-only — adding them as grid columns would force every other
// row to render placeholder cells. A sub-row keeps the grid clean.

function AmazonExtrasRow({
  entry, tab, onChange,
}: {
  entry: SourceEntry;
  tab: Tab;
  onChange: (key: keyof SourceEntry, value: string) => void;
}) {
  const t = useTheme();
  // v2.11.1: Amazon's audiobook scan ships in this release. The
  // Format dropdown swaps based on which tab the user is on —
  // Kindle/Paperback/etc. for ebook scans, Audible/Audio CD/etc.
  // for audiobook scans. Each tab writes its own settings key so
  // the user can configure both surfaces independently.
  const isAudiobook = tab === "audiobook";
  const formatKey: keyof SourceEntry = isAudiobook ? "audiobook_format" : "format";
  const formatDefault = isAudiobook ? "audible_audiobook" : "kindle";
  const formatOptions = isAudiobook
    ? AMAZON_AUDIOBOOK_FORMAT_OPTIONS
    : AMAZON_FORMAT_OPTIONS;
  const currentFormat = (isAudiobook ? entry.audiobook_format : entry.format)
    ?? formatDefault;
  return (
    <div style={{
      display: "flex", gap: 24, alignItems: "center",
      padding: "8px 4px 12px 60px",  // indent under the rank column
      borderBottom: `1px solid ${t.borderL}`,
      fontSize: 12,
    }}>
      <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ color: t.textDim, fontWeight: 600 }}>Format</span>
        <select
          value={currentFormat}
          onChange={e => onChange(formatKey, e.target.value)}
          style={{
            padding: "4px 8px", borderRadius: 6,
            border: `1px solid ${t.border}`, background: t.inp,
            color: t.text2, fontSize: 12, outline: "none",
          }}
        >
          {formatOptions.map(o => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </label>
      <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ color: t.textDim, fontWeight: 600 }}>Language</span>
        <select
          value={entry.language ?? "English"}
          onChange={e => onChange("language", e.target.value)}
          style={{
            padding: "4px 8px", borderRadius: 6,
            border: `1px solid ${t.border}`, background: t.inp,
            color: t.text2, fontSize: 12, outline: "none",
          }}
        >
          {AMAZON_LANGUAGE_OPTIONS.map(o => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </label>
      <span style={{ color: t.textDim, fontSize: 11, fontStyle: "italic" }}>
        Drives Amazon's Author Store filter — only{" "}
        {isAudiobook ? "audiobooks" : "books"} matching format +
        language are returned.
      </span>
    </div>
  );
}

// ─── Kobo-specific sub-row ──────────────────────────────────────
//
// Renders directly below the Kobo row in the Ebook tab. Lets the
// user tune the parallel detail-fetch worker count. Maps to
// `metadata_sources.kobo.concurrency` and flows through reload_sources
// to the live KoboSource singleton.
//
// Effective request rate = ~concurrency/rate_limit. At ship-defaults
// (4 / 3.0 = 1.33 req/s) Kobo stays below the Cloudflare-fronted
// soft-block threshold. Raising concurrency without also raising
// rate_limit will trigger soft-blocks — call out the multiplication
// in the help text so power users don't shoot themselves in the foot.

function KoboExtrasRow({
  entry, onChange,
}: {
  entry: SourceEntry;
  onChange: (key: keyof SourceEntry, value: number) => void;
}) {
  const t = useTheme();
  const concurrency = entry.concurrency ?? 4;
  const rateLimit = entry.rate_limit ?? 3.0;
  const effectiveRate = rateLimit > 0 ? concurrency / rateLimit : 0;
  return (
    <div style={{
      display: "flex", gap: 24, alignItems: "center",
      padding: "8px 4px 12px 60px",
      borderBottom: `1px solid ${t.borderL}`,
      fontSize: 12,
    }}>
      <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ color: t.textDim, fontWeight: 600 }}>Concurrency</span>
        <input
          type="number"
          min={1}
          max={16}
          step={1}
          value={concurrency}
          onChange={e => onChange("concurrency", parseInt(e.target.value) || 1)}
          style={{
            width: 60, padding: "4px 8px", textAlign: "center",
            borderRadius: 6,
            border: `1px solid ${t.border}`, background: t.inp,
            color: t.text2, fontSize: 12, outline: "none",
          }}
        />
      </label>
      <span style={{ color: t.textDim, fontSize: 11, fontStyle: "italic" }}>
        Parallel detail-fetch workers. Effective rate ≈
        {" "}{effectiveRate.toFixed(2)} req/s ({concurrency} workers ÷
        {" "}{rateLimit}s each). Raising concurrency without raising
        Rate triggers Cloudflare soft-blocks.
      </span>
    </div>
  );
}


// ─── Banner ───────────────────────────────────────────────────

function Banner({ tone, children }: { tone: "ok" | "err"; children: React.ReactNode }) {
  const t = useTheme();
  const color = tone === "ok" ? t.ok : t.err;
  return (
    <div style={{
      background: color + "22",
      border: `1px solid ${color}55`,
      color, padding: "10px 14px", borderRadius: 8, fontSize: 13,
    }}>
      {children}
    </div>
  );
}
