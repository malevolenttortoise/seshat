// Compact MAM account card: username/class + ratio + wedges +
// upload/download bytes. Read-only on mobile — tap-target is the
// whole card and routes to the MAM settings page so the user can
// fix a misconfigured cookie quickly.
import { useTheme } from "../../../theme";
import { useViewport } from "../../../hooks/useViewport";
import { fmtBytes, fmtNum, fmtRatio } from "../../../lib/format";
import { RADIUS, scaleFor } from "../tokens";

export interface MobileMamAccountStats {
  username?: string;
  classname?: string;
  ratio?: number;
  wedges?: number;
  seedbonus?: number;
  upload_buffer_bytes?: number;
  uploaded_bytes?: number;
  downloaded_bytes?: number;
}

export interface MobileMamAccountProps {
  mam: MobileMamAccountStats;
  onClick?: () => void;
}

export function MobileMamAccount({ mam, onClick }: MobileMamAccountProps) {
  const t = useTheme();
  const vp = useViewport();
  const s = scaleFor(vp);

  if (!mam.username) return null;

  const cell = (label: string, value: React.ReactNode, color?: string) => (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 2,
        alignItems: "flex-start",
      }}
    >
      <span
        style={{
          fontSize: s.type.micro,
          color: t.tg,
          textTransform: "uppercase",
          letterSpacing: "0.04em",
          fontWeight: 600,
        }}
      >
        {label}
      </span>
      <span
        style={{
          fontSize: s.type.label,
          fontWeight: 700,
          color: color || t.text,
        }}
      >
        {value}
      </span>
    </div>
  );

  return (
    <button
      onClick={onClick}
      disabled={!onClick}
      style={{
        display: "flex",
        flexDirection: "column",
        gap: s.space.sm,
        padding: s.pad.tight,
        background: t.bg3,
        border: `1px solid ${t.border}`,
        borderRadius: RADIUS.md,
        cursor: onClick ? "pointer" : "default",
        textAlign: "left",
        width: "100%",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          gap: s.space.sm,
        }}
      >
        <span style={{ fontSize: s.type.body, fontWeight: 700, color: t.accent }}>
          {mam.username}
        </span>
        {mam.classname && (
          <span style={{ fontSize: s.type.caption, color: t.td }}>
            {mam.classname}
          </span>
        )}
      </div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr 1fr",
          gap: s.space.sm,
        }}
      >
        {cell("Ratio", fmtRatio(mam.ratio), t.grn)}
        {cell("Wedges", fmtNum(mam.wedges ?? 0), t.accent)}
        {cell("Bonus", fmtNum(mam.seedbonus ?? 0), t.cyan)}
      </div>
      {(mam.uploaded_bytes !== undefined || mam.downloaded_bytes !== undefined) && (
        <div
          style={{
            display: "flex",
            gap: s.space.md,
            fontSize: s.type.micro,
            color: t.td,
            paddingTop: s.space.xs,
            borderTop: `1px solid ${t.borderL}`,
          }}
        >
          <span>↑ {fmtBytes(mam.uploaded_bytes)}</span>
          <span>↓ {fmtBytes(mam.downloaded_bytes)}</span>
        </div>
      )}
    </button>
  );
}
