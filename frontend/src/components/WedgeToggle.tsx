// The one "use wedge" control, for every place a grab can spend a wedge
// (audit G139). Both places follow the MAM page's "wedges on manual
// grabs" setting (`mam_economy_manual_wedge_offer_enabled`); the caller
// reads it.
//
// - The full form is Manual Grab's batch toggle with what goes around
//   it: the spendable count, the not-enough-wedges block, the "wedges
//   are off" hint and the balance error. `compact` keeps the phone's
//   labels and spacing.
// - The plain form is the book sidebar's per-book tick: a checkbox and
//   "Use wedge". The sidebar decides when it shows.
import { useTheme } from "../theme";
import { useNavigation } from "../providers/NavigationProvider";
import type { WedgeBudget } from "./manualGrab/types";

interface ToggleState {
  checked: boolean;
  onChange: (on: boolean) => void;
}

export interface PlainWedgeToggleProps extends ToggleState {
  form: "plain";
}

export interface FullWedgeToggleProps extends ToggleState {
  form: "full";
  compact?: boolean;
  disabled?: boolean;
  /** The setting is on: the toggle can show. */
  offered: boolean;
  /** The setting was read and is off: the hint can show. */
  switchedOff: boolean;
  /** Rows a wedge could make free. */
  eligible: number;
  /** Wedges the ticked rows need while the toggle is on. */
  needed: number;
  budget: WedgeBudget | null;
  /** `needed` is more than `budget` can spend. */
  short: boolean;
  error: string | null;
}

export type WedgeToggleProps = PlainWedgeToggleProps | FullWedgeToggleProps;

export function WedgeToggle(props: WedgeToggleProps) {
  return props.form === "plain" ? <PlainToggle {...props} /> : <FullToggle {...props} />;
}

function PlainToggle({ checked, onChange }: PlainWedgeToggleProps) {
  return (
    <label
      style={{
        display: "flex",
        gap: 4,
        alignItems: "center",
        cursor: "pointer",
      }}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      Use wedge
    </label>
  );
}

function FullToggle({
  compact, checked, onChange, disabled, offered, switchedOff, eligible, needed, budget, short, error,
}: FullWedgeToggleProps) {
  const t = useTheme();
  const { nav } = useNavigation();
  const pad = compact ? { paddingBottom: 8 } : {};
  const box = (
    <input
      type="checkbox"
      checked={checked}
      disabled={disabled}
      onChange={(e) => onChange(e.target.checked)}
      style={compact ? { width: 20, height: 20, accentColor: t.accent } : { accentColor: t.accent }}
    />
  );
  return (
    <>
      {offered && (checked || eligible > 0) && (compact ? (
        <label style={{ fontSize: 14, color: t.text2, display: "flex", alignItems: "center", gap: 10, padding: "10px 0" }}>
          {box}
          <span>
            Use wedges on rows that aren't free
            {checked && budget && (
              <span style={{ color: t.td }}> ({budget.spendable} spendable)</span>
            )}
          </span>
        </label>
      ) : (
        <label style={{ fontSize: 13, color: t.text2, display: "flex", alignItems: "center", gap: 8 }}>
          {box}
          Use wedges on the ticked rows that aren't free
          {checked && budget && (
            <span style={{ color: t.td }}>
              ({budget.spendable} spendable: {budget.wedges} − {budget.reserved} reserved)
            </span>
          )}
        </label>
      ))}
      {switchedOff && eligible > 0 && (
        <div style={{ fontSize: 12, color: t.td, ...pad }}>
          Wedges are off for manual grabs. Turn them on in{" "}
          <a
            href="#"
            onClick={(e) => { e.preventDefault(); nav("pipe-mam"); }}
            style={{ color: t.accent }}
          >
            MAM Status › Wedges on manual grabs
          </a>
          .
        </div>
      )}
      {error && <div style={{ fontSize: 12, color: t.err }}>{error}</div>}
      {short && budget && (
        <div style={{ fontSize: 12, color: t.err, ...pad }}>
          Needs {needed} wedges, {budget.spendable} spendable
          ({budget.wedges} − {budget.reserved} reserved). Untick rows or turn wedges off.
        </div>
      )}
    </>
  );
}
