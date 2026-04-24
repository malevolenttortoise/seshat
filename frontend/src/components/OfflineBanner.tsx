// Thin banner shown at top of the app when the device is offline.
//
// Purely informational — explains WHY clicks aren't doing what the
// user expects. Browse / read flows keep working from the service
// worker cache; write + MAM-requiring actions throw errors. The
// banner reframes those errors as "expected, you're offline" instead
// of "something broke".
//
// Renders nothing when online, so it has zero cost on the happy path.
import { useTheme } from "../theme";
import { useNetworkStatus } from "../hooks/useNetworkStatus";

export function OfflineBanner() {
  const t = useTheme();
  const { isOnline } = useNetworkStatus();
  if (isOnline) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        position: "sticky",
        top: 0,
        zIndex: 40,
        padding: "6px 14px",
        background: t.red + "22",
        color: t.redt,
        borderBottom: `1px solid ${t.red}44`,
        fontSize: 13,
        fontWeight: 600,
        textAlign: "center",
        letterSpacing: "0.02em",
      }}
    >
      Offline — browse works from cache, but scans / MAM / edits are
      paused until the connection returns.
    </div>
  );
}
