// Snatch budget display + active seeding entries. The desktop
// version splits into two side-by-side columns; mobile stacks them.
//
// Top: budget used / cap as a fraction, queue + ledger breakdown,
// next-release countdown.
// Bottom: scrollable list of currently-seeding entries with per-row
// progress bar + remaining-time label.
import { useTheme } from "../../../theme";
import { useViewport } from "../../../hooks/useViewport";
import { fmtDuration, fmtNum } from "../../../lib/format";
import { RADIUS, scaleFor } from "../tokens";

export interface BudgetEntry {
  grab_id?: number | null;
  torrent_name?: string;
  source?: string;
  seeding_seconds?: number;
  remaining_seconds?: number;
}

// `next_release_seconds` is nullable on the wire (API returns null
// when no torrent is currently waiting to release into the budget),
// so accept both null and undefined here. Same shape both desktop
// dashboards consume.
export interface BudgetData {
  budget_used?: number;
  budget_cap?: number;
  next_release_seconds?: number | null;
  ledger_active?: number;
  qbit_extras?: number;
  queue_size?: number;
  seed_seconds_required?: number;
  entries?: BudgetEntry[];
}

export interface MobileSnatchBudgetProps {
  budget: BudgetData;
}

export function MobileSnatchBudget({ budget }: MobileSnatchBudgetProps) {
  const t = useTheme();
  const vp = useViewport();
  const s = scaleFor(vp);

  const used = budget.budget_used ?? 0;
  const cap = budget.budget_cap ?? 0;
  const ledger = budget.ledger_active ?? 0;
  const qbitExtras = budget.qbit_extras ?? 0;
  const queue = budget.queue_size ?? 0;
  const next = budget.next_release_seconds ?? 0;
  const entries = budget.entries ?? [];
  const seedReq = budget.seed_seconds_required ?? 0;

  const usedPct = cap > 0 ? Math.min(100, (used / cap) * 100) : 0;
  const tone = usedPct >= 90 ? t.red : usedPct >= 70 ? t.ylw : t.grn;

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: s.space.md,
      }}
    >
      {/* Used / cap — large numerator, big enough to read at a glance */}
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          gap: s.space.sm,
        }}
      >
        <div>
          <span style={{ fontSize: s.type.title, fontWeight: 700, color: tone }}>
            {used}
          </span>
          <span style={{ fontSize: s.type.body, color: t.td, marginLeft: 6 }}>
            / {cap}
          </span>
        </div>
        <span style={{ fontSize: s.type.caption, color: t.td }}>budget</span>
      </div>

      {/* Cap fill bar */}
      <div
        style={{
          height: 6,
          borderRadius: RADIUS.full,
          background: t.bg3,
          overflow: "hidden",
        }}
      >
        <div
          style={{
            width: `${usedPct}%`,
            height: "100%",
            background: tone,
            transition: "width 0.3s",
          }}
        />
      </div>

      {/* Ledger / queue / next-release breakdown */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr 1fr",
          gap: s.space.sm,
          fontSize: s.type.caption,
        }}
      >
        <div>
          <div style={{ color: t.tg, fontSize: s.type.micro, fontWeight: 600 }}>
            Ledger
          </div>
          <div style={{ color: t.text, fontWeight: 700 }}>{fmtNum(ledger)}</div>
        </div>
        <div>
          <div style={{ color: t.tg, fontSize: s.type.micro, fontWeight: 600 }}>
            qBit
          </div>
          <div style={{ color: t.text, fontWeight: 700 }}>{fmtNum(qbitExtras)}</div>
        </div>
        <div>
          <div style={{ color: t.tg, fontSize: s.type.micro, fontWeight: 600 }}>
            Queue
          </div>
          <div style={{ color: t.text, fontWeight: 700 }}>{fmtNum(queue)}</div>
        </div>
      </div>

      {next > 0 && (
        <div
          style={{
            fontSize: s.type.caption,
            color: t.td,
            display: "flex",
            justifyContent: "space-between",
            paddingTop: s.space.xs,
            borderTop: `1px solid ${t.borderL}`,
          }}
        >
          <span>Next release</span>
          <span style={{ color: t.text, fontWeight: 600 }}>
            {fmtDuration(next)}
          </span>
        </div>
      )}

      {/* Active seeding entries list */}
      {entries.length > 0 && (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: s.space.xs,
            paddingTop: s.space.xs,
            borderTop: `1px solid ${t.borderL}`,
          }}
        >
          <div
            style={{
              fontSize: s.type.micro,
              color: t.tg,
              fontWeight: 600,
              textTransform: "uppercase",
              letterSpacing: "0.04em",
              marginBottom: 4,
            }}
          >
            Seeding ({entries.length})
          </div>
          <div
            style={{
              maxHeight: 180,
              overflowY: "auto",
              display: "flex",
              flexDirection: "column",
              gap: s.space.xs,
            }}
          >
            {entries.map((e) => {
              const rem = e.remaining_seconds ?? 0;
              const seeded = e.seeding_seconds ?? 0;
              const need = seedReq > 0 ? seedReq : seeded + rem;
              const progress = need > 0 ? Math.min(100, (seeded / need) * 100) : 0;
              return (
                <div
                  key={e.grab_id}
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: 4,
                    padding: `${s.space.xs}px ${s.space.sm}px`,
                    background: t.bg3,
                    borderRadius: RADIUS.sm,
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      gap: 6,
                      alignItems: "center",
                      fontSize: s.type.caption,
                    }}
                  >
                    {e.source === "external" && (
                      <span
                        style={{
                          fontSize: 9,
                          padding: "1px 4px",
                          background: t.purb,
                          color: t.pur,
                          borderRadius: RADIUS.sm,
                          fontWeight: 700,
                        }}
                      >
                        EXT
                      </span>
                    )}
                    <span
                      style={{
                        flex: 1,
                        minWidth: 0,
                        color: t.text2,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {e.torrent_name || `#${e.grab_id}`}
                    </span>
                    <span style={{ color: t.tg, fontSize: s.type.micro }}>
                      {fmtDuration(rem)}
                    </span>
                  </div>
                  <div
                    style={{
                      height: 3,
                      background: t.bg4,
                      borderRadius: RADIUS.full,
                    }}
                  >
                    <div
                      style={{
                        width: `${progress}%`,
                        height: "100%",
                        background: t.cyan,
                        borderRadius: RADIUS.full,
                      }}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
