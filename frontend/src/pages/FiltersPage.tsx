// FiltersPage — edit the torrent filter rules that gate every announce.
//
// Reads MAM enums from /api/v1/enums (categories, languages, formats)
// and the current filter settings from /api/v1/settings. Renders each
// filter as a multi-select checklist. Changes are collected in a local
// draft and sent as a single sparse PATCH on Save.
//
// All values are stored in their **normalized** form (lowercase,
// punctuation collapsed) so they compare correctly against live IRC
// announces. The display labels come from the enums endpoint.
import { useMemo } from "react";
import { Btn } from "../components/Btn";
import { Section } from "../components/Section";
import { Spin } from "../components/Spin";
import { useFilterSettings } from "../hooks/useFilterSettings";
import { useTheme } from "../theme";
import { useViewport } from "../hooks/useViewport";
import { useMobileCodepath } from "../components/mobile";
import MobileFiltersPage from "./MobileFiltersPage";

export default function FiltersPage() {
  const vp = useViewport();
  if (useMobileCodepath(vp)) return <MobileFiltersPage />;
  return <DesktopFiltersPage />;
}

function DesktopFiltersPage() {
  const theme = useTheme();
  const {
    enums, settings, draft, discard, error, ok, saving, effective, setField, save, catGroups, allowedCats, allowedAudiobookCats, excludedCats, allowedLangs, allowedFormats, excludedFormats, acceptAudiobooks, formatPriority,
  } = useFilterSettings();

  if (!enums || !settings) {
    return (
      <div style={{ display: "flex", justifyContent: "center", padding: 40 }}>
        <Spin />
      </div>
    );
  }

  function toggleInSet(
    settingKey: string,
    current: Set<string>,
    value: string,
  ) {
    const next = new Set(current);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    setField(settingKey, [...next]);
  }

  // Route audiobook category chips to the separate
  // `allowed_audiobook_categories` list so the user can toggle
  // audiobook acceptance at the filter level without mutating their
  // ebook category selections.
  function isAudiobookGroup(mainName: string): boolean {
    return mainName.toLowerCase().startsWith("audio");
  }

  const dirty = Object.keys(draft).length;

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
        Filters
      </h1>
      <p style={{ fontSize: 14, color: theme.textDim, marginBottom: 20 }}>
        Control which MAM announces pass the filter gate. Unchecked items
        are silently skipped. Changes take effect immediately after Save.
      </p>

      {error && <Banner tone="err">{error}</Banner>}
      {ok && <Banner tone="ok">{ok}</Banner>}

      {/* ── Categories ─────────────────────────────────────── */}
      <Section
        title="Allowed Categories"
        subtitle={`${allowedCats.size} ebook + ${allowedAudiobookCats.size} audiobook selected. Empty = accept all that pass the format gate.`}
      >
        {Object.entries(catGroups).map(([mainName, cats]) => {
          const isAudio = isAudiobookGroup(mainName);
          const activeSet = isAudio ? allowedAudiobookCats : allowedCats;
          const settingKey = isAudio
            ? "allowed_audiobook_categories"
            : "allowed_categories";
          const dimmed = isAudio && !acceptAudiobooks;
          return (
            <div key={mainName} style={{ marginBottom: 16, opacity: dimmed ? 0.4 : 1 }}>
              <h4
                style={{
                  fontSize: 13,
                  fontWeight: 700,
                  color: theme.text2,
                  marginBottom: 8,
                  textTransform: "uppercase",
                  letterSpacing: 0.4,
                }}
              >
                {mainName}
                {isAudio && !acceptAudiobooks && (
                  <span style={{
                    marginLeft: 8, fontSize: 11, fontWeight: 500,
                    color: theme.textDim, textTransform: "none",
                    letterSpacing: 0,
                  }}>
                    (add "audiobooks" to the Media Type filter below to enable)
                  </span>
                )}
              </h4>
              <div
                style={{
                  display: "flex",
                  flexWrap: "wrap",
                  gap: 6,
                }}
              >
                {cats.map((c) => (
                  <Chip
                    key={c.normalized}
                    label={c.name}
                    active={activeSet.has(c.normalized)}
                    excluded={excludedCats.has(c.normalized)}
                    onClick={() =>
                      toggleInSet(settingKey, activeSet, c.normalized)
                    }
                    onRightClick={() =>
                      toggleInSet(
                        "excluded_categories",
                        excludedCats,
                        c.normalized,
                      )
                    }
                  />
                ))}
              </div>
            </div>
          );
        })}
        <p style={{ fontSize: 11, color: theme.textDim, marginTop: 8 }}>
          Click to toggle allowed. Right-click to toggle excluded (red
          strike-through = excluded even if parent format is allowed).
        </p>
      </Section>

      {/* ── Media Type ─────────────────────────────────────── */}
      <Section
        title="Media Type"
        subtitle="High-level media gate. Empty = accept all. Adding 'audiobooks' here also enables audiobook subcategories above."
      >
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {enums.formats.map((f) => (
            <Chip
              key={f}
              label={f}
              active={allowedFormats.has(f)}
              excluded={excludedFormats.has(f)}
              onClick={() =>
                toggleInSet("allowed_formats", allowedFormats, f)
              }
              onRightClick={() =>
                toggleInSet("excluded_formats", excludedFormats, f)
              }
            />
          ))}
        </div>
      </Section>

      {/* ── Format Priority (v2.9.0) ───────────────────────── */}
      <Section
        title="Format Priority"
        subtitle="Per-media-type priority for dedup. Enabled formats always grab; disabled formats are held briefly to see if a higher-priority sibling arrives, then grabbed if none does."
      >
        <FormatPrioritySection
          formatPriority={formatPriority}
          onChange={(next) => setField("format_priority", next)}
        />
        <p style={{ fontSize: 11, color: theme.textDim, marginTop: 12 }}>
          Top of each list = highest priority. Enabled = grab immediately.
          Disabled = hold briefly (in case a higher-priority sibling
          arrives), then grab if alone. The hold window is{" "}
          {Math.round((effective.format_dedup_hold_seconds as number ?? 600) / 60)} min
          (configurable in Settings).
        </p>
      </Section>

      {/* ── Languages ──────────────────────────────────────── */}
      <Section
        title="Allowed Languages"
        subtitle={`${allowedLangs.size} selected. Empty = accept all languages.`}
      >
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {enums.languages.map((l) => (
            <Chip
              key={l}
              label={l}
              active={allowedLangs.has(l)}
              onClick={() =>
                toggleInSet("allowed_languages", allowedLangs, l)
              }
            />
          ))}
        </div>
      </Section>

      {/* ── Sticky save bar ────────────────────────────────── */}
      <div
        style={{
          position: "sticky",
          bottom: 20,
          display: "flex",
          justifyContent: "flex-end",
          gap: 10,
          background: theme.bg + "ee",
          backdropFilter: "blur(8px)",
          padding: "12px 0",
          borderTop: `1px solid ${theme.borderL}`,
          marginTop: 20,
        }}
      >
        <span
          style={{
            fontSize: 13,
            color: theme.textDim,
            alignSelf: "center",
          }}
        >
          {dirty > 0
            ? `${dirty} unsaved change(s)`
            : "No unsaved changes"}
        </span>
        <Btn
          variant="ghost"
          disabled={dirty === 0 || saving}
          onClick={discard}
        >
          Discard
        </Btn>
        <Btn
          variant="primary"
          disabled={dirty === 0 || saving}
          onClick={save}
        >
          {saving ? <Spin size={14} /> : "Save"}
        </Btn>
      </div>
    </div>
  );
}

// v2.9.0 — Format Priority editor. Renders one sub-card per media
// type ("ebook" / "audiobook" today) with a draggable, toggleable
// list of {fmt, enabled} entries. Top-of-list = highest priority.
// Up/down arrows for keyboard-accessible reorder mirrors the existing
// FormatPriorityList in SettingsPage.tsx; the enabled checkbox is
// the v2.9.0-specific addition that drives grab-now-vs-hold behavior.
type FmtEntry = { fmt: string; enabled: boolean };

function FormatPrioritySection({
  formatPriority,
  onChange,
}: {
  formatPriority: Record<string, FmtEntry[]>;
  onChange: (next: Record<string, FmtEntry[]>) => void;
}) {
  const theme = useTheme();
  // Iterate media types in a stable, user-friendly order — ebook
  // first, audiobook second, then anything else the backend ever
  // adds (forward-compat for comics etc).
  const orderedKeys = useMemo(() => {
    const known = ["ebook", "audiobook"];
    const others = Object.keys(formatPriority).filter(
      (k) => !known.includes(k),
    );
    return [...known.filter((k) => k in formatPriority), ...others];
  }, [formatPriority]);

  function updateMedia(media: string, next: FmtEntry[]) {
    onChange({ ...formatPriority, [media]: next });
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {orderedKeys.map((media) => {
        const entries = formatPriority[media] ?? [];
        return (
          <div
            key={media}
            style={{
              background: theme.bg3,
              border: `1px solid ${theme.borderL}`,
              borderRadius: 8,
              padding: "12px 14px",
            }}
          >
            <h4
              style={{
                fontSize: 13,
                fontWeight: 700,
                color: theme.text2,
                marginTop: 0,
                marginBottom: 10,
                textTransform: "uppercase",
                letterSpacing: 0.4,
              }}
            >
              {media} Formats
            </h4>
            <FormatPriorityList
              entries={entries}
              onChange={(next) => updateMedia(media, next)}
            />
          </div>
        );
      })}
    </div>
  );
}

function FormatPriorityList({
  entries,
  onChange,
}: {
  entries: FmtEntry[];
  onChange: (next: FmtEntry[]) => void;
}) {
  const theme = useTheme();
  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= entries.length) return;
    const next = [...entries];
    [next[i], next[j]] = [next[j], next[i]];
    onChange(next);
  };
  const toggle = (i: number) => {
    const next = entries.map((e, idx) =>
      idx === i ? { ...e, enabled: !e.enabled } : e,
    );
    onChange(next);
  };
  if (entries.length === 0) {
    return (
      <p style={{ fontSize: 12, color: theme.textDim }}>
        No formats configured for this media type.
      </p>
    );
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      {entries.map((entry, i) => {
        const isTop = i === 0;
        return (
          <div
            key={entry.fmt}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: "6px 12px",
              borderRadius: 6,
              background: isTop ? theme.abg : theme.bg,
              border: `1px solid ${isTop ? theme.abr : theme.borderL}`,
            }}
          >
            <span
              style={{
                fontSize: 12,
                fontWeight: 700,
                color: isTop ? theme.accent : theme.td,
                width: 20,
              }}
            >
              {i + 1}.
            </span>
            <span
              style={{
                fontSize: 14,
                fontWeight: 500,
                color: isTop ? theme.accent : theme.text2,
                flex: 1,
                textTransform: "uppercase",
                letterSpacing: 0.4,
              }}
            >
              {entry.fmt}
            </span>
            <label
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                fontSize: 12,
                color: theme.textDim,
                cursor: "pointer",
                userSelect: "none",
              }}
            >
              <input
                type="checkbox"
                checked={entry.enabled}
                onChange={() => toggle(i)}
              />
              Enabled
            </label>
            <button
              onClick={() => move(i, -1)}
              disabled={isTop}
              style={{
                background: "none",
                border: "none",
                cursor: isTop ? "default" : "pointer",
                color: isTop ? theme.tg : theme.td,
                fontSize: 14,
                padding: "0 4px",
                opacity: isTop ? 0.3 : 1,
              }}
              aria-label="Move up"
            >
              ▲
            </button>
            <button
              onClick={() => move(i, 1)}
              disabled={i === entries.length - 1}
              style={{
                background: "none",
                border: "none",
                cursor: i === entries.length - 1 ? "default" : "pointer",
                color: i === entries.length - 1 ? theme.tg : theme.td,
                fontSize: 14,
                padding: "0 4px",
                opacity: i === entries.length - 1 ? 0.3 : 1,
              }}
              aria-label="Move down"
            >
              ▼
            </button>
          </div>
        );
      })}
    </div>
  );
}

function Chip({
  label,
  active,
  excluded,
  onClick,
  onRightClick,
}: {
  label: string;
  active: boolean;
  excluded?: boolean;
  onClick: () => void;
  onRightClick?: () => void;
}) {
  const theme = useTheme();
  return (
    <button
      onClick={onClick}
      onContextMenu={(e) => {
        if (onRightClick) {
          e.preventDefault();
          onRightClick();
        }
      }}
      style={{
        padding: "6px 12px",
        borderRadius: 99,
        fontSize: 12,
        fontWeight: 600,
        border: `1px solid ${
          excluded
            ? theme.err + "88"
            : active
              ? theme.accent + "88"
              : theme.border
        }`,
        background: excluded
          ? theme.err + "18"
          : active
            ? theme.accent + "18"
            : "transparent",
        color: excluded
          ? theme.err
          : active
            ? theme.accent
            : theme.textDim,
        cursor: "pointer",
        textDecoration: excluded ? "line-through" : "none",
        textTransform: "capitalize",
      }}
    >
      {label}
    </button>
  );
}

function Banner({
  tone,
  children,
}: {
  tone: "ok" | "err";
  children: React.ReactNode;
}) {
  const theme = useTheme();
  const color = tone === "ok" ? theme.ok : theme.err;
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
