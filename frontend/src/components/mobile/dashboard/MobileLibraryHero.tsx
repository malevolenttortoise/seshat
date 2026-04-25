// Library ownership hero card. Shows: library name, owned vs total
// books, ownership %, and a progress bar tinted by completion. The
// MAM metrics row (upload candidates / available / missing) renders
// below as three tappable mini-tiles.
//
// Used on the unified dashboard for Athena (one card per library —
// ebook + audiobook stack vertically) and on the disc dashboard as
// the page hero.
import { useTheme } from "../../../theme";
import { useViewport } from "../../../hooks/useViewport";
import { fmtNum, pct } from "../../../lib/format";
import { RADIUS, scaleFor } from "../tokens";
import { MobileStatTile } from "./MobileStatTile";

export interface LibraryHeroStats {
  owned_books?: number;
  total_books?: number;
  missing_books?: number;
  new_books?: number;
  authors?: number;
  total_series?: number;
  upcoming_books?: number;
  suggestions?: number;
  total_duration_sec?: number;
  narrator_count?: number;
  unabridged_count?: number;
  mam?: {
    upload_candidates?: number;
    available_to_download?: number;
    missing_everywhere?: number;
    total_unscanned?: number;
  };
}

export interface MobileLibraryHeroProps {
  title: string;
  // Color used for the percent value + progress bar. Pass theme.jade
  // for ebooks, theme.cyan for audiobooks (Mark's Egyptian palette).
  color: string;
  // Optional icon next to the title — emoji is fine.
  icon?: string;
  stats: LibraryHeroStats;
  onMamClick?: () => void;
}

export function MobileLibraryHero({
  title,
  color,
  icon,
  stats,
  onMamClick,
}: MobileLibraryHeroProps) {
  const t = useTheme();
  const vp = useViewport();
  const s = scaleFor(vp);

  const owned = stats.owned_books ?? 0;
  const total = stats.total_books ?? 0;
  const ownedPct = pct(owned, total);
  const mam = stats.mam ?? {};
  const showMam =
    mam.upload_candidates !== undefined ||
    mam.available_to_download !== undefined ||
    mam.missing_everywhere !== undefined;

  return (
    <div
      style={{
        background: t.bg2,
        border: `1px solid ${t.border}`,
        borderRadius: RADIUS.lg,
        padding: s.pad.normal,
        display: "flex",
        flexDirection: "column",
        gap: s.space.md,
      }}
    >
      {/* Title + percent row */}
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          gap: s.space.sm,
        }}
      >
        <h2
          style={{
            margin: 0,
            fontSize: s.type.heading,
            fontWeight: 700,
            color: t.text,
            display: "flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          {icon && <span>{icon}</span>}
          <span>{title}</span>
        </h2>
        <div
          style={{
            fontSize: s.type.title,
            fontWeight: 700,
            color: color,
            lineHeight: 1,
          }}
        >
          {ownedPct}%
        </div>
      </div>

      {/* Owned / total counts + progress bar */}
      <div>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            fontSize: s.type.caption,
            color: t.td,
            marginBottom: 4,
          }}
        >
          <span>
            <strong style={{ color: t.text }}>{fmtNum(owned)}</strong> owned
          </span>
          <span>
            of <strong style={{ color: t.text }}>{fmtNum(total)}</strong>
          </span>
        </div>
        <div
          style={{
            height: 8,
            borderRadius: RADIUS.full,
            background: t.bg3,
            overflow: "hidden",
          }}
        >
          <div
            style={{
              width: `${Math.min(100, ownedPct)}%`,
              height: "100%",
              background: color,
              transition: "width 0.3s",
            }}
          />
        </div>
      </div>

      {/* MAM metrics — three tiles in a row */}
      {showMam && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr 1fr",
            gap: s.space.sm,
          }}
        >
          <MobileStatTile
            label="↑ Upload"
            value={fmtNum(mam.upload_candidates ?? 0)}
            color={t.cyan}
            onClick={onMamClick}
          />
          <MobileStatTile
            label="↓ Avail"
            value={fmtNum(mam.available_to_download ?? 0)}
            color={t.grn}
            onClick={onMamClick}
          />
          <MobileStatTile
            label="∅ Missing"
            value={fmtNum(mam.missing_everywhere ?? 0)}
            color={t.red}
            onClick={onMamClick}
          />
        </div>
      )}
    </div>
  );
}
