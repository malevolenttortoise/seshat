// Seshat — unified book discovery + acquisition platform.
//
// Two-section navigation:
//   Discovery: Library, Authors, Missing, Upcoming, MAM Search, Suggestions
//   Pipeline:  Review, New Authors, Weekly Ignored, Author Lists, Delayed
//   Shared:    Dashboard, Settings, Logs, Database, Filters
import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { ThemeProvider, useTheme, useThemeControls } from "./theme";
import type { Theme, NavFn } from "./types";
import { Spin } from "./components/Spin";
import { ErrorBoundary } from "./components/ErrorBoundary";
import Toaster from "./components/Toaster";
import { OfflineBanner } from "./components/OfflineBanner";
import { LibrarySyncBanner } from "./components/LibrarySyncBanner";
import { InstallPrompt } from "./components/InstallPrompt";
import { JumpToTop } from "./components/JumpToTop";
import { GlobalSearchBar, type SearchNavTarget } from "./components/GlobalSearchBar";
import { GlobalMetadataCacheStatusIcon } from "./components/GlobalMetadataCacheStatusIcon";
import { MobileNavDrawer, type NavItem } from "./components/MobileNavDrawer";
import { SseEventsProvider } from "./providers/SseEventsProvider";
import { useViewport } from "./hooks/useViewport";

// Pipeline pages
import LoginPage from "./pages/LoginPage";
import PipelineDashboard from "./pages/Dashboard";
import ReviewPage from "./pages/ReviewPage";
import TentativePage from "./pages/TentativePage";
import IgnoredWeeklyPage from "./pages/IgnoredWeeklyPage";
import PipelineAuthorsPage from "./pages/AuthorsPage";
import DelayedPage from "./pages/DelayedPage";
import FiltersPage from "./pages/FiltersPage";
import MigrationPage from "./pages/MigrationPage";
import PipelineMamPage from "./pages/MamPage";
import DatabasePage from "./pages/DatabasePage";
import AuthorTriagePage from "./pages/AuthorTriagePage";
import PersonsManagerPage from "./pages/PersonsManagerPage";
import LogsPage from "./pages/LogsPage";
import SettingsPage from "./pages/SettingsPage";

// Unified + Discovery pages
import UnifiedDashboard from "./pages/UnifiedDashboard";
import DiscDashboard from "./pages/DiscDashboard";
import { SetupWizard } from "./components/SetupWizard";
import DiscBooksPage from "./pages/DiscBooksPage";
import DiscAuthorsPage from "./pages/DiscAuthorsPage";
import DiscAuthorDetailPage from "./pages/DiscAuthorDetailPage";
import DiscMAMPage from "./pages/DiscMAMPage";
import DiscMetadataPage from "./pages/DiscMetadataPage";
import DiscSeriesPage from "./pages/DiscSeriesPage";
import DiscImportExportPage from "./pages/DiscImportExportPage";
import WorksPage from "./pages/WorksPage";
import { NavigationProvider } from "./providers/NavigationProvider";

interface AuthState {
  loading: boolean;
  authenticated: boolean;
  firstRun: boolean;
  username?: string;
}

type Section = "discovery" | "pipeline";

// ─── Navigation definitions ─────────────────────────────────

// v2.20.0 — Discovery nav collapses Works / Metadata / Series /
// Author Triage under a single "Tools" parent dropdown. The bare
// nav was overflowing into the global search bar at typical 1800px
// widths; grouping the four catalog-maintenance pages frees room
// while still keeping them one click + hover away.
const DISCOVERY_NAV = [
  { id: "disc-library",     label: "Library",     icon: "📖" },
  { id: "disc-authors",     label: "Authors",     icon: "◉" },
  { id: "disc-missing",     label: "Missing",     icon: "◌" },
  { id: "disc-upcoming",    label: "Upcoming",    icon: "📅" },
  { id: "disc-mam",         label: "MAM Search",  icon: "🔍" },
  { id: "disc-hidden",      label: "Hidden",      icon: "🚫" },
  {
    id: "disc-tools",
    label: "Tools",
    icon: "🛠",
    children: [
      { id: "disc-works",    label: "Works",          icon: "🔗" },
      { id: "disc-metadata", label: "Metadata",       icon: "📋" },
      { id: "disc-series",   label: "Series",         icon: "🗂️" },
      { id: "author-triage", label: "Author Triage",  icon: "⚙️" },
      { id: "persons-manager", label: "Persons & IDs", icon: "🪪" },
    ],
  },
];

const PIPELINE_NAV = [
  { id: "pipe-review",      label: "Review",        icon: "📚" },
  { id: "pipe-tentative",   label: "New Authors",   icon: "🔎" },
  { id: "pipe-ignored",     label: "Weekly Ignored", icon: "📊" },
  { id: "pipe-authors",     label: "Author Lists",  icon: "👤" },
  { id: "pipe-delayed",     label: "Delayed",       icon: "⏳" },
  { id: "filters",          label: "Filters",       icon: "🎯" },
];

const WIDE_PAGES = new Set([
  "dashboard", "disc-dashboard", "pipe-dashboard",
  "disc-library", "disc-authors", "disc-author-detail",
  "disc-missing", "disc-upcoming", "disc-mam", "disc-metadata",
  "disc-series", "disc-hidden", "disc-importexport", "disc-works",
  "author-triage", "persons-manager",
  "pipe-review", "pipe-tentative", "pipe-ignored", "pipe-authors",
  "pipe-delayed", "pipe-migration",
  "logs", "database",
]);

function loadSavedPage(): string {
  try { return localStorage.getItem("seshat_page") || "dashboard"; }
  catch { return "dashboard"; }
}

function loadSavedPageArg(): string | number | null {
  // Persist the page arg alongside `seshat_page` so F5 on a detail
  // page (e.g. disc-author-detail) rehydrates with the right id or
  // "slug:id" compound string. Without this the detail page boots
  // with authorId=null and spins forever on the initial fetch.
  // We store as string + a flag so numeric IDs round-trip cleanly.
  try {
    const raw = localStorage.getItem("seshat_page_arg");
    if (raw === null || raw === "") return null;
    const n = Number(raw);
    return Number.isFinite(n) && String(n) === raw ? n : raw;
  } catch { return null; }
}

function loadSavedSection(): Section {
  try {
    const s = localStorage.getItem("seshat_section");
    if (s === "discovery" || s === "pipeline") return s;
  } catch { /* */ }
  return "discovery";
}

// ─── Page rendering ─────────────────────────────────────────

function renderPage(
  page: string,
  pageArg: string | number | null,
  nav: (p: string, arg?: string | number | null) => void,
) {
  switch (page) {
    // Dashboard
    case "dashboard":          return <UnifiedDashboard onNav={nav} />;
    case "disc-dashboard":     return <DiscDashboard onNav={nav} />;
    case "pipe-dashboard":     return <PipelineDashboard onNav={nav} />;

    // Discovery pages (use onNav prop + useTheme context)
    case "disc-library":       return <DiscBooksPage title="Library" apiPath="/discovery/books" extraParams={{owned: "1"}} />;
    case "disc-missing":       return <DiscBooksPage title="Missing" apiPath="/discovery/missing" />;
    case "disc-upcoming":      return <DiscBooksPage title="Upcoming" apiPath="/discovery/upcoming" />;
    case "disc-authors":       return <DiscAuthorsPage onNav={nav} />;
    case "disc-author-detail": return <DiscAuthorDetailPage authorId={pageArg as number} onNav={nav} />;
    case "disc-mam":           return <DiscMAMPage onNav={nav} />;
    case "disc-metadata":      return <DiscMetadataPage />;
    case "disc-series":        return <DiscSeriesPage />;
    case "disc-hidden":        return <DiscBooksPage title="Hidden Books" apiPath="/discovery/books/hidden" showOwnedFilter />;
    case "disc-importexport":  return <DiscImportExportPage />;
    case "disc-works":         return <WorksPage />;

    // Pipeline pages (no props — use useTheme context)
    case "pipe-review":        return <ReviewPage />;
    case "pipe-tentative":     return <TentativePage />;
    case "pipe-ignored":       return <IgnoredWeeklyPage />;
    case "pipe-authors":       return <PipelineAuthorsPage />;
    case "pipe-delayed":       return <DelayedPage />;
    case "pipe-migration":     return <MigrationPage />;
    case "pipe-mam":           return <PipelineMamPage />;

    // Shared pages (no props)
    case "filters":            return <FiltersPage />;
    case "settings":           return <SettingsPage />;
    case "logs":               return <LogsPage />;
    case "database":           return <DatabasePage />;
    case "author-triage":      return <AuthorTriagePage onNav={nav} />;
    case "persons-manager":    return <PersonsManagerPage onNav={nav} />;

    default:                   return <PipelineDashboard onNav={nav} />;
  }
}

// ─── Main App ───────────────────────────────────────────────

function SeshatApp() {
  const t = useTheme();
  const { cycle, themeName } = useThemeControls();
  const vp = useViewport();
  // Any touch-class viewport gets the hamburger nav. Width alone
  // misclassified iPads (an iPad Pro 12.9" landscape is 1366px and
  // would otherwise be "desktop"); the pointer:coarse media query
  // covers every iPad/iPhone regardless of orientation while still
  // letting a mouse-pointer 1024-1366px laptop use the desktop nav.
  const isMobile = vp.isMobile || vp.isTablet || vp.isTouch;
  const [navOpen, setNavOpen] = useState(false);

  const [auth, setAuth] = useState<AuthState>({ loading: true, authenticated: false, firstRun: false });
  // Mobile-only — toggles the fullscreen GlobalSearchBar overlay.
  const [mobileSearchOpen, setMobileSearchOpen] = useState(false);
  // Library-level first-run gate — orthogonal to auth.firstRun (which
  // only covers "no admin account exists yet"). A user who finishes
  // the account-create flow but then lands on an empty library /
  // settings.json gets the SetupWizard next. `null` = not yet
  // fetched; true/false = known.
  const [setupNeeded, setSetupNeeded] = useState<boolean | null>(null);
  const [page, setPage] = useState(loadSavedPage);
  const [pageArg, setPageArg] = useState<string | number | null>(loadSavedPageArg);
  const [section, setSection] = useState<Section>(loadSavedSection);

  const nav = (p: string, arg?: string | number | null) => {
    setPage(p);
    const resolvedArg = arg ?? null;
    setPageArg(resolvedArg);
    try {
      localStorage.setItem("seshat_page", p);
      if (resolvedArg === null || resolvedArg === undefined) {
        localStorage.removeItem("seshat_page_arg");
      } else {
        localStorage.setItem("seshat_page_arg", String(resolvedArg));
      }
    } catch { /* */ }
    window.scrollTo(0, 0);
  };

  const switchSection = (s: Section) => {
    setSection(s);
    try { localStorage.setItem("seshat_section", s); } catch { /* */ }
  };

  // v2.15.0 #B — turn a GlobalSearchBar result selection into the
  // right nav() call. Settings + Series + Book results dispatch
  // a `seshat:focus` window event after navigation so the target
  // page can scroll-to / open the matching element.
  const navFromSearch = (target: SearchNavTarget) => {
    switch (target.kind) {
      case "page":
        if (target.section) setSection(target.section);
        nav(target.page_id);
        return;
      case "settings-section":
        nav("settings");
        setTimeout(() => window.dispatchEvent(new CustomEvent("seshat:focus", {
          detail: { kind: "settings-section", section_id: target.section_id },
        })), 50);
        return;
      case "author":
        // Existing pattern — Author Detail reads pageArg as the
        // numeric author id. library_slug is currently not consumed
        // by nav; the page resolves it via the author's stamped slug.
        nav("disc-author-detail", target.author_id);
        return;
      case "series":
        nav("disc-series");
        setTimeout(() => window.dispatchEvent(new CustomEvent("seshat:focus", {
          detail: {
            kind: "series",
            series_id: target.series_id,
            name: target.name,
            library_slug: target.library_slug,
          },
        })), 50);
        return;
      case "book":
        // No single-book page exists yet. Land on the library page
        // (owned books) and dispatch a focus event; DiscBooksPage
        // can open BookSidebar for that ID if it chooses to wire
        // a listener. For v2.15.0 the navigation alone is the
        // baseline behavior; deep-link is a polish follow-up.
        nav("disc-library");
        setTimeout(() => window.dispatchEvent(new CustomEvent("seshat:focus", {
          detail: { kind: "book", book_id: target.book_id, library_slug: target.library_slug },
        })), 50);
        return;
    }
  };

  // Auth check
  useEffect(() => {
    const check = async () => {
      try {
        const r = await api.get<{ authenticated: boolean; first_run: boolean; username?: string }>("/auth/check");
        setAuth({ loading: false, authenticated: r.authenticated, firstRun: r.first_run, username: r.username });
      } catch {
        setAuth({ loading: false, authenticated: false, firstRun: false });
      }
    };
    check();
    const onAuthRequired = () => setAuth(a => ({ ...a, authenticated: false }));
    window.addEventListener("seshat:auth-required", onAuthRequired);
    return () => window.removeEventListener("seshat:auth-required", onAuthRequired);
  }, []);

  // Library-setup check — runs once the user is authenticated. The
  // /discovery/platform endpoint's `first_run` composes "no libraries
  // discovered AND no user-configured sources AND setup not completed"
  // into a single bool, which is exactly the gate we want. On any
  // fetch error fall open (setupNeeded=false) — a broken /platform
  // shouldn't lock the user out of the app.
  useEffect(() => {
    if (!auth.authenticated) return;
    if (setupNeeded !== null) return;
    api
      .get<{ first_run?: boolean }>("/discovery/platform")
      .then(p => setSetupNeeded(!!p.first_run))
      .catch(() => setSetupNeeded(false));
  }, [auth.authenticated, setupNeeded]);

  if (auth.loading) {
    return (
      <div style={{ display: "flex", justifyContent: "center", alignItems: "center", height: "100vh", background: t.bg }}>
        <Spin size={28} />
      </div>
    );
  }

  if (!auth.authenticated) {
    return <LoginPage onLoginSuccess={() => setAuth(a => ({ ...a, authenticated: true, loading: false }))} firstRun={auth.firstRun} />;
  }

  // Library-level first-run wizard. Only shown when /discovery/platform
  // reports no libraries + no configured sources + setup_complete=false.
  // `onComplete` flips the gate to false so subsequent renders go
  // straight to the main app without a re-fetch.
  if (setupNeeded === null) {
    return (
      <div style={{ display: "flex", justifyContent: "center", alignItems: "center", height: "100vh", background: t.bg }}>
        <Spin size={28} />
      </div>
    );
  }
  if (setupNeeded) {
    return <SetupWizard onComplete={() => setSetupNeeded(false)} />;
  }

  const themeIcon = themeName === "dark" ? "🌙" : themeName === "dim" ? "⛅" : "☀️";
  const activeNav = section === "discovery" ? DISCOVERY_NAV : PIPELINE_NAV;
  const maxW = 1800;

  // Right-rail tool icons. Defined once and reused — desktop nav
  // renders them as a horizontal icon strip, mobile drawer renders
  // them as a vertical labeled list, same source of truth.
  const RIGHT_ICONS = [
    { id: "disc-importexport", icon: "📦", label: "Import / Export" },
    { id: "pipe-mam",          icon: "📡", label: "MAM Status" },
    { id: "logs",              icon: "📋", label: "Logs" },
    { id: "database",          icon: "🗄️", label: "Database" },
    { id: "settings",          icon: "⚙️", label: "Settings" },
  ];

  const handleLogout = async () => {
    if (!confirm("Sign out of Seshat?")) return;
    try { await api.post("/auth/logout", {}); } catch { /* */ }
    setAuth({ loading: false, authenticated: false, firstRun: false });
  };

  return (
    <div style={{ minHeight: "100vh", background: t.bg, color: t.text }}>
      {/* ─── Navbar — desktop horizontal, mobile compact + drawer ─── */}
      {/* paddingTop: env(safe-area-inset-top) adds the iOS PWA notch
          inset so the navbar doesn't tuck under the status bar in
          standalone mode. Falls back to 0 outside iOS standalone. */}
      <nav style={{
        background: t.bg2,
        borderBottom: `1px solid ${t.border}`,
        display: "flex",
        alignItems: "center",
        padding: isMobile ? "0 12px" : "0 80px",
        paddingTop: "env(safe-area-inset-top, 0px)",
        height: `calc(52px + env(safe-area-inset-top, 0px))`,
        position: "sticky",
        top: 0,
        zIndex: 100,
        gap: isMobile ? 10 : 0,
      }}>
        {/* Logo / Dashboard */}
        <div
          onClick={() => nav("dashboard")}
          style={{
            cursor: "pointer",
            fontWeight: 800,
            fontSize: isMobile ? 18 : 20,
            color: t.accent,
            letterSpacing: "0.02em",
            marginRight: isMobile ? 0 : 20,
            userSelect: "none",
            display: "flex",
            alignItems: "center",
            gap: 8,
            flex: isMobile ? 1 : "0 0 auto",
          }}
        >
          <img src="/icon.svg" alt="" style={{ width: isMobile ? 28 : 32, height: isMobile ? 28 : 32 }} />
          Seshat
        </div>

        {isMobile ? (
          // Mobile: search icon + hamburger. Search opens a fullscreen
          // overlay (mounted at the bottom of the SeshatApp tree);
          // the nav drawer is a separate overlay also mounted at
          // the bottom.
          <>
            <button
              onClick={() => setMobileSearchOpen(true)}
              aria-label="Open search"
              style={{
                background: "transparent",
                border: "none",
                cursor: "pointer",
                padding: "8px 10px",
                fontSize: 18,
                color: t.text2,
                lineHeight: 1,
                display: "flex",
                alignItems: "center",
              }}
            >
              🔍
            </button>
            <button
              onClick={() => setNavOpen(true)}
              aria-label="Open navigation"
              style={{
                background: "transparent",
                border: "none",
                cursor: "pointer",
                padding: "8px 10px",
                fontSize: 22,
                color: t.text2,
                lineHeight: 1,
              }}
            >
              ☰
            </button>
          </>
        ) : (
          <>
            {/* Section switcher */}
            <div style={{ display: "flex", gap: 2, marginRight: 16 }}>
              {(["discovery", "pipeline"] as Section[]).map(s => (
                <button
                  key={s}
                  onClick={() => switchSection(s)}
                  style={{
                    background: section === s ? t.abg : "transparent",
                    color: section === s ? t.accent : t.td,
                    border: `1px solid ${section === s ? t.abr : "transparent"}`,
                    borderRadius: 6,
                    padding: "5px 14px",
                    fontSize: 15,
                    fontWeight: 600,
                    cursor: "pointer",
                    textTransform: "capitalize",
                  }}
                >
                  {s}
                </button>
              ))}
            </div>

            {/* Section nav items */}
            <div style={{ display: "flex", gap: 4, flex: 1, minWidth: 0 }}>
              {activeNav.map(item => (
                <NavButton
                  key={item.id}
                  item={item}
                  page={page}
                  nav={nav}
                  t={t}
                />
              ))}
            </div>

            {/* v2.15.0 #B — global search bar between section nav
               items and the right-rail tool icons. Sits in the
               navbar's flex row so it stays available on every page. */}
            <div style={{ marginRight: 10 }}>
              <GlobalSearchBar onNavigate={navFromSearch} />
            </div>

            {/* Right icons */}
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              {/* v2.21.0 Phase F tier 1 — global cache worker
                 status indicator. Click navigates to Settings →
                 Metadata Sources → Amazon cache card. */}
              <GlobalMetadataCacheStatusIcon onClick={() => nav("settings")} />
              {RIGHT_ICONS.map(btn => (
                <button
                  key={btn.id}
                  onClick={() => nav(btn.id)}
                  title={btn.label}
                  style={{
                    background: page === btn.id ? t.abg : "transparent",
                    border: "none",
                    cursor: "pointer",
                    fontSize: 18,
                    padding: "4px 8px",
                    borderRadius: 4,
                    opacity: page === btn.id ? 1 : 0.7,
                  }}
                >
                  {btn.icon}
                </button>
              ))}
              <button
                onClick={cycle}
                title={`Theme: ${themeName}`}
                style={{ background: "transparent", border: "none", cursor: "pointer", fontSize: 18, padding: "4px 6px" }}
              >
                {themeIcon}
              </button>
              <button
                onClick={handleLogout}
                title="Sign out"
                style={{
                  background: "transparent",
                  border: "none",
                  cursor: "pointer",
                  fontSize: 15,
                  color: t.td,
                  padding: "4px 8px",
                }}
              >
                ⏻
              </button>
            </div>
          </>
        )}
      </nav>

      {/* Library sync banner — sticky strip under the navbar while
          startup sync runs, or full-screen splash on the very first
          boot. Self-clearing once everything settles. */}
      <LibrarySyncBanner />

      {/* ─── Page content ───────────────────────────────────── */}
      <main
        className="seshat-main"
        style={{ maxWidth: maxW, margin: "0 auto", padding: "24px 16px" }}
      >
        <NavigationProvider value={{ nav }}>
          <ErrorBoundary>
            {renderPage(page, pageArg, nav)}
          </ErrorBoundary>
        </NavigationProvider>
      </main>

      {/* Mobile nav drawer — only mounted when isMobile flips true.
          The MobileNavDrawer guards on `open` itself so this stays
          cheap to keep mounted; we still gate on isMobile to avoid
          firing the body-scroll-lock effect on desktop. */}
      {isMobile ? (
        <MobileNavDrawer
          open={navOpen}
          onClose={() => setNavOpen(false)}
          section={section}
          onSectionChange={switchSection}
          activePage={page}
          navItems={activeNav}
          rightIcons={RIGHT_ICONS}
          onNavigate={nav}
          themeIcon={themeIcon}
          themeName={themeName}
          onCycleTheme={cycle}
          onLogout={handleLogout}
        />
      ) : null}

      {/* v2.15.0 #B — mobile fullscreen search overlay. Sits above
         the page content and the nav drawer (the drawer uses z-index
         201, the search uses 220 so it covers a drawer that's
         already open). Closes on result-tap, on Escape, or via the
         backdrop tap. */}
      {isMobile && mobileSearchOpen ? (
        <div
          onClick={(e) => {
            if (e.target === e.currentTarget) setMobileSearchOpen(false);
          }}
          style={{
            position: "fixed", inset: 0, zIndex: 220,
            background: t.bg + "f0", backdropFilter: "blur(6px)",
            paddingTop: "calc(48px + env(safe-area-inset-top, 0px))",
            paddingLeft: 12, paddingRight: 12,
            display: "flex", flexDirection: "column", gap: 8,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <div style={{ flex: 1 }}>
              <GlobalSearchBar
                onNavigate={(target) => {
                  setMobileSearchOpen(false);
                  navFromSearch(target);
                }}
                autoFocus
                fullWidth
              />
            </div>
            <button
              onClick={() => setMobileSearchOpen(false)}
              aria-label="Close search"
              style={{
                background: "transparent", border: "none",
                color: t.text2, fontSize: 18, padding: 8,
                cursor: "pointer", lineHeight: 1,
              }}
            >
              ✕
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

// v2.20.0 — desktop nav button. Items WITHOUT children render as a
// direct link; items WITH children render as a dropdown trigger that
// toggles a menu of children. The dropdown closes on click-outside,
// Esc, or selecting a child. Parent is highlighted when the current
// page is any of its children.
function NavButton({
  item, page, nav, t,
}: {
  item: NavItem;
  page: string;
  nav: NavFn;
  t: Theme;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const hasChildren = !!item.children?.length;
  const isActive = hasChildren
    ? !!item.children?.some((c) => c.id === page)
    : page === item.id;

  // Click-outside + Esc close.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const baseBtnStyle: React.CSSProperties = {
    background: isActive ? t.abg : "transparent",
    color: isActive ? t.accent : t.tm,
    border: "none",
    borderRadius: 6,
    padding: "6px 12px",
    fontSize: 14,
    fontWeight: isActive ? 600 : 400,
    cursor: "pointer",
    display: "flex",
    alignItems: "center",
    gap: 6,
  };

  if (!hasChildren) {
    return (
      <button onClick={() => nav(item.id)} style={baseBtnStyle}>
        <span>{item.icon}</span>
        <span>{item.label}</span>
      </button>
    );
  }
  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        onClick={() => setOpen((o) => !o)}
        style={baseBtnStyle}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <span>{item.icon}</span>
        <span>{item.label}</span>
        <span style={{ fontSize: 10, opacity: 0.7 }}>▾</span>
      </button>
      {open ? (
        <div
          role="menu"
          style={{
            position: "absolute",
            top: "100%",
            left: 0,
            marginTop: 4,
            background: t.bg2,
            border: `1px solid ${t.border}`,
            borderRadius: 8,
            boxShadow: "0 4px 16px rgba(0,0,0,0.3)",
            minWidth: 180,
            padding: 4,
            zIndex: 200,
          }}
        >
          {item.children!.map((child) => (
            <button
              key={child.id}
              role="menuitem"
              onClick={() => {
                nav(child.id);
                setOpen(false);
              }}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                width: "100%",
                padding: "8px 12px",
                background: page === child.id ? t.abg : "transparent",
                color: page === child.id ? t.accent : t.tm,
                border: "none",
                borderRadius: 4,
                fontSize: 14,
                fontWeight: page === child.id ? 600 : 400,
                textAlign: "left",
                cursor: "pointer",
              }}
            >
              <span style={{ width: 20, textAlign: "center" }}>{child.icon}</span>
              <span>{child.label}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}


export default function App() {
  return (
    <ThemeProvider>
      <SseEventsProvider>
        <OfflineBanner />
        <SeshatApp />
        <Toaster />
        <InstallPrompt />
        <JumpToTop />
      </SseEventsProvider>
    </ThemeProvider>
  );
}
