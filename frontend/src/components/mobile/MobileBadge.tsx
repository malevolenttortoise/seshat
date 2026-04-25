// Small status pill — read-only, not interactive. For state labels
// like "missing", "owned", "queued", or counts on a card. If you
// need a tappable filter pill, use MobileChip instead.
import type { ReactNode } from "react";
import { useTheme } from "../../theme";
import { useViewport } from "../../hooks/useViewport";
import { RADIUS, scaleFor } from "./tokens";

type Tone = "neutral" | "accent" | "ok" | "warn" | "err" | "info";

export interface MobileBadgeProps {
  children: ReactNode;
  tone?: Tone;
  // Subtle = tinted background, no border. Solid = filled with the
  // tone color. Default subtle reads as a label, not an alert.
  variant?: "subtle" | "solid";
}

export function MobileBadge({
  children,
  tone = "neutral",
  variant = "subtle",
}: MobileBadgeProps) {
  const t = useTheme();
  const vp = useViewport();
  const s = scaleFor(vp);

  const colorMap = {
    neutral: { fg: t.text2, bg: t.bg3, border: t.border, solidFg: t.bg, solidBg: t.text2 },
    accent: { fg: t.accent, bg: t.abg, border: t.abr, solidFg: t.bg, solidBg: t.accent },
    ok: { fg: t.grn, bg: t.grnb, border: t.grnt, solidFg: t.bg, solidBg: t.grn },
    warn: { fg: t.ylw, bg: t.ylwb, border: t.ylwt, solidFg: t.bg, solidBg: t.ylw },
    err: { fg: t.red, bg: t.redb, border: t.redt, solidFg: t.bg, solidBg: t.red },
    info: { fg: t.cyan, bg: t.cyanb, border: t.cyant, solidFg: t.bg, solidBg: t.cyan },
  } as const;
  const c = colorMap[tone];

  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: `4px ${s.space.sm}px`,
        background: variant === "solid" ? c.solidBg : c.bg,
        color: variant === "solid" ? c.solidFg : c.fg,
        border: variant === "solid" ? "none" : `1px solid ${c.border}`,
        borderRadius: RADIUS.full,
        fontSize: s.type.micro,
        fontWeight: 600,
        lineHeight: 1.2,
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </span>
  );
}
