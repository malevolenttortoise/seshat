// Mobile book card. Replaces BCard/BListRow on every mobile book
// listing (Library, Missing, Upcoming, MAM Search, Hidden, Author
// Detail). Horizontal layout: cover on the left, text content on
// the right, status pill and MAM action inline at the bottom.
//
// On phones each card is full-width single-column. On iPads we let
// CSS grid put two cards per row via the parent grid (the card
// itself doesn't care about grid placement — it just fills its slot).
import { useTheme } from "../../theme";
import { useViewport } from "../../hooks/useViewport";
import { fmtDate } from "../../lib/format";
import type { Book, BookActionHandler } from "../../types";
import { RADIUS, scaleFor } from "./tokens";
import { MobileBadge } from "./MobileBadge";

export interface MobileBookCardProps {
  book: Book;
  onAction?: BookActionHandler;
  onClick?: (book: Book) => void;
  showAuthor?: boolean;
  showMamLink?: boolean;
  onSendToPipeline?: (ids: number[]) => void;
  selMode?: boolean;
  selected?: boolean;
  onToggleSel?: (id: number) => void;
}

function coverSrcFor(book: Book): string {
  const slugPath = book.library_slug
    ? `/api/discovery/covers/${book.library_slug}/${book.id}`
    : `/api/discovery/covers/${book.id}`;
  if (book.owned && (book.cover_path || book.audiobookshelf_id)) return slugPath;
  return book.cover_url || slugPath;
}

function pairedFormat(book: Book): "ebook" | "audiobook" | null {
  const sibs = book.work_siblings;
  if (!Array.isArray(sibs) || sibs.length === 0) return null;
  const myType =
    book.content_type || (book.audiobookshelf_id ? "audiobook" : "ebook");
  const other = sibs.find((s) => s.content_type && s.content_type !== myType);
  return other ? (other.content_type as "ebook" | "audiobook") : null;
}

// Collapse the four orthogonal status bits (is_new / is_unreleased /
// owned / missing) into a single pill — desktop renders them
// stacked, but on a phone one prominent state label reads cleaner.
function primaryStatus(book: Book): {
  label: string;
  tone: "ok" | "warn" | "err" | "info" | "accent";
} | null {
  if (book.is_unreleased) return { label: "Upcoming", tone: "info" };
  if (book.is_new) return { label: "New", tone: "accent" };
  if (book.owned === 1) return { label: "Owned", tone: "ok" };
  return { label: "Missing", tone: "err" };
}

export function MobileBookCard({
  book,
  onClick,
  showAuthor,
  showMamLink,
  onSendToPipeline,
  selMode,
  selected,
  onToggleSel,
}: MobileBookCardProps) {
  const t = useTheme();
  const vp = useViewport();
  const s = scaleFor(vp);

  const isUp = !!book.is_unreleased;
  const hasCover =
    !!book.cover_url || !!book.cover_path || !!book.audiobookshelf_id;
  const paired = pairedFormat(book);
  const status = primaryStatus(book);

  const handleClick = () => {
    if (selMode && onToggleSel) onToggleSel(book.id);
    else if (onClick) onClick(book);
  };

  const mamFound = book.mam_status === "found";
  const showMamButton =
    showMamLink && mamFound && onSendToPipeline && !book.mam_my_snatched;

  return (
    <div
      onClick={handleClick}
      style={{
        display: "flex",
        gap: s.space.md,
        padding: s.space.sm,
        background:
          selMode && selected ? t.abg : t.bg2,
        border: `1px solid ${
          selMode && selected ? t.accent : isUp ? t.cyant : t.border
        }`,
        borderRadius: RADIUS.lg,
        cursor: "pointer",
        opacity: isUp ? 0.85 : 1,
      }}
    >
      {/* Cover — fixed 2:3 aspect, left-aligned */}
      <div
        style={{
          width: 64,
          height: 96,
          flexShrink: 0,
          background: t.bg3,
          borderRadius: RADIUS.sm,
          overflow: "hidden",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        {hasCover ? (
          <img
            src={coverSrcFor(book)}
            alt=""
            loading="lazy"
            style={{
              width: "100%",
              height: "100%",
              objectFit: "cover",
            }}
            onError={(e) => {
              (e.currentTarget as HTMLImageElement).style.display = "none";
            }}
          />
        ) : (
          <span style={{ fontSize: 24, color: t.tg }}>?</span>
        )}
      </div>

      {/* Content column */}
      <div
        style={{
          flex: 1,
          minWidth: 0,
          display: "flex",
          flexDirection: "column",
          gap: 2,
        }}
      >
        {/* Title + paired-format icon */}
        <div
          style={{
            display: "flex",
            alignItems: "baseline",
            gap: 4,
            fontSize: s.type.label,
            fontWeight: 600,
            color: t.text,
            lineHeight: 1.3,
          }}
        >
          <span
            style={{
              flex: 1,
              minWidth: 0,
              overflow: "hidden",
              display: "-webkit-box",
              WebkitLineClamp: 2,
              WebkitBoxOrient: "vertical",
            }}
          >
            {book.title}
          </span>
          {paired && (
            <span
              title={`Also available as ${paired}`}
              style={{
                fontSize: s.type.caption,
                lineHeight: 1,
                flexShrink: 0,
              }}
            >
              {paired === "audiobook" ? "🎧" : "📖"}
            </span>
          )}
        </div>

        {/* Author — v3.0.0 Phase 7 multi-author byline (first 2 + "+N
            more"); names aren't tappable on mobile (the card tap opens the
            book). Falls back to author_name when no contributors stamped. */}
        {showAuthor && (book.contributors?.length || book.author_name) && (
          <div
            style={{
              fontSize: s.type.caption,
              color: t.td,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {book.contributors && book.contributors.length > 0
              ? (() => {
                  const c = book.contributors;
                  const shown = c.slice(0, 2).map((x) => x.name).join(", ");
                  const extra = c.length - Math.min(2, c.length);
                  return extra > 0 ? `${shown} +${extra} more` : shown;
                })()
              : book.author_name}
          </div>
        )}

        {/* Series fragment */}
        {book.series_name && (
          <div
            style={{
              fontSize: s.type.micro,
              color: t.purt,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {book.series_name}
            {book.series_index ? ` #${book.series_index}` : ""}
            {book.mainline_total ? ` of ${book.mainline_total}` : ""}
          </div>
        )}

        {/* Expected date for upcoming */}
        {isUp && book.expected_date && (
          <div style={{ fontSize: s.type.micro, color: t.cyant }}>
            Expected: {fmtDate(book.expected_date)}
          </div>
        )}

        {/* Status pill + MAM action row */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: s.space.xs,
            marginTop: 4,
            flexWrap: "wrap",
          }}
        >
          {status && (
            <MobileBadge tone={status.tone}>{status.label}</MobileBadge>
          )}
          {showMamLink && book.mam_url && (
            <a
              href={book.mam_url}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              style={{
                fontSize: s.type.micro,
                fontWeight: 600,
                color:
                  book.mam_status === "found"
                    ? t.grnt
                    : book.mam_status === "not_found"
                      ? t.redt
                      : t.ylwt,
                textDecoration: "none",
                padding: "3px 8px",
                borderRadius: RADIUS.sm,
                background:
                  book.mam_status === "found"
                    ? t.grnb
                    : book.mam_status === "not_found"
                      ? t.redb
                      : t.ylwb,
              }}
            >
              MAM ↗
            </a>
          )}
          {showMamButton && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                onSendToPipeline?.([book.id]);
              }}
              style={{
                fontSize: s.type.micro,
                fontWeight: 600,
                color: t.purt,
                background: t.purb,
                border: `1px solid ${t.purt}`,
                borderRadius: RADIUS.sm,
                padding: "4px 8px",
                cursor: "pointer",
                minHeight: 32,
              }}
            >
              ⬇ Send
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
