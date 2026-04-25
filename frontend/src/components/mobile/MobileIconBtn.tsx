// Square 44x44 (or larger) icon-only button. Same visual language as
// MobileBtn but without a label — for hamburger toggles, close
// buttons, inline action icons inside lists, etc.
//
// The hit area is always ≥ TAP.icon even when the icon glyph itself
// is small, so the user gets a forgiving tap target.
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { useTheme } from "../../theme";
import { TAP, RADIUS } from "./tokens";

type Tone = "neutral" | "accent" | "danger";

export interface MobileIconBtnProps
  extends ButtonHTMLAttributes<HTMLButtonElement> {
  children: ReactNode;
  tone?: Tone;
  // Filled gives a tinted background (accent/danger backdrops). Plain
  // is transparent and is the default — most icon buttons sit inside
  // a styled row already and a filled chip would compete.
  filled?: boolean;
  size?: number;
  label?: string; // a11y — sets aria-label
}

export function MobileIconBtn({
  children,
  tone = "neutral",
  filled,
  size = TAP.icon,
  label,
  style,
  ...rest
}: MobileIconBtnProps) {
  const t = useTheme();
  const fg = tone === "accent" ? t.accent : tone === "danger" ? t.err : t.text2;
  const bg = filled
    ? tone === "accent"
      ? t.abg
      : tone === "danger"
        ? t.redb
        : t.bg3
    : "transparent";
  const border = filled
    ? tone === "accent"
      ? t.abr
      : tone === "danger"
        ? t.redt
        : t.border
    : "transparent";
  return (
    <button
      {...rest}
      aria-label={label ?? rest["aria-label"]}
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        width: size,
        height: size,
        minWidth: size,
        minHeight: size,
        background: bg,
        color: fg,
        border: `1px solid ${border}`,
        borderRadius: RADIUS.md,
        cursor: "pointer",
        ...style,
      }}
    >
      {children}
    </button>
  );
}
