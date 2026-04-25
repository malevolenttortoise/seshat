// Tappable stat tile — icon, value, label. Used in stat grids on
// every dashboard. Tap navigates somewhere relevant (e.g. tapping
// "Owned" jumps to the library page).
import type { ReactNode } from "react";
import { useTheme } from "../../../theme";
import { useViewport } from "../../../hooks/useViewport";
import { TAP, RADIUS, scaleFor } from "../tokens";

export interface MobileStatTileProps {
  label: string;
  value: ReactNode;
  // Color of the value text. Defaults to theme.text. Pass a tone like
  // theme.jade / theme.red / theme.accent to make a tile pop.
  color?: string;
  // Subtitle / unit shown below the value (e.g. "12.3k hrs", "3 new").
  sub?: ReactNode;
  // Icon displayed in the top-left. Emoji works fine; any ReactNode.
  icon?: ReactNode;
  onClick?: () => void;
  // Highlight = thicker accent border and tinted background. Use for
  // tiles whose count > 0 (e.g. "Books to Review: 3"). Visually
  // pulls the eye to actionable items.
  highlight?: boolean;
}

export function MobileStatTile({
  label,
  value,
  color,
  sub,
  icon,
  onClick,
  highlight,
}: MobileStatTileProps) {
  const t = useTheme();
  const vp = useViewport();
  const s = scaleFor(vp);

  const valueColor = color || t.text;
  const bg = highlight ? t.abg : t.bg2;
  const border = highlight ? t.abr : t.border;

  return (
    <button
      onClick={onClick}
      disabled={!onClick}
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "flex-start",
        gap: 2,
        padding: s.pad.tight,
        minHeight: TAP.min + 24,
        background: bg,
        border: `1px solid ${border}`,
        borderRadius: RADIUS.md,
        cursor: onClick ? "pointer" : "default",
        textAlign: "left",
        width: "100%",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          fontSize: s.type.caption,
          color: t.td,
          fontWeight: 500,
          textTransform: "uppercase",
          letterSpacing: "0.04em",
        }}
      >
        {icon && <span style={{ fontSize: s.type.label }}>{icon}</span>}
        <span>{label}</span>
      </div>
      <div
        style={{
          fontSize: s.type.title,
          fontWeight: 700,
          color: valueColor,
          lineHeight: 1.1,
        }}
      >
        {value}
      </div>
      {sub && (
        <div
          style={{
            fontSize: s.type.micro,
            color: t.tg,
            fontWeight: 500,
          }}
        >
          {sub}
        </div>
      )}
    </button>
  );
}
