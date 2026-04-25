// Touch-sized button. Mirrors the desktop Btn variant set (primary,
// secondary, danger, ghost, accent) but with min-height ≥ 44pt and
// a 16px label so it feels right under a fingertip.
//
// Press feedback is handled globally in index.css (`button:active {
// transform: scale(0.97) }`) so we don't reimplement it here.
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { useTheme } from "../../theme";
import { useViewport } from "../../hooks/useViewport";
import { TAP, RADIUS, scaleFor } from "./tokens";

type Variant = "primary" | "secondary" | "danger" | "ghost" | "accent";

export interface MobileBtnProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  children: ReactNode;
  fullWidth?: boolean;
  // primary actions get the taller 48pt target (CTAs, save, submit);
  // everything else uses 44pt.
  primary?: boolean;
  leadingIcon?: ReactNode;
  trailingIcon?: ReactNode;
}

export function MobileBtn({
  variant = "secondary",
  children,
  fullWidth,
  primary,
  leadingIcon,
  trailingIcon,
  style,
  ...rest
}: MobileBtnProps) {
  const t = useTheme();
  const vp = useViewport();
  const s = scaleFor(vp);

  const palette: Record<Variant, { bg: string; fg: string; border: string }> = {
    primary: { bg: t.accent, fg: t.bg, border: t.accent },
    accent: { bg: t.accent, fg: t.bg, border: t.accent },
    secondary: { bg: t.bg3, fg: t.text2, border: t.border },
    danger: { bg: t.err, fg: t.bg, border: t.err },
    ghost: { bg: "transparent", fg: t.text2, border: t.border },
  };
  const c = palette[variant];
  const minHeight = primary || variant === "primary" ? TAP.primary : TAP.min;

  return (
    <button
      {...rest}
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        gap: s.space.sm,
        background: c.bg,
        color: c.fg,
        border: `1px solid ${c.border}`,
        borderRadius: RADIUS.md,
        fontSize: s.type.body,
        fontWeight: 600,
        minHeight,
        padding: `0 ${s.pad.normal}px`,
        width: fullWidth ? "100%" : undefined,
        cursor: "pointer",
        ...style,
      }}
    >
      {leadingIcon && <span style={{ display: "flex" }}>{leadingIcon}</span>}
      <span>{children}</span>
      {trailingIcon && <span style={{ display: "flex" }}>{trailingIcon}</span>}
    </button>
  );
}
