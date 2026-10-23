// Settings → Data Management: the pipeline tables' counts and clears.
// Split out of SettingsPage (wave 5b S16, issue 23).
import { useEffect, useState } from "react";
import { api } from "../../api";
import { useTheme } from "../../theme";
import { Btn } from "../Btn";
import { SF } from "./fields";

export function DataSection() {
  const t = useTheme();
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { api.get<Record<string, number>>("/v1/data/counts").then(setCounts).catch(() => {}); }, []);
  async function clear(target: string, dangerous = false) {
    const label = target.replace(/_/g, " ");
    if (dangerous) { const typed = prompt(`This will permanently delete all ${label}.\nType "${target}" to confirm:`); if (typed !== target) return; }
    else if (!confirm(`Clear all ${label}?`)) return;
    setBusy(true);
    try { const r = await api.post<{ rows_deleted: number }>(`/v1/data/clear/${target}`, dangerous ? { confirm: target } : {}); setMsg(`Cleared ${r.rows_deleted} rows`); const fresh = await api.get<Record<string, number>>("/v1/data/counts"); setCounts(fresh); }
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
      <DataRow target="tentative_torrents" label="Tentative torrents" desc="Captures from unknown authors." count={counts.tentative_torrents ?? 0} />
      <DataRow target="book_review_queue" label="Pending reviews" desc="Downloaded books awaiting approval." count={counts.book_review_queue ?? 0} />
      <DataRow target="ignored_torrents_seen" label="Ignored history" desc="Weekly ignored-author audit trail." count={counts.ignored_torrents_seen ?? 0} />
      <DataRow target="announces" label="Announce log" desc="IRC announce audit trail." count={counts.announces ?? 0} />
      <DataRow target="calibre_additions" label="Calibre additions" desc="Digest reporting counter." count={counts.calibre_additions ?? 0} />
      <DataRow target="authors_allowed" label="Allowed authors" desc="⚠ Clearing removes ALL." count={counts.authors_allowed ?? 0} dangerous />
      <DataRow target="authors_ignored" label="Ignored authors" desc="⚠ They'll reappear as 'new'." count={counts.authors_ignored ?? 0} dangerous />
    </>
  );
}
