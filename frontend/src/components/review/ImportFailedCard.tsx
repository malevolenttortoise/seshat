// A review whose CWA drop never showed up in Calibre (2026-10 audit wave
// 5a, G121 / G133). The review page lists these first. Re-drop delivers
// the kept files again (removing the earlier drop if CWA never took it);
// "Mark as imported" closes the review as delivered without dropping
// anything, for a book that reached the library some other way.
import { Btn } from "../Btn";
import { MobileBtn } from "../mobile";
import { Spin } from "../Spin";
import { useTheme } from "../../theme";

export interface ImportFailedItem {
  id: number;
  book_filename: string;
  decision_note?: string | null;
  metadata: Record<string, unknown> & { title?: string; author?: string };
}

export function ImportFailedCard({
  item,
  busy,
  mobile,
  onRedrop,
  onMarkImported,
}: {
  item: ImportFailedItem;
  busy: boolean;
  mobile?: boolean;
  onRedrop: () => void;
  onMarkImported: () => void;
}) {
  const t = useTheme();
  const title = item.metadata.title || item.book_filename;
  const author = item.metadata.author || "";
  const markImported = () => {
    if (confirm(`Mark "${title}" as imported? Nothing is dropped into CWA.`)) onMarkImported();
  };

  return (
    <article
      style={{
        background: t.redb,
        border: `1px solid ${t.redt}`,
        borderRadius: 12,
        padding: mobile ? 12 : 16,
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      <div style={{ fontSize: 11, fontWeight: 700, color: t.red, letterSpacing: 0.4, textTransform: "uppercase" }}>
        Import failed
      </div>
      <div>
        <div style={{ fontSize: mobile ? 15 : 17, fontWeight: 700, color: t.text, wordBreak: "break-word" }}>
          {title}
        </div>
        {author && <div style={{ fontSize: 13, color: t.text2, marginTop: 2 }}>{author}</div>}
      </div>
      <div style={{ fontSize: 13, color: t.text2 }}>
        {item.decision_note || "CWA never imported this book."}
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        {mobile ? (
          <>
            <MobileBtn variant="primary" primary onClick={onRedrop} disabled={busy}>
              Re-drop
            </MobileBtn>
            <MobileBtn variant="secondary" onClick={markImported} disabled={busy}>
              Mark as imported
            </MobileBtn>
          </>
        ) : (
          <>
            <Btn variant="primary" size="sm" onClick={onRedrop} disabled={busy}>
              Re-drop
            </Btn>
            <Btn variant="secondary" size="sm" onClick={markImported} disabled={busy}>
              Mark as imported
            </Btn>
          </>
        )}
        {busy && <Spin size={16} />}
      </div>
    </article>
  );
}
