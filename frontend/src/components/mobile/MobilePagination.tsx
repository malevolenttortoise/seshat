// Mobile pagination — bottom-only, big prev/next buttons, page
// indicator centered between them. The desktop discovery pages
// render a row of numbered buttons; on phones that's both too narrow
// to fit and too small to tap.
//
// Behavior:
//   - prev/next disable at the ends
//   - tapping the page indicator (when total > 1) opens a sheet to
//     jump to a specific page (caller wires this; we just expose an
//     onJump callback)
//   - if onJump is omitted, the indicator is non-interactive and
//     just shows current/total
import { useTheme } from "../../theme";
import { useViewport } from "../../hooks/useViewport";
import { scaleFor } from "./tokens";
import { MobileBtn } from "./MobileBtn";

export interface MobilePaginationProps {
  page: number; // 1-based
  totalPages: number;
  onPrev: () => void;
  onNext: () => void;
  onJump?: () => void;
}

export function MobilePagination({
  page,
  totalPages,
  onPrev,
  onNext,
  onJump,
}: MobilePaginationProps) {
  const t = useTheme();
  const vp = useViewport();
  const s = scaleFor(vp);

  if (totalPages <= 1) return null;

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: s.space.md,
        padding: `${s.space.lg}px 0`,
      }}
    >
      <MobileBtn
        variant="secondary"
        onClick={onPrev}
        disabled={page <= 1}
        style={{ flex: 1 }}
      >
        ‹ Prev
      </MobileBtn>
      <button
        onClick={onJump}
        disabled={!onJump}
        style={{
          minWidth: 80,
          textAlign: "center",
          padding: `8px ${s.space.md}px`,
          background: "transparent",
          border: "none",
          color: t.td,
          fontSize: s.type.caption,
          fontWeight: 600,
          cursor: onJump ? "pointer" : "default",
        }}
      >
        {page} / {totalPages}
      </button>
      <MobileBtn
        variant="secondary"
        onClick={onNext}
        disabled={page >= totalPages}
        style={{ flex: 1 }}
      >
        Next ›
      </MobileBtn>
    </div>
  );
}
