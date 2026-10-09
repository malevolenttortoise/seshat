// A Dashboard stat tile: value (a spinner while it's null), label, an
// optional sub-line; `compact` for the Seshat Stats rail.
import { useTheme } from "../../theme";
import { Spin } from "../Spin";

interface TileProps {
  label: string;
  value: React.ReactNode;
  color?: string;
  sub?: string;
  onClick?: () => void;
}

export function Tile({ label, value, color, sub, onClick, compact = false }: TileProps & { compact?: boolean }) {
  const t = useTheme();
  // v2.21.0 Phase F.3 — compact variant scoped to the Seshat Stats
  // widget. ~30% less vertical real estate per tile so the widget
  // fits the new Amazon Cache section without a full dashboard
  // redesign.
  const padding = compact ? "6px 10px" : "12px 14px";
  const valueSize = compact ? 16 : 24;
  const labelSize = compact ? 10 : 13;
  const subSize = compact ? 9 : 10;
  const labelMargin = compact ? 1 : 4;
  return (
    <div
      onClick={onClick}
      style={{
        background: t.bg3,
        borderRadius: compact ? 6 : 8,
        padding,
        cursor: onClick ? "pointer" : "default",
      }}
    >
      <div
        style={{
          fontSize: valueSize,
          fontWeight: 700,
          color: color || t.text,
          lineHeight: 1.1,
        }}
      >
        {value === null ? <Spin size={compact ? 12 : 16} /> : value}
      </div>
      <div style={{ fontSize: labelSize, color: t.td, marginTop: labelMargin }}>{label}</div>
      {sub && (
        <div
          style={{
            fontSize: subSize,
            color: t.tf,
            marginTop: 2,
            textTransform: "uppercase",
            letterSpacing: "0.04em",
          }}
        >
          {sub}
        </div>
      )}
    </div>
  );
}
