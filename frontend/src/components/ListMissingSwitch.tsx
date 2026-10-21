// "List missing books" (F18), on both author pages; the state and the
// request live in useListMissing.
import { useTheme } from "../theme";
import { useListMissing } from "../hooks/useListMissing";

export function ListMissingSwitch({
  authorId, slug, authorName, onChanged, compact,
}: {
  authorId: number | null | undefined;
  slug: string | null | undefined;
  authorName: string;
  onChanged?: () => void;
  /** Phone: the description goes on its own line under the switch;
   * desktop keeps it beside the switch. */
  compact?: boolean;
}) {
  const t = useTheme();
  const { st, err, busy, toggle } = useListMissing({ authorId, slug, authorName, onChanged });
  if (!authorId) return null;
  if (err) {
    return (
      <span style={{ fontSize: 11, color: t.redt }} title={err}>
        List missing books: couldn't load
      </span>
    );
  }
  if (!st) return null;
  const desc =
    `Off: Seshat stops scanning ${authorName} for books you don't own and hides the ` +
    `${st.missing.toLocaleString()} already listed. MAM announces for ${authorName} still pass your filter.`;
  return (
    <div
      style={{
        display: "flex",
        flexDirection: compact ? "column" : "row",
        alignItems: compact ? "flex-start" : "baseline",
        gap: compact ? 4 : 8,
        flexWrap: "wrap",
      }}
    >
      <label
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          fontSize: 12,
          fontWeight: 600,
          color: st.on ? t.text2 : t.tg,
          cursor: busy ? "wait" : "pointer",
          whiteSpace: "nowrap",
        }}
      >
        <input type="checkbox" checked={st.on} disabled={busy} onChange={toggle} />
        List missing books
      </label>
      <span style={{ fontSize: 11, color: t.tg }}>{desc}</span>
    </div>
  );
}
