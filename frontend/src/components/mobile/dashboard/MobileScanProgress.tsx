// Per-scan progress row. Shows label, current/total, percentage,
// progress bar, status text, and an optional Cancel button while
// the scan is running. When complete, collapses to a one-line
// summary with the timestamp.
import { useTheme } from "../../../theme";
import { useViewport } from "../../../hooks/useViewport";
import type { ScanProgress } from "../../../types";
import { RADIUS, scaleFor } from "../tokens";
import { MobileBtn } from "../MobileBtn";

export interface MobileScanProgressProps {
  scan: ScanProgress;
  // Friendly label override. Defaults to scan.label.
  label?: string;
  onCancel?: () => void;
}

export function MobileScanProgress({
  scan,
  label,
  onCancel,
}: MobileScanProgressProps) {
  const t = useTheme();
  const vp = useViewport();
  const s = scaleFor(vp);

  const displayLabel = label ?? scan.label;
  const total = scan.total ?? 0;
  const cur = scan.current ?? 0;
  const progress = total > 0 ? Math.min(100, (cur / total) * 100) : 0;
  const isRunning = scan.running;

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: s.space.xs,
        padding: `${s.space.sm}px ${s.space.md}px`,
        background: isRunning ? t.abg : t.bg3,
        border: `1px solid ${isRunning ? t.abr : t.borderL}`,
        borderRadius: RADIUS.md,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: s.space.sm,
        }}
      >
        <div
          style={{
            flex: 1,
            minWidth: 0,
            fontSize: s.type.caption,
            fontWeight: 700,
            color: isRunning ? t.accent : t.text2,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {displayLabel}
        </div>
        {isRunning ? (
          <span
            style={{
              fontSize: s.type.micro,
              color: t.td,
              fontWeight: 600,
              flexShrink: 0,
            }}
          >
            {cur} / {total} · {Math.round(progress)}%
          </span>
        ) : (
          <span
            style={{
              fontSize: s.type.micro,
              color: t.tg,
              flexShrink: 0,
            }}
          >
            {scan.status || "Idle"}
          </span>
        )}
      </div>
      {isRunning && (
        <>
          <div
            style={{
              height: 4,
              borderRadius: RADIUS.full,
              background: t.bg4,
              overflow: "hidden",
            }}
          >
            <div
              style={{
                width: `${progress}%`,
                height: "100%",
                background: t.accent,
                transition: "width 0.3s",
              }}
            />
          </div>
          {scan.current_book && (
            <div
              style={{
                fontSize: s.type.micro,
                color: t.td,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {scan.current_book}
            </div>
          )}
          {onCancel && (
            <MobileBtn
              variant="ghost"
              onClick={onCancel}
              style={{ alignSelf: "flex-end", minHeight: 36, fontSize: s.type.caption }}
            >
              Stop
            </MobileBtn>
          )}
        </>
      )}
    </div>
  );
}
