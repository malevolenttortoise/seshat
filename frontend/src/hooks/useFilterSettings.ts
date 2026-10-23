// The Filters page's data: MAM's category / language / format lists, the
// saved settings, a sparse draft of the user's changes, and saving it.
//
// FiltersPage + MobileFiltersPage each carried this verbatim (wave 5b
// S8). The shells keep how a chip is clicked (desktop: click to allow,
// right-click to exclude; phone: one tap cycles off → allow → exclude).
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";

export interface CategoryEntry {
  id: string;
  name: string;
  main_id: string;
  main_name: string;
  normalized: string;
}

export interface EnumsResponse {
  categories: CategoryEntry[];
  languages: string[];
  formats: string[];
}

export type SettingsMap = Record<string, unknown>;

interface PatchResponse {
  ok: boolean;
  updated: string[];
  rejected: string[];
}

export type FmtEntry = { fmt: string; enabled: boolean };

export function useFilterSettings() {
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

  /** The saved settings with the draft on top. */
  const effective: SettingsMap = { ...(settings ?? {}), ...draft };

  /** Change one key in the draft; a value equal to the saved one drops out. */
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

  /** PATCH the draft, then reload the settings so the draft resets cleanly. */
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
      const fresh = await api.get<SettingsMap>("/v1/settings");
      setSettings(fresh);
      setDraft({});
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }

  /** Drop every unsaved change. */
  const discard = () => setDraft({});

  // Categories grouped by their main_name (AudioBooks, E-Books, etc.).
  const catGroups = useMemo(() => {
    const cats = enums?.categories ?? [];
    const groups: Record<string, CategoryEntry[]> = {};
    for (const c of cats) (groups[c.main_name] ??= []).push(c);
    return groups;
  }, [enums?.categories]);

  const set = (key: string) => new Set((effective[key] as string[]) ?? []);
  const allowedFormats = set("allowed_formats");

  return {
    enums, settings, draft, error, ok, saving, effective, setField, save, discard, catGroups,
    allowedCats: set("allowed_categories"),
    allowedAudiobookCats: set("allowed_audiobook_categories"),
    excludedCats: set("excluded_categories"),
    allowedLangs: set("allowed_languages"),
    allowedFormats,
    excludedFormats: set("excluded_formats"),
    // v2.9.0: audiobook acceptance is derived from the Media Type filter.
    // Empty allowed_formats means "accept all" — including audiobooks.
    // Otherwise the user must have ticked the audiobooks chip explicitly.
    // Mirrors `_build_filter_config` in app/main.py.
    acceptAudiobooks: allowedFormats.size === 0 || allowedFormats.has("audiobooks"),
    // Format Priority — per-media-type list of {fmt, enabled} entries.
    // Drives the v2.9.0 format-priority dedup gate. Top = highest.
    formatPriority: (effective.format_priority as Record<string, FmtEntry[]>) ?? {},
  };
}
