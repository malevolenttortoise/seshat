// Seshat theme — Egyptian goddess color scheme.
//
// Three brightness variants, each with distinct Egyptian character:
//   - dark:  nighttime temple — deep indigo with gold accents
//   - dim:   torchlit chamber — dark warm sand/papyrus
//   - light: sunlit papyrus — warm sand with rich bronze
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
  tm: string; td: string; tf: string; tg: string; ti: string;
  accent: string; accentDim: string;
  abg: string; abr: string;
  jade: string; jadeDim: string;
  grn: string; grnt: string; grnb: string;
  red: string; redt: string; redb: string;
  ylw: string; ylwt: string; ylwb: string;
  pur: string; purt: string; purb: string;
  cyan: string; cyant: string; cyanb: string;
  ok: string; warn: string; err: string;
  inp: string;
  textDim: string;
}

export const THEMES: Record<string, Theme> = {
  dark: {
    name: "Dark",
    // Nighttime temple — deep indigo
    bg:  "#1a1c30",
    bg2: "#222438",
    bg3: "#1e2034",
    bg4: "#2a2c42",
    border:  "#3a3c56",
    borderL: "#2e3048",
    borderH: "#545878",
    text:  "#e4e2ec",
    text2: "#cccad8",
    tm: "#a8a6b8", td: "#8886a0", tf: "#707088",
    tg: "#585870", ti: "#484860",
    accent:    "#deb060",
    accentDim: "#b89040",
    abg: "rgba(222,176,96,0.16)",
    abr: "rgba(222,176,96,0.32)",
    jade:    "#4cb888",
    jadeDim: "#3a9468",
    grn: "#4cb888", grnt: "#3a9468", grnb: "rgba(76,184,136,0.14)",
    red: "#e06060", redt: "#c04848", redb: "rgba(224,96,96,0.14)",
    ylw: "#e0c060", ylwt: "#c8a848", ylwb: "rgba(224,192,96,0.14)",
    pur: "#a088cc", purt: "#8870b4", purb: "rgba(160,136,204,0.14)",
    cyan: "#58b8cc", cyant: "#4898b0", cyanb: "rgba(88,184,204,0.14)",
    ok: "#4cb888", warn: "#e0c060", err: "#e06060",
    inp: "#222438",
    textDim: "#8886a0",
  },
  dim: {
    name: "Dim",
    // Torchlit chamber — dark warm sand/papyrus
    bg:  "#2a2520",
    bg2: "#332e28",
    bg3: "#2e2924",
    bg4: "#3a342e",
    border:  "#504840",
    borderL: "#443e38",
    borderH: "#6a6058",
    text:  "#ece6dc",
    text2: "#d8d0c4",
    tm: "#b8b0a4", td: "#989088", tf: "#807870",
    tg: "#686058", ti: "#585048",
    accent:    "#d4a050",
    accentDim: "#b88838",
    abg: "rgba(212,160,80,0.18)",
    abr: "rgba(212,160,80,0.34)",
    jade:    "#4aaa80",
    jadeDim: "#389060",
    grn: "#4aaa80", grnt: "#389060", grnb: "rgba(74,170,128,0.14)",
    red: "#d86060", redt: "#b84848", redb: "rgba(216,96,96,0.14)",
    ylw: "#d8b050", ylwt: "#c09840", ylwb: "rgba(216,176,80,0.14)",
    pur: "#9880b8", purt: "#8068a0", purb: "rgba(152,128,184,0.14)",
    cyan: "#50b0b8", cyant: "#40909a", cyanb: "rgba(80,176,184,0.14)",
    ok: "#4aaa80", warn: "#d8b050", err: "#d86060",
    inp: "#332e28",
    textDim: "#989088",
  },
  light: {
    name: "Light",
    // Sunlit papyrus
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
