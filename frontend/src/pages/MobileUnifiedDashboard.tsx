// Mobile-native unified dashboard. The desktop UnifiedDashboard
// branches to this component when useMobileCodepath() is true (phone
// or iPad). Renders a vertical stack of sections instead of the
// 2-3 column grid the desktop uses.
//
// Its data and commands come from useDashboard, shared with the desktop.
import { toast } from "../lib/toast";
import { useTheme } from "../theme";
import { fmtNum } from "../lib/format";
import { useDashboard } from "../hooks/useDashboard";
import type { NavFn, ScanProgress } from "../types";
import {
  MobileBtn,
  MobileSection,
  MobileRow,
} from "../components/mobile";
import {
  MobileLibraryHero,
  MobileHealthPill,
  MobileMamAccount,
  MobileSnatchBudget,
  MobileScanProgress,
  MobileRecentActivity,
  MobileStatTile,
} from "../components/mobile/dashboard";

interface Props {
  onNav: NavFn;
}

export default function MobileUnifiedDashboard({ onNav }: Props) {
  const t = useTheme();
  const {
    health, mam, budget, reviewCount, counts, scans: scanStatus, ebookStats, audiobookStats, settings, grabs,
    tentativeCount, syncingSlug, scanning, mamScanning, showHygieneConfirm, setShowHygieneConfirm, hygieneStarting,
    triggerSync, triggerEbookSources, triggerAudiobookSources, triggerMam,
    cancelSources, cancelMam, triggerHygiene, cancelHygiene,
  } = useDashboard(toast.error);

  // Pipeline health derivations
  const dispatcherOk = !!health?.dispatcher_ready;
  const mamCookieOk = !!mam?.cookie_configured && !mam?.error;
  const ircOk = !!mam?.username; // proxy: if MAM stats are flowing, IRC + MAM are reachable

  const calibreWebUrl = settings?.cwa_web_url || settings?.calibre_web_url || "";
  const absWebUrl = settings?.abs_web_url || "";
  const allowed = counts?.authors_allowed ?? 0;
  const ignored = counts?.authors_ignored ?? 0;
  const totalGrabs = counts?.grabs ?? 0;
  const calibreAdds = counts?.calibre_additions ?? 0;

  const lookupScan = scanStatus.find((s) => s.kind === "lookup");
  const mamScan = scanStatus.find((s) => s.kind === "mam");
  const hygieneScan = scanStatus.find((s) => s.kind === "hygiene");
  const libScans = scanStatus.filter((s) => s.kind === "library");
  const activeScans = scanStatus.filter((s) => s.running);

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 12,
      }}
    >
      {/* ─── Pipeline health pills ─────────────────────────── */}
      {/* Wrap to a second line on narrow phones — all four statuses
          should be visible at a glance, not hidden behind a horizontal
          scroll the user has to discover. */}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 6,
          padding: "4px 2px",
        }}
      >
        <MobileHealthPill label="Dispatcher" ok={dispatcherOk} />
        <MobileHealthPill label="MAM" ok={mamCookieOk} />
        <MobileHealthPill label="IRC" ok={ircOk} warn={!ircOk} />
        <MobileHealthPill
          label="Budget"
          ok={(budget?.budget_used ?? 0) < (budget?.budget_cap ?? 1)}
          warn={(budget?.budget_used ?? 0) >= (budget?.budget_cap ?? 1) * 0.9}
        />
      </div>

      {/* ─── Athena: library heroes ────────────────────────── */}
      <MobileLibraryHero
        title={ebookStats.library_display_name || "Library"}
        icon="📖"
        color={t.jade}
        stats={ebookStats}
        onMamClick={() => onNav("disc-mam")}
      />
      {audiobookStats && (
        <MobileLibraryHero
          title={audiobookStats.library_display_name || "Audiobooks"}
          icon="🎧"
          color={t.cyan}
          stats={audiobookStats}
          onMamClick={() => onNav("disc-mam")}
        />
      )}

      {/* ─── Command Center: triggers + active scans ───────── */}
      <MobileSection title="Command Center" defaultOpen={true}>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
            <MobileBtn
              variant="secondary"
              fullWidth
              onClick={() => triggerSync()}
              disabled={syncingSlug !== null}
            >
              {syncingSlug ? "Syncing…" : "Sync Library"}
            </MobileBtn>
            <MobileBtn
              variant="secondary"
              fullWidth
              onClick={triggerEbookSources}
              disabled={scanning}
            >
              {scanning ? "Scanning…" : "Scan Ebooks"}
            </MobileBtn>
            <MobileBtn
              variant="secondary"
              fullWidth
              onClick={triggerAudiobookSources}
              disabled={scanning}
            >
              {scanning ? "Scanning…" : "Scan Audiobooks"}
            </MobileBtn>
            <MobileBtn
              variant="secondary"
              fullWidth
              onClick={triggerMam}
              disabled={mamScanning}
            >
              {mamScanning ? "MAM Scanning…" : "MAM Scan"}
            </MobileBtn>
            <MobileBtn
              variant="secondary"
              fullWidth
              onClick={() => setShowHygieneConfirm(true)}
              disabled={hygieneStarting || !!hygieneScan?.running}
            >
              {hygieneScan?.running ? "Hygiene…" : "Data Hygiene"}
            </MobileBtn>
            <MobileBtn
              variant="primary"
              fullWidth
              onClick={() => onNav("pipe-review")}
              primary
            >
              Review {reviewCount > 0 ? `(${reviewCount})` : ""}
            </MobileBtn>
          </div>
          {activeScans.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 4 }}>
              {libScans.map((s) => (
                <MobileScanProgress
                  key={`${s.kind}-${(s as ScanProgress & { slug?: string }).slug || ""}`}
                  scan={s}
                  label={(s as ScanProgress & { slug?: string }).slug ? `Library: ${(s as ScanProgress & { slug?: string }).slug}` : s.label}
                />
              ))}
              {lookupScan && lookupScan.running && (
                <MobileScanProgress
                  scan={lookupScan}
                  label="Sources Scan"
                  onCancel={cancelSources}
                />
              )}
              {mamScan && mamScan.running && (
                <MobileScanProgress
                  scan={mamScan}
                  label="MAM Scan"
                  onCancel={cancelMam}
                />
              )}
              {hygieneScan && hygieneScan.running && (
                <MobileScanProgress
                  scan={hygieneScan}
                  label="Data Hygiene"
                  onCancel={cancelHygiene}
                />
              )}
            </div>
          )}
        </div>
      </MobileSection>

      {/* ─── Hermes: MAM + budget + recent activity ────────── */}
      <MobileSection title="Hermes" subtitle="Pipeline detail" defaultOpen={true}>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {mam?.username && (
            <MobileMamAccount
              mam={mam}
              onClick={() => onNav("pipe-mam")}
            />
          )}
          {budget && <MobileSnatchBudget budget={budget} />}
          <div>
            <div
              style={{
                fontSize: 12,
                color: t.tg,
                fontWeight: 600,
                textTransform: "uppercase",
                letterSpacing: "0.04em",
                marginBottom: 6,
              }}
            >
              Recent Activity
            </div>
            <MobileRecentActivity grabs={grabs} max={5} />
          </div>
        </div>
      </MobileSection>

      {/* ─── Discovery quick actions ───────────────────────── */}
      <MobileSection title="Discovery" defaultOpen={true}>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <MobileRow
            title="Library"
            leadingIcon="📖"
            onClick={() => onNav("disc-library")}
          />
          <MobileRow
            title="Authors"
            leadingIcon="◉"
            onClick={() => onNav("disc-authors")}
          />
          <MobileRow
            title="Missing"
            leadingIcon="◌"
            onClick={() => onNav("disc-missing")}
          />
          <MobileRow
            title="Upcoming"
            leadingIcon="📅"
            onClick={() => onNav("disc-upcoming")}
          />
          <MobileRow
            title="MAM Search"
            leadingIcon="🔍"
            onClick={() => onNav("disc-mam")}
          />
          <MobileRow
            title="Metadata"
            leadingIcon="💡"
            onClick={() => onNav("disc-metadata")}
          />
          <MobileRow
            title="Works"
            leadingIcon="🔗"
            onClick={() => onNav("disc-works")}
          />
          <MobileRow
            title="Hidden"
            leadingIcon="🚫"
            onClick={() => onNav("disc-hidden")}
          />
        </div>
      </MobileSection>

      {/* ─── Pipeline quick actions ────────────────────────── */}
      <MobileSection title="Pipeline" defaultOpen={true}>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <MobileRow
            title="Review"
            subtitle={reviewCount > 0 ? `${reviewCount} pending` : undefined}
            leadingIcon="📚"
            onClick={() => onNav("pipe-review")}
          />
          <MobileRow
            title="New Authors"
            subtitle={tentativeCount > 0 ? `${tentativeCount} pending` : undefined}
            leadingIcon="🔎"
            onClick={() => onNav("pipe-tentative")}
          />
          <MobileRow
            title="Weekly Ignored"
            leadingIcon="📊"
            onClick={() => onNav("pipe-ignored")}
          />
          <MobileRow
            title="Author Lists"
            leadingIcon="👤"
            onClick={() => onNav("pipe-authors")}
          />
          <MobileRow
            title="Filters"
            leadingIcon="🎯"
            onClick={() => onNav("filters")}
          />
          <MobileRow
            title="Delayed"
            leadingIcon="⏳"
            onClick={() => onNav("pipe-delayed")}
          />
          <MobileRow
            title="Grab from MAM"
            subtitle="Paste a MAM link"
            leadingIcon="⬇"
            onClick={() => onNav("pipe-manual-grab")}
          />
        </div>
      </MobileSection>

      {/* ─── Tools ─────────────────────────────────────────── */}
      <MobileSection title="Tools" defaultOpen={false}>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {calibreWebUrl && (
            <MobileRow
              title="Calibre-Web"
              leadingIcon="🌐"
              onClick={() => window.open(calibreWebUrl, "_blank", "noopener")}
            />
          )}
          {absWebUrl && (
            <MobileRow
              title="Audiobookshelf"
              leadingIcon="🎧"
              onClick={() => window.open(absWebUrl, "_blank", "noopener")}
            />
          )}
          <MobileRow
            title="Import / Export"
            leadingIcon="📦"
            onClick={() => onNav("disc-importexport")}
          />
          <MobileRow
            title="MAM Status"
            leadingIcon="📡"
            onClick={() => onNav("pipe-mam")}
          />
          <MobileRow
            title="Logs"
            leadingIcon="📋"
            onClick={() => onNav("logs")}
          />
          <MobileRow
            title="Database"
            leadingIcon="🗄️"
            onClick={() => onNav("database")}
          />
          <MobileRow
            title="Settings"
            leadingIcon="⚙️"
            onClick={() => onNav("settings")}
          />
        </div>
      </MobileSection>

      {/* ─── Stats grid ────────────────────────────────────── */}
      <MobileSection title="Stats" defaultOpen={true}>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: 8,
          }}
        >
          <MobileStatTile
            label="Owned"
            value={fmtNum(ebookStats.owned_books ?? 0)}
            color={t.jade}
            onClick={() => onNav("disc-library")}
          />
          <MobileStatTile
            label="Missing"
            value={fmtNum(ebookStats.missing_books ?? 0)}
            color={t.red}
            onClick={() => onNav("disc-missing")}
          />
          <MobileStatTile
            label="New"
            value={fmtNum(ebookStats.new_books ?? 0)}
            color={t.cyan}
            onClick={() => onNav("disc-library")}
          />
          <MobileStatTile
            label="Upcoming"
            value={fmtNum(ebookStats.upcoming_books ?? 0)}
            color={t.pur}
            onClick={() => onNav("disc-upcoming")}
          />
          <MobileStatTile
            label="Authors"
            value={fmtNum(ebookStats.authors ?? 0)}
            onClick={() => onNav("disc-authors")}
          />
          <MobileStatTile
            label="Series"
            value={fmtNum(ebookStats.total_series ?? 0)}
          />
          {audiobookStats && (
            <>
              <MobileStatTile
                label="🎧 Owned"
                value={fmtNum(audiobookStats.owned_books ?? 0)}
                color={t.cyan}
              />
              <MobileStatTile
                label="🎧 Hours"
                value={fmtNum(Math.round((audiobookStats.total_duration_sec ?? 0) / 3600))}
                color={t.cyan}
              />
            </>
          )}
          <MobileStatTile
            label="To Review"
            value={fmtNum(reviewCount)}
            color={reviewCount > 0 ? t.accent : undefined}
            highlight={reviewCount > 0}
            onClick={() => onNav("pipe-review")}
          />
          <MobileStatTile
            label="New Authors"
            value={fmtNum(tentativeCount)}
            color={tentativeCount > 0 ? t.accent : undefined}
            highlight={tentativeCount > 0}
            onClick={() => onNav("pipe-tentative")}
          />
          <MobileStatTile
            label="Allowed"
            value={fmtNum(allowed)}
          />
          <MobileStatTile
            label="Ignored"
            value={fmtNum(ignored)}
          />
          <MobileStatTile
            label="To Calibre"
            value={fmtNum(calibreAdds)}
          />
          <MobileStatTile
            label="Total Grabs"
            value={fmtNum(totalGrabs)}
          />
        </div>
      </MobileSection>
      {showHygieneConfirm && (
        <MobileHygieneConfirm
          starting={hygieneStarting}
          onConfirm={triggerHygiene}
          onCancel={() => setShowHygieneConfirm(false)}
        />
      )}
    </div>
  );
}

const MOBILE_HYGIENE_JOBS = [
  "Empty author + series cleanup",
  "Hardcover identifier backfill",
  "Phase-2 author goodreads_id backfill",
  "Book deduplication",
  "Series consolidation",
  "ABS author cross-stamp",
  "Orphan author retrolink",
  "Cross-library person backfill",
  "Consolidate persons by shared source ID",
  "Prune orphan author_links",
  "Image URL health check",
  "Soft-delete retention sweep",
  "Non-roster author cleanup",
];

interface MobileHygieneConfirmProps {
  starting: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

function MobileHygieneConfirm({
  starting,
  onConfirm,
  onCancel,
}: MobileHygieneConfirmProps) {
  const t = useTheme();
  return (
    <div
      onClick={onCancel}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.6)",
        display: "flex",
        alignItems: "flex-end",
        justifyContent: "center",
        zIndex: 200,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "100%",
          maxHeight: "85vh",
          overflowY: "auto",
          background: t.bg2,
          borderTop: `1px solid ${t.border}`,
          borderTopLeftRadius: 12,
          borderTopRightRadius: 12,
          padding: 16,
          color: t.text,
        }}
      >
        <div style={{ fontSize: 16, fontWeight: 700, marginBottom: 6 }}>
          Run Data Hygiene?
        </div>
        <div style={{ fontSize: 12, color: t.text2, marginBottom: 10 }}>
          Fans 11 jobs across every library, in order. Re-running is idempotent.
        </div>
        <ol style={{ paddingLeft: 22, margin: 0, marginBottom: 14 }}>
          {MOBILE_HYGIENE_JOBS.map((name) => (
            <li
              key={name}
              style={{ fontSize: 12, color: t.text2, marginBottom: 4 }}
            >
              {name}
            </li>
          ))}
        </ol>
        <div style={{ display: "flex", gap: 8 }}>
          <MobileBtn variant="secondary" fullWidth onClick={onCancel}>
            Cancel
          </MobileBtn>
          <MobileBtn
            variant="primary"
            fullWidth
            onClick={onConfirm}
            disabled={starting}
            primary
          >
            {starting ? "Starting…" : "Run Hygiene"}
          </MobileBtn>
        </div>
      </div>
    </div>
  );
}
