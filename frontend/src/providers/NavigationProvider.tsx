// Navigation context — exposes the App-level `nav()` function so
// in-page UI (the mobile back button, future deep-link triggers,
// etc.) can navigate without prop drilling.
//
// App.tsx wraps the page tree in <NavigationProvider value={{ nav }}>.
// A no-op default lets components import the hook safely outside the
// provider (e.g. test renders).
import { createContext, useContext, type ReactNode } from "react";

export type NavFn = (page: string, arg?: string | number | null) => void;

export interface NavigationApi {
  nav: NavFn;
}

const NavCtx = createContext<NavigationApi>({
  nav: () => {},
});

export function NavigationProvider({
  value,
  children,
}: {
  value: NavigationApi;
  children: ReactNode;
}) {
  return <NavCtx.Provider value={value}>{children}</NavCtx.Provider>;
}

export function useNavigation(): NavigationApi {
  return useContext(NavCtx);
}
