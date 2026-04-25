// Bottom sheet wrapper. Slides up from the bottom edge, dims the
// background with a scrim, traps Escape to close.
//
// Used for action sheets (replaces dropdowns/menus on mobile —
// "Sort by" picker, "Filter" panel, etc.) and for full-screen modal
// content (AddBookModal, ExportModal, etc. when ported in Phase 5).
//
// Tap outside / Escape closes. The sheet itself has a grab handle
// at the top and rounded top corners. On iPad the sheet is centered
// and capped at 600px wide instead of edge-to-edge.
import { useEffect, type ReactNode } from "react";
import { useTheme } from "../../theme";
import { useViewport } from "../../hooks/useViewport";
import { RADIUS, scaleFor } from "./tokens";
import { MobileIconBtn } from "./MobileIconBtn";

export interface MobileSheetProps {
  open: boolean;
  onClose: () => void;
  // Title is rendered in the sticky header bar. Pass null to render
  // a sheet without a header (rare; useful for image lightboxes).
  title?: ReactNode;
  // Content height: "auto" hugs content (good for short menus),
  // "tall" caps at 90vh and lets content scroll (good for forms),
  // "full" takes the whole viewport (good for nested-page views).
  height?: "auto" | "tall" | "full";
  // Sticky bottom action bar (Cancel/Save row, etc.).
  footer?: ReactNode;
  children: ReactNode;
}

export function MobileSheet({
  open,
  onClose,
  title,
  height = "tall",
  footer,
  children,
}: MobileSheetProps) {
  const t = useTheme();
  const vp = useViewport();
  const s = scaleFor(vp);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [open, onClose]);

  if (!open) return null;

  const sheetMaxHeight =
    height === "auto" ? "85vh" : height === "tall" ? "90vh" : "100vh";
  const sheetHeight = height === "full" ? "100vh" : undefined;

  // On iPad, center the sheet and cap width so it doesn't go
  // edge-to-edge on a wide tablet. On phones, full width.
  const isTablet = vp.isTablet;
  const sheetWidth = isTablet ? "min(600px, 92vw)" : "100%";
  const radius = height === "full"
    ? 0
    : `${RADIUS.xl}px ${RADIUS.xl}px 0 0`;

  return (
    <>
      <div
        onClick={onClose}
        style={{
          position: "fixed",
          inset: 0,
          background: "rgba(0,0,0,0.5)",
          zIndex: 200,
          animation: "fade-in 0.18s ease-out",
        }}
      />
      <div
        role="dialog"
        aria-modal="true"
        style={{
          position: "fixed",
          left: isTablet ? "50%" : 0,
          right: isTablet ? "auto" : 0,
          bottom: 0,
          transform: isTablet ? "translateX(-50%)" : undefined,
          width: sheetWidth,
          maxHeight: sheetMaxHeight,
          height: sheetHeight,
          background: t.bg2,
          borderTop: `1px solid ${t.border}`,
          borderRadius: radius,
          zIndex: 201,
          display: "flex",
          flexDirection: "column",
          animation: "slide-up 0.22s ease-out",
          paddingBottom: "env(safe-area-inset-bottom, 0px)",
        }}
      >
        {/* grab handle */}
        {height !== "full" && (
          <div
            style={{
              padding: "8px 0 4px",
              display: "flex",
              justifyContent: "center",
              flexShrink: 0,
            }}
          >
            <span
              style={{
                width: 40,
                height: 4,
                borderRadius: 2,
                background: t.borderH,
              }}
            />
          </div>
        )}
        {title !== undefined && title !== null && (
          <header
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: `${s.space.md}px ${s.pad.normal}px`,
              borderBottom: `1px solid ${t.borderL}`,
              flexShrink: 0,
            }}
          >
            <h2
              style={{
                fontSize: s.type.heading,
                fontWeight: 700,
                color: t.text,
                margin: 0,
              }}
            >
              {title}
            </h2>
            <MobileIconBtn onClick={onClose} label="Close">
              <span style={{ fontSize: 22 }}>×</span>
            </MobileIconBtn>
          </header>
        )}
        <div
          style={{
            flex: 1,
            overflowY: "auto",
            padding: s.pad.normal,
          }}
        >
          {children}
        </div>
        {footer && (
          <div
            style={{
              borderTop: `1px solid ${t.borderL}`,
              padding: s.pad.normal,
              display: "flex",
              gap: s.space.sm,
              flexShrink: 0,
              background: t.bg2,
            }}
          >
            {footer}
          </div>
        )}
      </div>
    </>
  );
}
