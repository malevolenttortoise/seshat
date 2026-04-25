// Mobile equivalent of the desktop Section component. The desktop
// version uses 16px padding + 12px radius and a small ▾/▸ caret as
// the collapse affordance. On mobile the whole header is the tap
// target (44pt min), the caret is bigger, and the section itself
// takes less internal padding so cards waste less width.
import { useState, type ReactNode } from "react";
import { useTheme } from "../../theme";
import { useViewport } from "../../hooks/useViewport";
import { TAP, RADIUS, scaleFor } from "./tokens";

export interface MobileSectionProps {
  title: ReactNode;
  count?: ReactNode;
  subtitle?: ReactNode;
  // Trailing slot in the header — typically an action chip or icon
  // button. Click events are stopped from bubbling so tapping it
  // doesn't toggle the collapse.
  right?: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
  // Pass false for sections that should never collapse — used when
  // the section is the only thing on a page and a collapse toggle
  // would just be confusing.
  collapsible?: boolean;
}

export function MobileSection({
  title,
  count,
  subtitle,
  right,
  children,
  defaultOpen = true,
  collapsible = true,
}: MobileSectionProps) {
  const t = useTheme();
  const vp = useViewport();
  const s = scaleFor(vp);
  const [open, setOpen] = useState(defaultOpen);

  const expanded = !collapsible || open;

  return (
    <section
      style={{
        background: t.bg2,
        border: `1px solid ${t.borderL}`,
        borderRadius: RADIUS.lg,
        padding: s.pad.tight,
        marginBottom: s.space.md,
      }}
    >
      <header
        onClick={collapsible ? () => setOpen((v) => !v) : undefined}
        style={{
          display: "flex",
          alignItems: "center",
          gap: s.space.sm,
          minHeight: TAP.min - 8, // header is itself the tap target
          marginBottom: expanded ? s.space.md : 0,
          cursor: collapsible ? "pointer" : "default",
          userSelect: "none",
        }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              fontSize: s.type.heading,
              fontWeight: 700,
              color: t.text,
            }}
          >
            <span
              style={{
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {title}
            </span>
            {count !== undefined && count !== null && (
              <span
                style={{
                  fontSize: s.type.caption,
                  fontWeight: 500,
                  color: t.td,
                }}
              >
                ({count})
              </span>
            )}
          </div>
          {subtitle && (
            <div
              style={{
                fontSize: s.type.caption,
                color: t.td,
                marginTop: 2,
              }}
            >
              {subtitle}
            </div>
          )}
        </div>
        {right && (
          <div
            onClick={(e) => e.stopPropagation()}
            style={{ display: "flex", alignItems: "center", gap: 6 }}
          >
            {right}
          </div>
        )}
        {collapsible && (
          <span
            style={{
              fontSize: s.type.body,
              color: t.tg,
              flexShrink: 0,
              transform: open ? "rotate(0deg)" : "rotate(-90deg)",
              transition: "transform 0.15s",
            }}
            aria-hidden
          >
            ▾
          </span>
        )}
      </header>
      {expanded && children}
    </section>
  );
}
