// Service-health pill for the Hermes pipeline section. Colored dot
// + label + status text. Three states: ok (green), warn (yellow),
// err (red). Read-only — does not navigate; this is purely a status
// indicator.
import { useTheme } from "../../../theme";
import { useViewport } from "../../../hooks/useViewport";
import { RADIUS, scaleFor } from "../tokens";

export interface MobileHealthPillProps {
  label: string;
  ok: boolean;
  warn?: boolean;
  // Optional override status text. If omitted, derived as
  // "Online" / "Check" / "Offline" from ok+warn.
  status?: string;
}

export function MobileHealthPill({
  label,
  ok,
  warn,
  status,
}: MobileHealthPillProps) {
  const t = useTheme();
  const vp = useViewport();
  const s = scaleFor(vp);

  const tone = ok ? "ok" : warn ? "warn" : "err";
  const dotColor = tone === "ok" ? t.grn : tone === "warn" ? t.ylw : t.red;
  const statusText = status ?? (tone === "ok" ? "Online" : tone === "warn" ? "Check" : "Offline");
  const bg = tone === "ok" ? t.grnb : tone === "warn" ? t.ylwb : t.redb;
  const border = tone === "ok" ? t.grnt : tone === "warn" ? t.ylwt : t.redt;

  return (
    <div
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 8,
        padding: `8px ${s.space.md}px`,
        background: bg,
        border: `1px solid ${border}`,
        borderRadius: RADIUS.full,
        whiteSpace: "nowrap",
      }}
    >
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: dotColor,
          flexShrink: 0,
        }}
      />
      <span
        style={{
          fontSize: s.type.caption,
          fontWeight: 600,
          color: t.text,
        }}
      >
        {label}
      </span>
      <span
        style={{
          fontSize: s.type.micro,
          color: t.td,
        }}
      >
        {statusText}
      </span>
    </div>
  );
}
