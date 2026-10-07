// Links handed over by the Import page (D5): add them to the batch
// once, then drop the page arg so a refresh doesn't look them up again.
import { useEffect } from "react";
import { useNavigation } from "../../providers/NavigationProvider";
import { linesOf } from "./text";
import type { ManualGrabBatch } from "./useManualGrabBatch";

export function useCarriedLinks(batch: ManualGrabBatch, initial?: string | number | null) {
  const { nav } = useNavigation();
  useEffect(() => {
    const carried = linesOf(initial);
    if (carried.length) {
      batch.addLinks(carried);
      nav("pipe-manual-grab", null);
    }
    // Only the hand-over on first render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
