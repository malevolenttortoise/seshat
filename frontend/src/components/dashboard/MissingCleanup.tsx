// "Clean up missing books" (discovery fix pass S8, G209): the dialog
// body both Dashboard shells show, the desktop in a modal and the phone in
// a bottom sheet. Each shell adds its own buttons; the state and the
// requests are `useMissingCleanup`'s.
import { useTheme } from "../../theme";
import { Spin } from "../Spin";
import { fmtNum, timeAgo } from "../../lib/format";
import {
  CLEANUP_STEPS,
  rowNote,
  stepVerb,
  type CleanupReport,
  type useMissingCleanup,
} from "../../hooks/useMissingCleanup";

type Cleanup = ReturnType<typeof useMissingCleanup>;

export function MissingCleanupBody({ mc, compact = false }: { mc: Cleanup; compact?: boolean }) {
  const t = useTheme();
  const { status, last, applied, fresh } = mc;
  const muted = { fontSize: compact ? 12 : 13, color: t.td };

  if (!status) {
    return (
      <div style={{ ...muted, display: "flex", alignItems: "center", gap: 8 }}>
        <Spin size={12} /> Loading…
      </div>
    );
  }

  if (status.running) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 14, color: t.text }}>
          <Spin size={14} />
          {status.mode === "apply" ? "Applying" : "Previewing"}…
          {status.library_name ? ` ${status.library_name}` : ""}
          {status.step_name ? ` · ${status.step_name}` : ""}
        </div>
        <div style={muted}>It runs in the background: you can close this and come back.</div>
      </div>
    );
  }

  const report = applied ?? (last && fresh ? last : null);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ fontSize: compact ? 13 : 14, color: t.text2 }}>
        {applied
          ? `Applied ${timeAgo(applied.finished_at).toLowerCase()}.`
          : report
            ? `Previewed ${timeAgo(report.finished_at).toLowerCase()}: nothing has changed yet. Apply makes these changes (a few more or fewer if the library changed since).`
            : "Applies today's discovery rules to the missing books listed before them. Preview runs every step on a copy of your databases: nothing changes until you Apply."}
      </div>
      {!report && last && !fresh && (
        <div style={{ ...muted, color: t.ylw }}>
          The last preview is over an hour old: preview again before applying.
        </div>
      )}
      {report ? <StepList report={report} compact={compact} /> : <StepIntro compact={compact} />}
      {report && report.errors.length > 0 && (
        <div style={{ fontSize: 12, color: t.red, background: t.redb, padding: 8, borderRadius: 6 }}>
          {report.errors.map((e) => <div key={e}>{e}</div>)}
        </div>
      )}
      <div style={{ fontSize: 11, color: t.td, background: t.bg3, padding: 8, borderRadius: 6 }}>
        Never touched: owned books, books you hid or showed, books hidden because an author's List missing
        books switch is off, and books a grab is linked to. No source is asked anything.
      </div>
    </div>
  );
}

function StepIntro({ compact }: { compact: boolean }) {
  const t = useTheme();
  return (
    <ol style={{ paddingLeft: 22, margin: 0, display: "flex", flexDirection: "column", gap: compact ? 6 : 10 }}>
      {CLEANUP_STEPS.map((s) => (
        <li key={s.key}>
          <div style={{ fontSize: 13, fontWeight: 600, color: t.text }}>{s.name}</div>
          <div style={{ fontSize: 12, color: t.text2, marginTop: 2 }}>{s.blurb}</div>
        </li>
      ))}
    </ol>
  );
}

function StepList({ report, compact }: { report: CleanupReport; compact: boolean }) {
  const t = useTheme();
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: compact ? 10 : 12 }}>
      {report.steps.map((s) => (
        <div key={s.key} style={{ borderTop: `1px solid ${t.borderL}`, paddingTop: 8 }}>
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "baseline", gap: "4px 8px" }}>
            <span
              style={{
                fontSize: 13,
                fontWeight: 700,
                color: s.count ? t.accent : t.td,
                fontVariantNumeric: "tabular-nums",
              }}
            >
              {stepVerb(s.action, report.mode)} {fmtNum(s.count)}
            </span>
            <span style={{ fontSize: 13, color: t.text }}>{s.name}</span>
            {s.skipped_count > 0 && (
              <span style={{ fontSize: 12, color: t.td }}>· {fmtNum(s.skipped_count)} left as they are</span>
            )}
          </div>
          {s.sample.length > 0 && (
            <ul style={{ margin: "6px 0 0", paddingLeft: 18, display: "flex", flexDirection: "column", gap: 2 }}>
              {s.sample.map((r, i) => {
                const note = rowNote(s.key, r);
                return (
                  <li key={`${r.library}-${r.id ?? "n"}-${i}`} style={{ fontSize: 12, color: t.text2, minWidth: 0 }}>
                    {r.title}
                    {r.authors && r.authors.length > 0 && (
                      <span style={{ color: t.td }}> — {r.authors.join(", ")}</span>
                    )}
                    {note && <span style={{ color: t.tf }}> · {note}</span>}
                  </li>
                );
              })}
            </ul>
          )}
          {s.count > s.sample.length && (
            <div style={{ fontSize: 12, color: t.td, marginTop: 4, paddingLeft: 18 }}>
              …and {fmtNum(s.count - s.sample.length)} more
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
