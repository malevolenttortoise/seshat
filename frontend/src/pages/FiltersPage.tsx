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
import { useEffect, useState, useMemo } from "react";
import { Btn } from "../components/Btn";
import { Section } from "../components/Section";
import { Spin } from "../components/Spin";
import { api } from "../api";
import { useTheme } from "../theme";

interface CategoryEntry {
  id: string;
  name: string;
  main_id: string;
  main_name: string;
  normalized: string;
}

interface EnumsResponse {
  categories: CategoryEntry[];
  languages: string[];
  formats: string[];
}

type SettingsMap = Record<string, unknown>;

interface PatchResponse {
  ok: boolean;
  updated: string[];
  rejected: string[];
}

export default function FiltersPage() {
  const theme = useTheme();
  const [enums, setEnums] = useState<EnumsResponse | null>(null);
  const [settings, setSettings] = useState<SettingsMap | null>(null);
  const [draft, setDraft] = useState<SettingsMap>({});
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    Promise.all([
      api.get<EnumsResponse>("/v1/enums"),
      api.get<SettingsMap>("/v1/settings"),
    ])
      .then(([e, s]) => {
        setEnums(e);
        setSettings(s);
      })
      .catch((e) => setError(String(e)));
  }, []);

  const effective: SettingsMap = { ...(settings ?? {}), ...draft };

  function setField(key: string, value: unknown) {
    setDraft((d) => {
      const next = { ...d, [key]: value };
      if (
        settings &&
        JSON.stringify(settings[key]) === JSON.stringify(value)
      ) {
        delete next[key];
      }
      return next;
    });
    setOk(null);
  }

  async function save() {
    if (Object.keys(draft).length === 0) return;
    setSaving(true);
    setError(null);
    setOk(null);
    try {
      const r = await api.patch<PatchResponse>("/v1/settings", draft);
      if (r.rejected.length > 0) {
        setError(`Rejected: ${r.rejected.join(", ")}`);
      } else {
        setOk(`Updated ${r.updated.length} filter(s).`);
      }
      // Reload settings so the draft resets cleanly.
      const fresh = await api.get<SettingsMap>("/v1/settings");
      setSettings(fresh);
      setDraft({});
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }

  // Group categories by their main_name (AudioBooks, E-Books, etc.)
  // Hook must be called unconditionally (Rules of Hooks) — before
  // any early return. Safe because enums?.categories is just [].
  const catGroups = useMemo(() => {
    const cats = enums?.categories ?? [];
    const groups: Record<string, CategoryEntry[]> = {};
    for (const c of cats) {
      (groups[c.main_name] ??= []).push(c);
    }
    return groups;
  }, [enums?.categories]);

  if (!enums || !settings) {
    return (
      <div style={{ display: "flex", justifyContent: "center", padding: 40 }}>
        <Spin />
      </div>
    );
  }

  const allowedCats = new Set(
    (effective.allowed_categories as string[]) ?? [],
  );
  const excludedCats = new Set(
    (effective.excluded_categories as string[]) ?? [],
  );
  const allowedLangs = new Set(
    (effective.allowed_languages as string[]) ?? [],
  );
  const allowedFormats = new Set(
    (effective.allowed_formats as string[]) ?? [],
  );
  const excludedFormats = new Set(
    (effective.excluded_formats as string[]) ?? [],
  );

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
        title="Allowed categories"
        subtitle={`${allowedCats.size} selected. Empty = accept all that pass the format gate.`}
      >
        {Object.entries(catGroups).map(([mainName, cats]) => (
          <div key={mainName} style={{ marginBottom: 16 }}>
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
                  active={allowedCats.has(c.normalized)}
                  excluded={excludedCats.has(c.normalized)}
                  onClick={() =>
                    toggleInSet(
                      "allowed_categories",
                      allowedCats,
                      c.normalized,
                    )
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
        ))}
        <p style={{ fontSize: 11, color: theme.textDim, marginTop: 8 }}>
          Click to toggle allowed. Right-click to toggle excluded (red
          strike-through = excluded even if parent format is allowed).
        </p>
      </Section>

      {/* ── Formats ────────────────────────────────────────── */}
      <Section
        title="Formats"
        subtitle="Top-level format gate. Empty allowed = accept all."
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

      {/* ── Languages ──────────────────────────────────────── */}
      <Section
        title="Allowed languages"
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
          onClick={() => setDraft({})}
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
