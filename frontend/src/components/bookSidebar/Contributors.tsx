// The book sidebar's byline: the full contributor set (position order),
// each a chip with an ×. With more than one author the removal is
// immediate (after a confirm); with exactly one it opens the replacement
// modal, since the backend won't leave a book authorless.
//
// The sidebar calls the hook and places the row (in the metadata list)
// and the modal (at the end of the panel) where they've always been
// (audit G158).
import { useEffect, useState } from "react";
import { api, slugQuery } from "../../api";
import { useTheme } from "../../theme";
import { toast } from "../../lib/toast";
import type { Book, Contributor } from "../../types";
import { SBRow } from "../SBRow";
import { ReplaceAuthorModal } from "../ReplaceAuthorModal";

export function useContributors(book: Book, onEdit?: () => void | Promise<void>) {
  // A local copy of the byline so removals reflect instantly without
  // waiting on the parent list refresh. `removingId` drives the
  // per-chip spinner; `replaceTarget` opens the last-author
  // replacement modal.
  const [contribs, setContribs] = useState<Contributor[]>(
    book.contributors ?? [],
  );
  const [removingId, setRemovingId] = useState<number | null>(null);
  const [replaceTarget, setReplaceTarget] = useState<Contributor | null>(null);
  // Re-seed when a different book is selected into the same sidebar
  // instance (the parent reuses one BookSidebar and swaps `book`).
  useEffect(() => {
    setContribs(book.contributors ?? []);
    setReplaceTarget(null);
    setRemovingId(null);
  }, [book.id, book.library_slug, book.contributors]);

  // DELETE one contributor. With >1 author it removes immediately; with
  // exactly 1 it opens the replacement modal (the backend refuses to
  // leave a book authorless). `replacementId` is supplied only from the
  // modal path.
  const remove = async (c: Contributor, replacementId?: number) => {
    if (contribs.length <= 1 && replacementId === undefined) {
      setReplaceTarget(c);
      return;
    }
    setRemovingId(c.author_id);
    try {
      const slugQs = slugQuery(book.library_slug);
      const repQs =
        replacementId !== undefined
          ? `${slugQs ? "&" : "?"}replacement_author_id=${replacementId}`
          : "";
      const res = await api.del<{
        contributors: Contributor[];
        removed_author_orphaned: boolean;
      }>(`/discovery/books/${book.id}/contributors/${c.author_id}${slugQs}${repQs}`);
      setContribs(res.contributors);
      setReplaceTarget(null);
      toast.success(
        res.removed_author_orphaned
          ? `Removed ${c.name} — now-empty author will be cleaned up automatically`
          : `Removed ${c.name} from this book`,
      );
      // Refresh the parent list/counts (byline, author totals) in the
      // background — the local `contribs` already updated the sidebar.
      if (onEdit) {
        Promise.resolve(onEdit()).catch(() => {
          /* background refresh — surfaces on parent list */
        });
      }
    } catch (e) {
      toast.error(
        `Remove failed: ${e instanceof Error ? e.message : String(e)}`,
      );
      throw e; // let the modal surface it too
    } finally {
      setRemovingId(null);
    }
  };

  return { contribs, removingId, replaceTarget, setReplaceTarget, remove };
}

export type ContributorsState = ReturnType<typeof useContributors>;

export function ContributorsRow({ c, book }: { c: ContributorsState; book: Book }) {
  const t = useTheme();
  const { contribs, removingId } = c;
  return (
    <SBRow
      label={contribs.length > 1 ? "Authors" : "Author"}
      value={
        contribs.length ? (
          <span
            style={{
              display: "inline-flex",
              flexWrap: "wrap",
              gap: 6,
              justifyContent: "flex-end",
            }}
          >
            {contribs.map((a) => (
              <span
                key={`${a.author_id}-${a.position}`}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 4,
                  padding: "1px 4px 1px 8px",
                  background: t.bg4,
                  border: `1px solid ${t.border}`,
                  borderRadius: 11,
                  fontSize: 12,
                  lineHeight: 1.5,
                }}
              >
                {a.name}
                {a.role ? <span style={{ color: t.td }}>({a.role})</span> : null}
                <button
                  title={`Remove ${a.name} from this book`}
                  aria-label={`Remove ${a.name}`}
                  disabled={removingId !== null}
                  onClick={() => {
                    if (
                      contribs.length > 1 &&
                      !window.confirm(
                        `Remove ${a.name} from "${book.title}"?`,
                      )
                    )
                      return;
                    c.remove(a).catch(() => {
                      /* toast already surfaced in handler */
                    });
                  }}
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    justifyContent: "center",
                    width: 15,
                    height: 15,
                    padding: 0,
                    borderRadius: "50%",
                    border: "none",
                    background: "none",
                    color: t.red,
                    cursor: removingId !== null ? "default" : "pointer",
                    fontSize: 14,
                    opacity: removingId === a.author_id ? 0.4 : 0.8,
                  }}
                >
                  ×
                </button>
              </span>
            ))}
          </span>
        ) : (
          book.author_name
        )
      }
    />
  );
}

export function ReplaceSoleAuthor({ c, book }: { c: ContributorsState; book: Book }) {
  const target = c.replaceTarget;
  if (!target) return null;
  return (
    <ReplaceAuthorModal
      bookTitle={book.title}
      slug={book.library_slug}
      removingName={target.name}
      removingAuthorId={target.author_id}
      onCancel={() => c.setReplaceTarget(null)}
      onConfirm={(replacementId) => c.remove(target, replacementId)}
    />
  );
}
