// The Settings draft, for SettingsPage and MobileSettingsPage (wave 5b
// S16, issue 23 / G140), which carried it line for line: the settings
// blob loaded once and edited in place (`upd`), saved whole (PATCH, then
// re-read so the page shows what the server kept), and the credential
// list the credential fields re-read after a change.
//
// `msg` is the page's status line: "Saved!" (cleared after 3s), "Error
// saving", or the load failure.
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

export type SettingsDraft = Record<string, unknown>;
export type SettingsUpd = (k: string, v: unknown) => void;

export interface CredItem {
  key: string;
  label: string;
  configured: boolean;
}

export function useSettingsDraft() {
  const [s, setS] = useState<SettingsDraft | null>(null);
  const [creds, setCreds] = useState<CredItem[]>([]);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    api.get<SettingsDraft>("/v1/settings").then(setS).catch((e) => setMsg(`Error: ${e}`));
  }, []);

  const loadCreds = useCallback(() => {
    api.get<{ items: CredItem[] }>("/v1/credentials").then((r) => setCreds(r.items)).catch(() => {});
  }, []);
  useEffect(() => { loadCreds(); }, [loadCreds]);

  const upd: SettingsUpd = useCallback((k, v) => setS((o) => (o ? { ...o, [k]: v } : o)), []);

  const save = async () => {
    setSaving(true);
    setMsg("");
    try {
      await api.patch("/v1/settings", s);
      setMsg("Saved!");
      const fresh = await api.get<SettingsDraft>("/v1/settings");
      setS(fresh);
      setTimeout(() => setMsg(""), 3000);
    } catch {
      setMsg("Error saving");
    } finally {
      setSaving(false);
    }
  };

  return { s, creds, loadCreds, upd, save, saving, msg };
}
