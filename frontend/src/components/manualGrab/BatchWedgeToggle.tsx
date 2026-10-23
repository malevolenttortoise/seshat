// Manual Grab's batch wedge toggle: the full WedgeToggle wired to the
// batch, for the desktop footer and the phone page (`compact`).
import { WedgeToggle } from "../WedgeToggle";
import type { ManualGrabBatch } from "./useManualGrabBatch";

export function BatchWedgeToggle({ batch, compact }: { batch: ManualGrabBatch; compact?: boolean }) {
  return (
    <WedgeToggle
      form="full"
      compact={compact}
      checked={batch.useWedges}
      onChange={(on) => void batch.setUseWedges(on)}
      disabled={batch.grabbing}
      offered={batch.offerWedges}
      switchedOff={batch.wedgesSwitchedOff}
      eligible={batch.eligibleForWedges}
      needed={batch.wedgeCount}
      budget={batch.wedges}
      short={batch.wedgeShort}
      error={batch.wedgeError}
    />
  );
}
