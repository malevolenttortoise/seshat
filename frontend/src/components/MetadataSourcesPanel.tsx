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
  // Declared up here (not next to resetToDefaults) so the hook count
  // is stable across renders — moving it below the loading-state
  // early return triggers React #310.
  const [resetting, setResetting] = useState(false);

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

      {/* v2.13.0 migration prompt — existing users whose saved
          goodreads rate is still on the pre-v2.13.0 default (2.0s)
          should consider bumping to the new 5.0s default for the
          Phase-A Cloudflare bypass. One-click bump avoids forcing
          them through the full "Reset to defaults" workflow. */}
      {draft.sources.goodreads && Number(draft.sources.goodreads.rate_limit ?? 5) < 5 && (
        <div style={{
          background: t.accent + "18",
          border: `1px solid ${t.accent}55`,
          color: t.text, padding: "10px 14px", borderRadius: 8,
          fontSize: 13, display: "flex", gap: 12,
          alignItems: "center", flexWrap: "wrap",
        }}>
          <span style={{ flex: 1 }}>
            <b>v2.13.0:</b> Goodreads's default rate limit is now <b>5.0s</b>{" "}
            (was 2.0s). The slower pace lets the new Cloudflare bypass run
            cleanly under burst load. Your current setting is{" "}
            <b>{draft.sources.goodreads.rate_limit}s</b>.
          </span>
          <Btn
            onClick={() => {
              setDraft(d => d ? {
                ...d,
                sources: {
                  ...d.sources,
                  goodreads: { ...d.sources.goodreads, rate_limit: 5.0 },
                },
              } : d);
            }}
          >
            Bump to 5.0s
          </Btn>
        </div>
      )}

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
        // v2.13.0 Stage 6 — Goodreads gets a status + probe panel below
        // its row on both tabs. The Goodreads session-state flag is
        // global across content types, so we render the same card on
        // the audiobook tab too (Cloudflare doesn't care which scan
        // tripped the gate).
        const showGoodreadsExtras = name === "goodreads";
        const hasExtrasRow = showAmazonExtras || showKoboExtras || showGoodreadsExtras;
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
            <>
              <AmazonExtrasRow
                entry={entry}
                tab={tab}
                onChange={(key, value) => setToggle(name, key, value)}
              />
              <AmazonCacheStatusCard />
            </>
          )}
          {showKoboExtras && (
            <KoboExtrasRow
              entry={entry}
              onChange={(key, value) => setToggle(name, key, value)}
            />
          )}
          {showGoodreadsExtras && <GoodreadsStatusCard />}
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


// ─── Goodreads status + probe panel (v2.13.0 Stage 6 Phase A) ───
//
// Renders directly below the Goodreads row in both tabs. Provides:
//   - Status pill: Active / Soft-blocked / Unknown
//   - Run probe button — single GET to /book/show/237832459
//   - Run burst button — 10 GETs against the canonical pool
//   - Mark as active — manual flag reset after investigation
//
// Phase A: NO cookie input fields. The probe is the diagnostic for
// "is the Chrome120 fingerprint alone enough?" — if Phase A UAT
// shows 202s under burst, v2.13.1 adds the encrypted cookie panel
// here.

type GoodreadsState = {
  state: "active" | "soft_blocked" | "unknown";
  since: number | null;
  last_status: number | null;
};

type ProbeRequestResult = {
  goodreads_id: string;
  status: number;
  body_size_kb: number;
  wall_ms: number;
  soft_blocked: boolean;
};

type ProbeBurstSummary = {
  requests: number;
  status_distribution: Record<string, number>;
  soft_blocks: number;
  total_wall_s: number;
  mean_body_kb: number;
  per_request: ProbeRequestResult[];
};

type ProbeResponse = {
  mode: "single" | "burst";
  state_after: GoodreadsState;
  single?: ProbeRequestResult;
  burst?: ProbeBurstSummary;
};


function GoodreadsStatusCard() {
  const t = useTheme();
  const [state, setState] = useState<GoodreadsState | null>(null);
  const [running, setRunning] = useState<null | "single" | "burst">(null);
  const [lastResult, setLastResult] = useState<ProbeResponse | null>(null);
  const [err, setErr] = useState<string>("");

  // Initial state fetch + refresh after any probe.
  useEffect(() => {
    let cancelled = false;
    api.get<GoodreadsState>("/v1/metadata/goodreads/state").then(s => {
      if (!cancelled) setState(s);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, []);

  async function runProbe(mode: "single" | "burst") {
    setRunning(mode);
    setErr("");
    try {
      const r = await api.post<ProbeResponse>(
        "/v1/metadata/goodreads/test", { mode },
      );
      setLastResult(r);
      setState(r.state_after);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Probe failed");
    } finally {
      setRunning(null);
    }
  }

  async function clearFlag() {
    setErr("");
    try {
      const r = await api.post<{ state_after: GoodreadsState }>(
        "/v1/metadata/goodreads/mark-active",
      );
      setState(r.state_after);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to clear flag");
    }
  }

  const pillColor =
    state?.state === "active" ? t.ok :
    state?.state === "soft_blocked" ? t.err : t.textDim;
  const pillLabel =
    state?.state === "active" ? "Active" :
    state?.state === "soft_blocked" ? "Soft-blocked" : "Unknown";
  const sinceText = state?.since
    ? new Date(state.since * 1000).toLocaleString()
    : null;

  return (
    <div style={{
      display: "flex", flexDirection: "column", gap: 10,
      padding: "10px 4px 14px 60px",
      borderBottom: `1px solid ${t.borderL}`,
      fontSize: 12,
    }}>
      {/* Status pill + last-seen */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <span style={{ color: t.textDim, fontWeight: 600 }}>Session state</span>
        <span style={{
          fontSize: 11, fontWeight: 700, textTransform: "uppercase",
          padding: "3px 9px", borderRadius: 99, letterSpacing: 0.5,
          background: pillColor + "22", color: pillColor,
        }}>{pillLabel}</span>
        {state?.last_status != null && (
          <span style={{ color: t.textDim, fontSize: 11 }}>
            last HTTP {state.last_status}
          </span>
        )}
        {sinceText && state?.state === "soft_blocked" && (
          <span style={{ color: t.textDim, fontSize: 11 }}>
            since {sinceText}
          </span>
        )}
      </div>

      {/* Action buttons */}
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <Btn
          onClick={() => runProbe("single")}
          disabled={running !== null}
        >
          {running === "single" ? <Spin /> : "Run probe"}
        </Btn>
        <Btn
          onClick={() => runProbe("burst")}
          disabled={running !== null}
        >
          {running === "burst" ? <Spin /> : "Run burst (10×)"}
        </Btn>
        {state?.state === "soft_blocked" && (
          <Btn onClick={clearFlag} disabled={running !== null}>
            Mark as active
          </Btn>
        )}
        {running === "burst" && (
          <span style={{ color: t.textDim, fontSize: 11, fontStyle: "italic" }}>
            ~50s with default 5s rate-limit
          </span>
        )}
      </div>

      {/* Error banner */}
      {err && (
        <div style={{
          background: t.err + "22", border: `1px solid ${t.err}55`,
          color: t.err, padding: "6px 10px", borderRadius: 6, fontSize: 12,
        }}>
          {err}
        </div>
      )}

      {/* Result panel */}
      {lastResult && (
        <div style={{
          background: t.bg3, border: `1px solid ${t.borderL}`,
          borderRadius: 6, padding: "8px 12px", fontSize: 12,
        }}>
          {lastResult.mode === "single" && lastResult.single && (
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
              <span><b>HTTP</b> {lastResult.single.status}</span>
              <span><b>{lastResult.single.body_size_kb}KB</b></span>
              <span>{lastResult.single.wall_ms}ms</span>
              {lastResult.single.soft_blocked && (
                <span style={{ color: t.err, fontWeight: 600 }}>SOFT-BLOCK</span>
              )}
            </div>
          )}
          {lastResult.mode === "burst" && lastResult.burst && (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
                <span><b>{lastResult.burst.requests}</b> requests</span>
                <span style={{
                  color: lastResult.burst.soft_blocks > 0 ? t.err : t.ok,
                  fontWeight: 600,
                }}>
                  {lastResult.burst.soft_blocks} soft-blocks
                </span>
                <span>{lastResult.burst.total_wall_s}s total</span>
                <span>mean {lastResult.burst.mean_body_kb}KB / response</span>
              </div>
              <div style={{ color: t.textDim, fontSize: 11 }}>
                statuses: {Object.entries(lastResult.burst.status_distribution)
                  .map(([s, n]) => `${s}×${n}`).join(", ")}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Help text */}
      <div style={{ color: t.textDim, fontSize: 11, fontStyle: "italic", lineHeight: 1.4 }}>
        Phase A bypass: curl_cffi Chrome120 TLS impersonation. Run probe
        for a quick connectivity check; run burst to verify density holds
        under realistic scan load. Soft-block during the burst means
        Cloudflare is rejecting on request density — try raising the rate
        limit above (8s+ is conservative) or wait a few hours for the
        bot-score to decay, then re-test.
      </div>
    </div>
  );
}


// ─── Amazon metadata cache status card (v2.21.0 Phase E) ─────────
//
// Renders directly below AmazonExtrasRow in the Ebook tab. Shows the
// background worker's live state, exposes the enable/disable toggle,
// and offers an emergency "Reset cooldown" button when the IP-level
// penalty box is engaged.
//
// Polls /api/v1/metadata-cache/amazon/status every 30s when the
// component is mounted. The status payload covers worker state
// (heartbeat, scans/blocks today), queue stats, cache row counts,
// and the cooldown flag.

type CacheWorkerStatus = {
  last_block_at: number;
  block_cooldown_s: number;
  consecutive_blocks: number;
  last_heartbeat_at: number | null;
  last_scan_completed_at: number | null;
  today_scan_count: number;
  today_block_count: number;
  seconds_since_heartbeat: number | null;
  seconds_since_scan_completed: number | null;
};

type CacheQueueStats = {
  total: number;
  pending: number;
  in_progress: number;
  failed_permanent: number;
  other: number;
};

type CacheStats = {
  state_rows: number;
  books_rows: number;
  ok_authors: number;
  error_authors: number;
};

type CacheCooldown = {
  blocked: boolean;
  remaining_s: number;
  reason: string | null;
};

type CacheSchedule = {
  active_hours: string;  // "HH:MM-HH:MM"
  timezone: string;      // IANA tz name; "" = system local
};

type CacheMode = "continuous" | "scheduled" | "disabled";

type CacheStatusResponse = {
  source: string;
  enabled: boolean;
  mode: CacheMode;
  schedule: CacheSchedule;
  inside_schedule_window: boolean;
  seconds_until_window_open: number;
  cooldown: CacheCooldown;
  worker: CacheWorkerStatus;
  queue: CacheQueueStats;
  cache: CacheStats;
};

type CacheSettingsResponse = {
  ok: boolean;
  source: string;
  enabled: boolean;
  mode: CacheMode;
  schedule: CacheSchedule;
};

type ResetCooldownResponse = {
  ok: boolean;
  source: string;
  previously_blocked: boolean;
  previous_remaining_s: number;
};


function _formatSecondsAgo(s: number | null): string {
  if (s === null || s === undefined) return "never";
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h ago`;
  return `${(s / 86400).toFixed(1)}d ago`;
}

function _formatCooldown(s: number): string {
  if (s <= 0) return "clear";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}


function AmazonCacheStatusCard() {
  const t = useTheme();
  const [status, setStatus] = useState<CacheStatusResponse | null>(null);
  const [busy, setBusy] = useState<null | "mode" | "schedule" | "reset">(null);
  const [err, setErr] = useState<string>("");
  // Local-edit buffer for the active-hours field — committed via the
  // Save button rather than every keystroke so we don't ping the
  // backend with `08:0` mid-typing (which would 400 validation).
  const [pendingHours, setPendingHours] = useState<string>("");
  const [pendingTz, setPendingTz] = useState<string>("");
  // Track whether the user has dirtied the inputs vs the server state
  // so the Save button can stay disabled when there's nothing to save.
  const [scheduleDirty, setScheduleDirty] = useState<boolean>(false);

  // Poll every 30s while mounted. Heartbeat-staleness checks rely on
  // a recent reading; faster polling burns CPU for no real-world
  // benefit (worker iterations are ≥30s by design).
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setInterval> | null = null;
    const fetchStatus = async () => {
      try {
        const r = await api.get<CacheStatusResponse>(
          "/v1/metadata-cache/amazon/status",
        );
        if (cancelled) return;
        setStatus(r);
        // Seed the local edit buffer on first load — and on every
        // subsequent poll where the user hasn't started editing
        // (dirty flag stays false until they touch an input). This
        // way a remote change (e.g. via PATCH from another tab)
        // surfaces in the inputs.
        setPendingHours((prev) =>
          scheduleDirty ? prev : r.schedule.active_hours,
        );
        setPendingTz((prev) => (scheduleDirty ? prev : r.schedule.timezone));
      } catch (e) {
        if (!cancelled && !api.isAbort(e)) {
          setErr(e instanceof Error ? e.message : "Status fetch failed");
        }
      }
    };
    fetchStatus();
    timer = setInterval(fetchStatus, 30_000);
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, [scheduleDirty]);

  async function setMode(nextMode: CacheMode) {
    if (status === null || nextMode === status.mode) return;
    setBusy("mode");
    setErr("");
    try {
      const r = await api.patch<CacheSettingsResponse>(
        "/v1/metadata-cache/amazon/settings",
        { mode: nextMode },
      );
      // Optimistic: update mode + enabled (kept in sync server-side)
      // so the pill flips without waiting for the next poll.
      setStatus({
        ...status,
        enabled: r.enabled,
        mode: r.mode,
        schedule: r.schedule,
      });
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Mode update failed");
    } finally {
      setBusy(null);
    }
  }

  async function saveSchedule() {
    if (status === null) return;
    setBusy("schedule");
    setErr("");
    try {
      const r = await api.patch<CacheSettingsResponse>(
        "/v1/metadata-cache/amazon/settings",
        {
          schedule: {
            active_hours: pendingHours.trim(),
            timezone: pendingTz.trim(),
          },
        },
      );
      setStatus({ ...status, schedule: r.schedule });
      setScheduleDirty(false);
    } catch (e) {
      // Backend returns 400 with detail on invalid spec — surface it.
      setErr(e instanceof Error ? e.message : "Save failed");
    } finally {
      setBusy(null);
    }
  }

  async function resetCooldown() {
    setBusy("reset");
    setErr("");
    try {
      await api.post<ResetCooldownResponse>(
        "/v1/metadata-cache/amazon/reset-cooldown",
      );
      // Force-refresh status so the cooldown banner clears.
      const r = await api.get<CacheStatusResponse>(
        "/v1/metadata-cache/amazon/status",
      );
      setStatus(r);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Reset failed");
    } finally {
      setBusy(null);
    }
  }

  if (status === null) {
    return (
      <div style={{
        padding: "10px 4px 14px 60px",
        borderBottom: `1px solid ${t.borderL}`,
        color: t.textDim, fontSize: 12,
      }}>
        Loading cache status…
      </div>
    );
  }

  // Status pill: cooldown (red) > disabled (gray) > outside-schedule
  // (gray-ish) > stalled (amber) > active (green). Stalled = enabled,
  // inside-window, but >5min since the last heartbeat — the worker
  // should be ticking at 30-90s, so 5min of silence is the supervised
  // task crashed. Outside-schedule is gray (intentional sleep) not
  // amber (looks like a problem).
  const stalled = status.enabled
    && status.inside_schedule_window
    && (status.worker.seconds_since_heartbeat === null
        || status.worker.seconds_since_heartbeat > 300);
  let pillColor: string;
  let pillLabel: string;
  if (status.cooldown.blocked) {
    pillColor = t.err;
    pillLabel = "Cooldown";
  } else if (status.mode === "disabled" || !status.enabled) {
    pillColor = t.textDim;
    pillLabel = "Disabled";
  } else if (status.mode === "scheduled" && !status.inside_schedule_window) {
    pillColor = t.textDim;
    pillLabel = "Off-hours";
  } else if (stalled) {
    pillColor = "#cc9933";  // amber — pre-existing palette has no warning tone
    pillLabel = "Stalled";
  } else {
    pillColor = t.ok;
    pillLabel = "Active";
  }

  const MODES: Array<{ key: CacheMode; label: string; desc: string }> = [
    { key: "continuous", label: "Continuous",
      desc: "Worker fires every 30–90s round the clock." },
    { key: "scheduled", label: "Scheduled",
      desc: "Worker only runs inside the active-hours window below." },
    { key: "disabled", label: "Disabled",
      desc: "Worker stays idle; cache reads still hit existing rows." },
  ];

  return (
    <div style={{
      display: "flex", flexDirection: "column", gap: 10,
      padding: "10px 4px 14px 60px",
      borderBottom: `1px solid ${t.borderL}`,
      fontSize: 12,
    }}>
      {/* Header: status pill + label */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <span style={{ color: t.textDim, fontWeight: 600 }}>
          Cache worker
        </span>
        <span style={{
          fontSize: 11, fontWeight: 700, textTransform: "uppercase",
          padding: "3px 9px", borderRadius: 99, letterSpacing: 0.5,
          background: pillColor + "22", color: pillColor,
        }}>{pillLabel}</span>
        {status.cooldown.blocked && (
          <span style={{ color: t.textDim, fontSize: 11 }}>
            clears in {_formatCooldown(status.cooldown.remaining_s)}
          </span>
        )}
        <span style={{ color: t.textDim, fontSize: 11, marginLeft: "auto" }}>
          heartbeat {_formatSecondsAgo(status.worker.seconds_since_heartbeat)}
        </span>
      </div>

      {/* Live-stats panel */}
      <div style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
        gap: 8,
        background: t.bg3, border: `1px solid ${t.borderL}`,
        borderRadius: 6, padding: "8px 12px",
      }}>
        <StatTile label="Queue (pending)" value={status.queue.pending.toLocaleString()} />
        <StatTile
          label="Cached authors"
          value={`${status.cache.ok_authors.toLocaleString()} / ${status.cache.state_rows.toLocaleString()}`}
          hint="ok / total state rows"
        />
        <StatTile
          label="Cached books"
          value={status.cache.books_rows.toLocaleString()}
        />
        <StatTile
          label="Scans today"
          value={status.worker.today_scan_count.toLocaleString()}
        />
        <StatTile
          label="Blocks today"
          value={status.worker.today_block_count.toLocaleString()}
          tone={status.worker.today_block_count > 0 ? "warn" : undefined}
        />
        <StatTile
          label="In progress"
          value={status.queue.in_progress.toLocaleString()}
          tone={status.queue.in_progress > 1 ? "warn" : undefined}
          hint={
            status.queue.in_progress > 1
              ? "should normally be 0-1; >1 hints at a stuck row"
              : undefined
          }
        />
        <StatTile
          label="Failed permanent"
          value={status.queue.failed_permanent.toLocaleString()}
          tone={status.queue.failed_permanent > 0 ? "err" : undefined}
        />
        <StatTile
          label="Last scan"
          value={_formatSecondsAgo(status.worker.seconds_since_scan_completed)}
        />
      </div>

      {/* Mode selector (segmented control) */}
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <div style={{ fontSize: 11, color: t.textDim, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5 }}>
          Mode
        </div>
        <div style={{ display: "flex", gap: 0, alignItems: "stretch", flexWrap: "wrap" }}>
          {MODES.map((m, idx) => {
            const selected = status.mode === m.key;
            return (
              <button
                key={m.key}
                onClick={() => setMode(m.key)}
                disabled={busy !== null || selected}
                title={m.desc}
                style={{
                  flex: "1 1 0", minWidth: 0,
                  padding: "6px 10px",
                  fontSize: 12, fontWeight: 600,
                  background: selected ? t.accent + "22" : t.bg3,
                  color: selected ? t.accent : t.text,
                  border: `1px solid ${selected ? t.accent : t.borderL}`,
                  borderLeftWidth: idx === 0 ? 1 : 0,
                  borderTopLeftRadius: idx === 0 ? 6 : 0,
                  borderBottomLeftRadius: idx === 0 ? 6 : 0,
                  borderTopRightRadius: idx === MODES.length - 1 ? 6 : 0,
                  borderBottomRightRadius: idx === MODES.length - 1 ? 6 : 0,
                  cursor: busy !== null || selected ? "default" : "pointer",
                  opacity: busy !== null && !selected ? 0.5 : 1,
                }}
              >
                {busy === "mode" && selected ? <Spin /> : m.label}
              </button>
            );
          })}
        </div>
        <div style={{ fontSize: 11, color: t.textDim, lineHeight: 1.5 }}>
          {MODES.find((m) => m.key === status.mode)?.desc}
        </div>
      </div>

      {/* Schedule editor — only relevant when mode=scheduled */}
      {status.mode === "scheduled" && (
        <div style={{
          display: "flex", flexDirection: "column", gap: 8,
          background: t.bg3, border: `1px solid ${t.borderL}`,
          borderRadius: 6, padding: "10px 12px",
        }}>
          <div style={{ fontSize: 11, color: t.textDim, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5 }}>
            Active hours
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            <input
              type="text"
              value={pendingHours}
              onChange={(e) => {
                setPendingHours(e.target.value);
                setScheduleDirty(true);
              }}
              placeholder="10:00-22:00"
              style={{
                fontFamily: "monospace", fontSize: 13,
                padding: "5px 8px", width: 130,
                background: t.bg2, color: t.text,
                border: `1px solid ${t.borderL}`, borderRadius: 4,
              }}
            />
            <input
              type="text"
              value={pendingTz}
              onChange={(e) => {
                setPendingTz(e.target.value);
                setScheduleDirty(true);
              }}
              placeholder="timezone (blank = system)"
              style={{
                fontSize: 12, padding: "5px 8px", flex: "1 1 200px",
                minWidth: 180,
                background: t.bg2, color: t.text,
                border: `1px solid ${t.borderL}`, borderRadius: 4,
              }}
            />
            <Btn
              onClick={saveSchedule}
              disabled={busy !== null || !scheduleDirty}
            >
              {busy === "schedule" ? <Spin /> : "Save"}
            </Btn>
          </div>
          <div style={{ fontSize: 11, color: t.textDim, lineHeight: 1.4 }}>
            Format <code>HH:MM-HH:MM</code> (24-hour). Overnight windows
            allowed (start &gt; end, e.g. <code>22:00-06:00</code>).
            Timezone accepts IANA names like <code>America/Detroit</code>;
            blank uses the system local time.
            {!status.inside_schedule_window && status.seconds_until_window_open > 0 && (
              <> Currently outside the window — next open in {_formatCooldown(status.seconds_until_window_open)}.</>
            )}
          </div>
        </div>
      )}

      {/* Action buttons */}
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        {status.cooldown.blocked && (
          <Btn
            onClick={resetCooldown}
            disabled={busy !== null}
          >
            {busy === "reset" ? <Spin /> : "Reset cooldown"}
          </Btn>
        )}
      </div>

      {/* Cooldown reason banner (info, not error) */}
      {status.cooldown.blocked && status.cooldown.reason && (
        <div style={{
          background: t.bg3, border: `1px solid ${t.borderL}`,
          color: t.textDim, padding: "6px 10px", borderRadius: 6, fontSize: 11,
        }}>
          <b>Last block:</b> {status.cooldown.reason}
        </div>
      )}

      {/* Error banner */}
      {err && (
        <div style={{
          background: t.err + "22", border: `1px solid ${t.err}55`,
          color: t.err, padding: "6px 10px", borderRadius: 6, fontSize: 12,
        }}>
          {err}
        </div>
      )}

      {/* Help text */}
      <div style={{ color: t.textDim, fontSize: 11, fontStyle: "italic", lineHeight: 1.4 }}>
        The background worker drains the cache queue at humanized cadence
        (30-90s jitter). Synchronous scans always read from this cache —
        Amazon is never hit live during a user-triggered scan, so soft-
        block cascades can't spill into other sources. Pick <b>Disabled</b>
        to pause the worker without disabling Amazon as a metadata source;
        <b>Scheduled</b> restricts the worker to user-chosen hours and
        keeps a respectful presence overnight.
      </div>
    </div>
  );
}


function StatTile({
  label, value, hint, tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "warn" | "err";
}) {
  const t = useTheme();
  const color = tone === "err" ? t.err
              : tone === "warn" ? "#cc9933"
              : t.text2;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
      <span style={{
        color: t.textDim, fontSize: 10, fontWeight: 700,
        textTransform: "uppercase", letterSpacing: 0.5,
      }}>{label}</span>
      <span style={{ color, fontSize: 14, fontWeight: 600 }}>{value}</span>
      {hint && (
        <span style={{ color: t.textDim, fontSize: 10, fontStyle: "italic" }}>
          {hint}
        </span>
      )}
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
