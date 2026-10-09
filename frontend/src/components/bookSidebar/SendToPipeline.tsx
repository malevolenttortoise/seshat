// Send to pipeline, in the book sidebar: grab a book MAM has (found, not
// snatched) as a server-side job, with the per-book "Use wedge" tick and
// the buffer-gate preflight.
//
// The sidebar calls the hook and places the button (in the MAM row), the
// wedge tick (under it) and the buffer banner (at the bottom of the
// panel) where they've always been (audit G158).
import { useEffect, useState } from "react";
import { useTheme } from "../../theme";
import { runBatchJob } from "../../lib/batchJob";
import { economyApi, type PreflightResponse } from "../../lib/economyApi";
import type { Book } from "../../types";
import { Btn } from "../Btn";
import { Spin } from "../Spin";
import { BufferInsufficientBanner } from "../BufferInsufficientBanner";
import { WedgeToggle } from "../WedgeToggle";

interface SendToPipelineResponse {
  sent: number;
  message?: string;
}

export function useSendToPipeline(book: Book) {
  const [sending, setSending] = useState(false);
  // Economy offers — the "use wedge" tick only renders when the user has
  // opted into it via MamPage. (The "buy personal FL" tick was dropped
  // 2026-10-07: MAM refuses spendtype=personalFL via the API, "Not
  // allowed via API".) `preflight` caches the result of the most recent
  // buffer gate check for this book so the BufferInsufficientBanner has
  // something to render.
  const [offerWedge, setOfferWedge] = useState(false);
  const [bufferGateOn, setBufferGateOn] = useState(false);
  const [useWedge, setUseWedge] = useState(false);
  const [preflight, setPreflight] = useState<PreflightResponse | null>(null);

  // Economy offers config — cheap one-shot fetch. Failures are
  // non-blocking (the tick just stays hidden).
  useEffect(() => {
    economyApi
      .getConfig()
      .then((cfg) => {
        setOfferWedge(!!cfg.mam_economy_manual_wedge_offer_enabled);
        setBufferGateOn(!!cfg.mam_economy_buffer_gate_enabled);
      })
      .catch(() => {});
  }, []);

  const send = async () => {
    if (sending) return;
    setSending(true);
    setPreflight(null);

    // Buffer-gate preflight: only when the gate is enabled AND the
    // book has a MAM torrent ID we can probe. A failed preflight
    // (no torrent ID, MAM offline, etc.) falls through to the
    // normal grab — the server-side gate is authoritative.
    if (bufferGateOn && book.mam_torrent_id) {
      try {
        const match = /(\d+)/.exec(String(book.mam_torrent_id));
        if (match) {
          const pf = await economyApi.preflight(match[1]);
          if (!pf.sufficient) {
            setPreflight(pf);
            setSending(false);
            return;
          }
        }
      } catch {
        /* preflight is best-effort — let the server decide */
      }
    }

    try {
      const r = await runBatchJob<SendToPipelineResponse>(
        "/discovery/send-to-pipeline",
        {
          book_ids: [book.id],
          use_wedge_override: useWedge,
        },
      );
      if (r.sent > 0) {
        alert("Sent to pipeline for download!");
        setUseWedge(false);
      } else {
        alert(r.message || "Failed to send");
      }
    } catch (e) {
      alert(`Send failed: ${(e as Error).message || e}`);
    }
    setSending(false);
  };

  return { sending, send, offerWedge, useWedge, setUseWedge, preflight, setPreflight };
}

export type SendToPipelineState = ReturnType<typeof useSendToPipeline>;

export function SendButton({ s }: { s: SendToPipelineState }) {
  const t = useTheme();
  return (
    <Btn
      size="sm"
      onClick={s.send}
      disabled={s.sending}
      style={{
        background: t.accent + "22",
        color: t.accent,
        border: `1px solid ${t.accent}44`,
      }}
    >
      {s.sending ? <Spin /> : "⬇"} Send to pipeline
    </Btn>
  );
}

// On its own row so narrow sidebar widths don't wrap the Found /
// Re-scan / Send buttons around it. The sidebar shows it only where the
// Send button shows and the wedge offer is on.
export function SendWedgeRow({ s }: { s: SendToPipelineState }) {
  const t = useTheme();
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "flex-end",
        gap: 14,
        marginTop: 4,
        fontSize: 11,
        color: t.tg,
        flexWrap: "wrap",
      }}
    >
      <WedgeToggle form="plain" checked={s.useWedge} onChange={s.setUseWedge} />
    </div>
  );
}

export function SendBufferGate({ s }: { s: SendToPipelineState }) {
  if (!s.preflight) return null;
  return (
    <div style={{ margin: "12px 14px 0" }}>
      <BufferInsufficientBanner
        preflight={s.preflight}
        onBufferReady={() => {
          s.setPreflight(null);
          s.send();
        }}
        onCancel={() => s.setPreflight(null)}
      />
    </div>
  );
}
