// Seshat theme — Egyptian goddess color scheme.
//
// Three brightness variants:
//   - dark:  deep indigo with gold accents
//   - dim:   warm charcoal with muted gold
//   - light: warm papyrus with rich bronze accents
//
// Color palette inspired by Egyptian aesthetics:
//   - Gold (#d4a357) — primary accent, highlights, links
//   - Deep indigo (#0a0b1a, #11132a) — dark mode surfaces
//   - Warm sand/papyrus (#f5f0e8) — light mode surfaces
//   - Jade green (#4daf8b) — tertiary accent, success states
//
// Usage:
//   import { useTheme } from "../theme";
//   const t = useTheme();
//   <div style={{color: t.text, background: t.bg2}} />
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export interface Theme {
  name: string;
  bg: string;   bg2: string;  bg3: string;  bg4: string;
  border: string; borderL: string; borderH: string;
  text: string; text2: string;
  // Extended text gradations (medium → dim → faint → ghost → invisible)
  tm: string; td: string; tf: string; tg: string; ti: string;
  // Gold accent
  accent: string; accentDim: string;
  abg: string;  // accent background tint
  abr: string;  // accent border tint
  // Jade green (tertiary)
  jade: string; jadeDim: string;
  // Semantic colors with tints + backgrounds
  grn: string; grnt: string; grnb: string;
  red: string; redt: string; redb: string;
  ylw: string; ylwt: string; ylwb: string;
  pur: string; purt: string; purb: string;
  cyan: string; cyant: string; cyanb: string;
  // Aliases for simple usage
  ok: string; warn: string; err: string;
  inp: string;
  // Backward-compat alias (pipeline pages use textDim)
  textDim: string;
}

export const THEMES: Record<string, Theme> = {
  dark: {
    name: "Dark",
    // Deep indigo backgrounds
    bg:  "#0a0b1a",
    bg2: "#11132a",
    bg3: "#0e1022",
    bg4: "#1a1c30",
    border:  "#2a2c4a",
    borderL: "#1a1c30",
    borderH: "#4a4c6a",
    // Cool-tinted text
    text:  "#e8e4f0",
    text2: "#c8c4d8",
    tm: "#a8a4c0", td: "#8884a0", tf: "#706c90",
    tg: "#585478", ti: "#404060",
    // Gold accent
    accent:    "#d4a357",
    accentDim: "#b08840",
    abg: "rgba(212,163,87,0.14)",
    abr: "rgba(212,163,87,0.30)",
    // Jade green
    jade:    "#4daf8b",
    jadeDim: "#3a8c6e",
    // Semantic
    grn: "#4daf8b", grnt: "#3a8c6e", grnb: "rgba(77,175,139,0.12)",
    red: "#e06060", redt: "#c04848", redb: "rgba(224,96,96,0.12)",
    ylw: "#e0b84a", ylwt: "#c8a040", ylwb: "rgba(224,184,74,0.12)",
    pur: "#a07cc8", purt: "#8866b0", purb: "rgba(160,124,200,0.12)",
    cyan: "#5ab8c8", cyant: "#4898a8", cyanb: "rgba(90,184,200,0.12)",
    ok: "#4daf8b", warn: "#e0b84a", err: "#e06060",
    inp: "#11132a",
    textDim: "#8884a0",
  },
  dim: {
    name: "Dim",
    // Warm charcoal
    bg:  "#28282e",
    bg2: "#313136",
    bg3: "#2c2c32",
    bg4: "#38383e",
    border:  "#48484e",
    borderL: "#3e3e44",
    borderH: "#62626a",
    text:  "#eae8e4",
    text2: "#d8d6d0",
    tm: "#b8b6b0", td: "#989690", tf: "#808078",
    tg: "#686860", ti: "#585850",
    accent:    "#e0aa50",
    accentDim: "#c09040",
    abg: "rgba(224,170,80,0.16)",
    abr: "rgba(224,170,80,0.32)",
    jade:    "#52b890",
    jadeDim: "#40966e",
    grn: "#52b890", grnt: "#40966e", grnb: "rgba(82,184,144,0.14)",
    red: "#e87070", redt: "#c85858", redb: "rgba(232,112,112,0.14)",
    ylw: "#e8c050", ylwt: "#d0a848", ylwb: "rgba(232,192,80,0.14)",
    pur: "#a888cc", purt: "#9070b4", purb: "rgba(168,136,204,0.14)",
    cyan: "#60c0cc", cyant: "#50a0b0", cyanb: "rgba(96,192,204,0.14)",
    ok: "#52b890", warn: "#e8c050", err: "#e87070",
    inp: "#313136",
    textDim: "#989690",
  },
  light: {
    name: "Light",
    // Warm papyrus / sand
    bg:  "#f5f0e8",
    bg2: "#fffdf8",
    bg3: "#faf6f0",
    bg4: "#eee8e0",
    border:  "#d8d0c4",
    borderL: "#e8e0d4",
    borderH: "#b0a898",
    text:  "#1a1820",
    text2: "#2a2830",
    tm: "#504840", td: "#686058", tf: "#887868",
    tg: "#a09888", ti: "#c0b8a8",
    accent:    "#b8862d",
    accentDim: "#9c7028",
    abg: "rgba(184,134,45,0.10)",
    abr: "rgba(184,134,45,0.28)",
    jade:    "#2e8a62",
    jadeDim: "#247050",
    grn: "#2e8a62", grnt: "#247050", grnb: "rgba(46,138,98,0.10)",
    red: "#c04242", redt: "#a03636", redb: "rgba(192,66,66,0.10)",
    ylw: "#a07824", ylwt: "#886420", ylwb: "rgba(160,120,36,0.10)",
    pur: "#7856a0", purt: "#644888", purb: "rgba(120,86,160,0.10)",
    cyan: "#388898", cyant: "#307080", cyanb: "rgba(56,136,152,0.10)",
    ok: "#2e8a62", warn: "#a07824", err: "#c04242",
    inp: "#fffdf8",
    textDim: "#686058",
  },
};

const THEME_ORDER: readonly string[] = ["dark", "dim", "light"] as const;
const STORAGE_KEY = "seshat_theme";

export const ThemeContext = createContext<Theme>(THEMES.dark);

export const useTheme = (): Theme => useContext(ThemeContext);

interface ThemeControls {
  theme: Theme;
  themeName: string;
  cycle: () => void;
  setThemeName: (name: string) => void;
}

const ThemeControlsContext = createContext<ThemeControls>({
  theme: THEMES.dark,
  themeName: "dark",
  cycle: () => {},
  setThemeName: () => {},
});

export const useThemeControls = (): ThemeControls =>
  useContext(ThemeControlsContext);

function loadSavedTheme(): string {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved && THEMES[saved]) return saved;
  } catch { /* ignore */ }
  return "dark";
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [themeName, setThemeNameState] = useState<string>(loadSavedTheme);
  const theme = THEMES[themeName] ?? THEMES.dark;

  const setThemeName = useCallback((name: string) => {
    if (!THEMES[name]) return;
    setThemeNameState(name);
    try { localStorage.setItem(STORAGE_KEY, name); } catch { /* ignore */ }
  }, []);

  const cycle = useCallback(() => {
    const idx = THEME_ORDER.indexOf(themeName);
    const next = THEME_ORDER[(idx + 1) % THEME_ORDER.length];
    setThemeName(next);
  }, [themeName, setThemeName]);

  useEffect(() => {
    document.documentElement.style.background = theme.bg;
    document.documentElement.style.colorScheme =
      themeName === "light" ? "light" : "dark";
  }, [theme.bg, themeName]);

  const controls = useMemo<ThemeControls>(
    () => ({ theme, themeName, cycle, setThemeName }),
    [theme, themeName, cycle, setThemeName],
  );

  return (
    <ThemeControlsContext.Provider value={controls}>
      <ThemeContext.Provider value={theme}>{children}</ThemeContext.Provider>
    </ThemeControlsContext.Provider>
  );
}
