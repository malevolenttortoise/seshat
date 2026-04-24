// Network-connectivity status hook.
//
// `navigator.onLine` is best-effort — browsers return `true` whenever
// a network interface is up, which isn't the same as "the backend is
// reachable". For server-reachability, the SSE `client-status` event
// is the source of truth (SseEventsProvider exposes `clientReachable`).
// This hook handles the coarser case: device has NO network interface
// at all (airplane mode, wifi off, cellular killed). Useful for the
// global offline banner + gating action buttons that would definitely
// fail without any connection.
import { useEffect, useState } from "react";

export interface NetworkStatus {
  isOnline: boolean;
  // Monotonic counter that bumps on every status change — lets
  // effects depend on the hook without memoizing the whole object.
  changeCount: number;
}

export function useNetworkStatus(): NetworkStatus {
  const [state, setState] = useState<NetworkStatus>(() => ({
    isOnline:
      typeof navigator !== "undefined" ? navigator.onLine !== false : true,
    changeCount: 0,
  }));

  useEffect(() => {
    if (typeof window === "undefined") return;

    const handleOnline = () => {
      setState((s) => ({ isOnline: true, changeCount: s.changeCount + 1 }));
    };
    const handleOffline = () => {
      setState((s) => ({ isOnline: false, changeCount: s.changeCount + 1 }));
    };

    window.addEventListener("online", handleOnline);
    window.addEventListener("offline", handleOffline);
    return () => {
      window.removeEventListener("online", handleOnline);
      window.removeEventListener("offline", handleOffline);
    };
  }, []);

  return state;
}
