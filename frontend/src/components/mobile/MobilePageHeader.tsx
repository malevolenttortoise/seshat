// Mobile page header — replaces the inline `[h1] [...controls]` flex
// pattern that lives at the top of every discovery and pipeline
// page. The desktop pattern crams a 24-32px title and 4-5 control
// elements into one row; on phones that wraps awkwardly even with
// the .page-header-row CSS patches.
//
// This component splits the header into three explicit zones:
//
//   1. Title row    — page title + count, single line
//   2. Search row   — full-width search input (optional)
//   3. Controls row — chips/sort/view picker, horizontally scrollable
//                     if it overflows
//
// Pages that don't need search omit it. Pages that don't have any
// controls hide the third row. The component handles that
// automatically — pass only what you need.
import type { ReactNode } from "react";
import { useTheme } from "../../theme";
import { useViewport } from "../../hooks/useViewport";
import { scaleFor } from "./tokens";

export interface MobilePageHeaderProps {
  title: ReactNode;
  count?: ReactNode;
  // Search input (typically <MobileInput> or <SearchBar>). Renders
  // full-width below the title row.
  search?: ReactNode;
  // Filter chips, sort selector, view toggle, etc. Rendered as a
  // horizontally scrollable row so a long set of controls doesn't
  // wrap onto two lines.
  controls?: ReactNode;
  // Optional right-aligned action on the title row (e.g. an "Add"
  // icon button). Kept out of the controls row because the title
  // row stays single-line and is always visible.
  rightAction?: ReactNode;
}

export function MobilePageHeader({
  title,
  count,
  search,
  controls,
  rightAction,
}: MobilePageHeaderProps) {
  const t = useTheme();
  const vp = useViewport();
  const s = scaleFor(vp);

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: s.space.sm,
        marginBottom: s.space.md,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: s.space.sm,
          minHeight: 36,
        }}
      >
        <h1
          style={{
            flex: 1,
            margin: 0,
            fontSize: s.type.title,
            fontWeight: 700,
            color: t.text,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {title}
          {count !== undefined && count !== null && (
            <span
              style={{
                fontSize: s.type.label,
                fontWeight: 500,
                color: t.td,
                marginLeft: 8,
              }}
            >
              ({count})
            </span>
          )}
        </h1>
        {rightAction}
      </div>
      {search}
      {controls && (
        <div
          style={{
            display: "flex",
            gap: s.space.sm,
            overflowX: "auto",
            paddingBottom: 2,
            // hide the scrollbar — the chip row should look like a
            // free-flowing strip, not a scrollable panel
            scrollbarWidth: "none",
          }}
        >
          {controls}
        </div>
      )}
    </div>
  );
}
