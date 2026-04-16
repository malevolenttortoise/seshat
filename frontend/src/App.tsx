// Seshat — unified book discovery + acquisition platform.
//
// Two-section navigation:
//   Discovery: Library, Authors, Missing, Upcoming, MAM Search, Suggestions
//   Pipeline:  Review, New Authors, Weekly Ignored, Author Lists, Delayed
//   Shared:    Dashboard, Settings, Logs, Database, Filters
import { useEffect, useState } from "react";
import { api } from "./api";
import { ThemeProvider, useTheme, useThemeControls } from "./theme";
import { Spin } from "./components/Spin";
import { ErrorBoundary } from "./components/ErrorBoundary";

// Pipeline pages (from Hermeece)
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
import LogsPage from "./pages/LogsPage";
import SettingsPage from "./pages/SettingsPage";

// Unified + Discovery pages
import UnifiedDashboard from "./pages/UnifiedDashboard";
import DiscDashboard from "./pages/DiscDashboard";
import DiscBooksPage from "./pages/DiscBooksPage";
import DiscAuthorsPage from "./pages/DiscAuthorsPage";
import DiscAuthorDetailPage from "./pages/DiscAuthorDetailPage";
import DiscMAMPage from "./pages/DiscMAMPage";
import DiscSuggestionsPage from "./pages/DiscSuggestionsPage";
import DiscHiddenPage from "./pages/DiscHiddenPage";
import DiscImportExportPage from "./pages/DiscImportExportPage";

interface AuthState {
  loading: boolean;
  authenticated: boolean;
  firstRun: boolean;
  username?: string;
}

type Section = "discovery" | "pipeline";

// ─── Navigation definitions ─────────────────────────────────

const DISCOVERY_NAV = [
  { id: "disc-library",     label: "Library",     icon: "📖" },
  { id: "disc-authors",     label: "Authors",     icon: "◉" },
  { id: "disc-missing",     label: "Missing",     icon: "◌" },
  { id: "disc-upcoming",    label: "Upcoming",    icon: "📅" },
  { id: "disc-mam",         label: "MAM Search",  icon: "🔍" },
  { id: "disc-suggestions", label: "Suggestions", icon: "💡" },
];

const PIPELINE_NAV = [
  { id: "pipe-review",      label: "Review",        icon: "📚" },
  { id: "pipe-tentative",   label: "New Authors",   icon: "🔎" },
  { id: "pipe-ignored",     label: "Weekly Ignored", icon: "📊" },
  { id: "pipe-authors",     label: "Author Lists",  icon: "👤" },
  { id: "pipe-delayed",     label: "Delayed",       icon: "⏳" },
];

const WIDE_PAGES = new Set([
  "dashboard", "disc-dashboard", "pipe-dashboard",
  "disc-library", "disc-authors", "disc-author-detail",
  "disc-missing", "disc-upcoming", "disc-mam", "disc-suggestions",
  "disc-hidden", "disc-importexport",
  "pipe-review", "pipe-tentative", "pipe-ignored", "pipe-authors",
  "pipe-delayed", "pipe-migration",
  "logs", "database",
]);

function loadSavedPage(): string {
  try { return localStorage.getItem("seshat_page") || "dashboard"; }
  catch { return "dashboard"; }
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
    case "disc-suggestions":   return <DiscSuggestionsPage onNav={nav} />;
    case "disc-hidden":        return <DiscHiddenPage onNav={nav} />;
    case "disc-importexport":  return <DiscImportExportPage />;

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

    default:                   return <PipelineDashboard onNav={nav} />;
  }
}

// ─── Main App ───────────────────────────────────────────────

function SeshatApp() {
  const t = useTheme();
  const { cycle, themeName } = useThemeControls();

  const [auth, setAuth] = useState<AuthState>({ loading: true, authenticated: false, firstRun: false });
  const [page, setPage] = useState(loadSavedPage);
  const [pageArg, setPageArg] = useState<string | number | null>(null);
  const [section, setSection] = useState<Section>(loadSavedSection);

  const nav = (p: string, arg?: string | number | null) => {
    setPage(p);
    setPageArg(arg ?? null);
    try { localStorage.setItem("seshat_page", p); } catch { /* */ }
    window.scrollTo(0, 0);
  };

  const switchSection = (s: Section) => {
    setSection(s);
    try { localStorage.setItem("seshat_section", s); } catch { /* */ }
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

  const themeIcon = themeName === "dark" ? "🌙" : themeName === "dim" ? "⛅" : "☀️";
  const activeNav = section === "discovery" ? DISCOVERY_NAV : PIPELINE_NAV;
  const maxW = page === "settings" ? 1800 : WIDE_PAGES.has(page) ? 1400 : 1120;

  return (
    <div style={{ minHeight: "100vh", background: t.bg, color: t.text }}>
      {/* ─── Navbar ─────────────────────────────────────────── */}
      <nav style={{
        background: t.bg2,
        borderBottom: `1px solid ${t.border}`,
        display: "flex",
        alignItems: "center",
        padding: "0 16px",
        height: 48,
        position: "sticky",
        top: 0,
        zIndex: 100,
      }}>
        {/* Logo / Dashboard */}
        <div
          onClick={() => nav("dashboard")}
          style={{
            cursor: "pointer",
            fontWeight: 800,
            fontSize: 18,
            color: t.accent,
            letterSpacing: "0.02em",
            marginRight: 20,
            userSelect: "none",
          }}
        >
          𓋹 Seshat
        </div>

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
                padding: "4px 12px",
                fontSize: 12,
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
        <div style={{ display: "flex", gap: 4, flex: 1 }}>
          {activeNav.map(item => (
            <button
              key={item.id}
              onClick={() => nav(item.id)}
              style={{
                background: page === item.id ? t.abg : "transparent",
                color: page === item.id ? t.accent : t.tm,
                border: "none",
                borderRadius: 6,
                padding: "6px 10px",
                fontSize: 13,
                fontWeight: page === item.id ? 600 : 400,
                cursor: "pointer",
                display: "flex",
                alignItems: "center",
                gap: 5,
              }}
            >
              <span>{item.icon}</span>
              <span>{item.label}</span>
            </button>
          ))}
        </div>

        {/* Right icons */}
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          {[
            { id: "filters", icon: "🎯", title: "Filters" },
            { id: "pipe-mam", icon: "📡", title: "MAM Status" },
            { id: "logs", icon: "📋", title: "Logs" },
            { id: "database", icon: "🗄️", title: "Database" },
            { id: "settings", icon: "⚙️", title: "Settings" },
          ].map(btn => (
            <button
              key={btn.id}
              onClick={() => nav(btn.id)}
              title={btn.title}
              style={{
                background: page === btn.id ? t.abg : "transparent",
                border: "none",
                cursor: "pointer",
                fontSize: 16,
                padding: "4px 6px",
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
            style={{ background: "transparent", border: "none", cursor: "pointer", fontSize: 16, padding: "4px 6px" }}
          >
            {themeIcon}
          </button>
          <button
            onClick={async () => {
              if (!confirm("Sign out of Seshat?")) return;
              try { await api.post("/auth/logout", {}); } catch { /* */ }
              setAuth({ loading: false, authenticated: false, firstRun: false });
            }}
            title="Sign out"
            style={{
              background: "transparent",
              border: "none",
              cursor: "pointer",
              fontSize: 13,
              color: t.td,
              padding: "4px 8px",
            }}
          >
            ⏻
          </button>
        </div>
      </nav>

      {/* ─── Page content ───────────────────────────────────── */}
      <main style={{ maxWidth: maxW, margin: "0 auto", padding: "24px 16px" }}>
        <ErrorBoundary>
          {renderPage(page, pageArg, nav)}
        </ErrorBoundary>
      </main>
    </div>
  );
}

export default function App() {
  return (
    <ThemeProvider>
      <SeshatApp />
    </ThemeProvider>
  );
}
