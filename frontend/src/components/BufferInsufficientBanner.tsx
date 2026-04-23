// Shown on the manual-inject confirm UI when the pre-download
// buffer gate would refuse the grab. Renders size/buffer/shortfall
// and offers two actions:
//
//   - "Buy N GB"  → posts to /v1/mam/economy/upload/buy and, on
//                   success, invokes `onBufferReady` so the parent
//                   can retry the inject.
//   - "Cancel"    → calls `onCancel` so the parent can dismiss.
//
// The banner is intentionally dumb about what "retry the inject"
// means — the parent wires the retry callback. That lets the same
// component plug into BookSidebar, DiscMAMPage, or any future
// inject entry point without duplicating the math here.

import { useState } from "react";
import { Btn } from "./Btn";
import { Spin } from "./Spin";
import {
  economyApi,
  formatBp,
  type PreflightResponse,
} from "../lib/economyApi";
import { toast } from "../lib/toast";
import { useTheme } from "../theme";

export interface Props {
  preflight: PreflightResponse;
  onBufferReady: () => void;
  onCancel?: () => void;
}

export function BufferInsufficientBanner({
  preflight,
  onBufferReady,
  onCancel,
}: Props) {
  const theme = useTheme();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function buyAndRetry() {
    setBusy(true);
    setError(null);
    try {
      const result = await economyApi.uploadBuy({
        gb: preflight.recommended_buy_gb,
      });
      if (!result.ok) {
        setError(result.message || "Buy failed");
        toast.error(`Upload buy failed: ${result.message}`);
        return;
      }
      toast.success(
        `Upload buy OK — new seedbonus ${result.new_seedbonus?.toLocaleString() ?? "?"}`,
      );
      onBufferReady();
    } catch (e) {
      setError(String(e));
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      style={{
        background: theme.warn + "18",
        border: `1px solid ${theme.warn}55`,
        borderRadius: 10,
        padding: "12px 14px",
        fontSize: 13,
        color: theme.text2,
        marginBottom: 12,
      }}
    >
      <div
        style={{
          fontWeight: 700,
          color: theme.warn,
          marginBottom: 4,
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        <span>Buffer gate: not enough upload buffer</span>
      </div>
      <div style={{ color: theme.textDim, lineHeight: 1.55 }}>
        This torrent would need{" "}
        <strong style={{ color: theme.text }}>
          {preflight.size_gb.toFixed(2)} GB
        </strong>
        , but your buffer is only{" "}
        <strong style={{ color: theme.text }}>
          {preflight.buffer_gb.toFixed(2)} GB
        </strong>{" "}
        (safety margin {preflight.safety_margin_gb.toFixed(1)} GB).
        You're{" "}
        <strong style={{ color: theme.warn }}>
          {preflight.shortfall_gb.toFixed(2)} GB
        </strong>{" "}
        short.
      </div>

      {error && (
        <div
          style={{
            color: theme.err,
            background: theme.err + "18",
            padding: "6px 10px",
            borderRadius: 6,
            marginTop: 10,
            fontSize: 12,
          }}
        >
          {error}
        </div>
      )}

      <div
        style={{
          marginTop: 12,
          display: "flex",
          gap: 8,
          justifyContent: "flex-end",
        }}
      >
        {onCancel && (
          <Btn variant="ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </Btn>
        )}
        <Btn
          variant="primary"
          onClick={buyAndRetry}
          disabled={busy || preflight.recommended_buy_gb <= 0}
        >
          {busy ? (
            <Spin size={14} />
          ) : (
            <>
              Buy {preflight.recommended_buy_gb} GB (
              {formatBp(preflight.recommended_buy_cost_bp)}) &amp; retry
            </>
          )}
        </Btn>
      </div>
    </div>
  );
}
