// Tappable full-width row. Replaces the desktop "text link" pattern
// — on touch you don't tap a 60px-wide span of underlined text, you
// tap the whole row. Used for nav lists, action menus, settings
// items, "see all" entry points on the dashboard.
//
// Slots: leadingIcon (left), title + subtitle (center), trailing
// (right — chevron, badge, count, or another icon button). The whole
// row is one tap target.
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { useTheme } from "../../theme";
import { useViewport } from "../../hooks/useViewport";
import { TAP, RADIUS, scaleFor } from "./tokens";

export interface MobileRowProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "title"> {
  title: ReactNode;
  subtitle?: ReactNode;
  leadingIcon?: ReactNode;
  trailing?: ReactNode;
  // When destructive, the title and any inherited foreground go red.
  destructive?: boolean;
  // When active, applies the accent-tinted background + accent text.
  active?: boolean;
  // Hide the right chevron — useful when the row is informational
  // (a tap toggles state in place) rather than navigational.
  hideChevron?: boolean;
}

export function MobileRow({
  title,
  subtitle,
  leadingIcon,
  trailing,
  destructive,
  active,
  hideChevron,
  style,
  ...rest
}: MobileRowProps) {
  const t = useTheme();
  const vp = useViewport();
  const s = scaleFor(vp);

  const fg = destructive ? t.err : active ? t.accent : t.text;
  const bg = active ? t.abg : "transparent";

  return (
    <button
      {...rest}
      style={{
        display: "flex",
        alignItems: "center",
        gap: s.space.md,
        width: "100%",
        minHeight: TAP.min,
        padding: `${s.space.md}px ${s.pad.normal}px`,
        background: bg,
        color: fg,
        border: "none",
        borderRadius: RADIUS.md,
        textAlign: "left",
        cursor: "pointer",
        ...style,
      }}
    >
      {leadingIcon && (
        <span
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            width: 24,
            color: active ? t.accent : t.td,
            flexShrink: 0,
          }}
        >
          {leadingIcon}
        </span>
      )}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: s.type.body,
            fontWeight: active ? 700 : 500,
            color: fg,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {title}
        </div>
        {subtitle && (
          <div
            style={{
              fontSize: s.type.caption,
              color: t.td,
              marginTop: 2,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {subtitle}
          </div>
        )}
      </div>
      {trailing}
      {!hideChevron && !trailing && (
        <span
          style={{ color: t.tg, fontSize: s.type.body, flexShrink: 0 }}
          aria-hidden
        >
          ›
        </span>
      )}
    </button>
  );
}
