// Tappable filter / toggle chip. Differs from MobileBadge in that
// it's interactive (button), has an active state, and is sized for
// touch (≥ 36px tall — looser than the 44pt rule because chips
// usually live in a horizontal scroll row where they get tapped
// alongside generous spacing on each side).
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { useTheme } from "../../theme";
import { useViewport } from "../../hooks/useViewport";
import { RADIUS, scaleFor } from "./tokens";

export interface MobileChipProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  children: ReactNode;
  active?: boolean;
  leadingIcon?: ReactNode;
}

export function MobileChip({
  children,
  active,
  leadingIcon,
  style,
  ...rest
}: MobileChipProps) {
  const t = useTheme();
  const vp = useViewport();
  const s = scaleFor(vp);

  return (
    <button
      {...rest}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: `8px ${s.space.md}px`,
        minHeight: 36,
        background: active ? t.abg : t.bg3,
        color: active ? t.accent : t.text2,
        border: `1px solid ${active ? t.abr : t.border}`,
        borderRadius: RADIUS.full,
        fontSize: s.type.caption,
        fontWeight: active ? 700 : 500,
        cursor: "pointer",
        whiteSpace: "nowrap",
        ...style,
      }}
    >
      {leadingIcon && <span style={{ display: "flex" }}>{leadingIcon}</span>}
      {children}
    </button>
  );
}
