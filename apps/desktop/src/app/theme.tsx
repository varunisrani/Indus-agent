// Light / dark / system themes from the @indus/ui tokens. The token CSS for both themes is
// written once; switching flips `data-theme` on <html> and tells main so the title bar matches.

import { type ThemeName, themeNames, tokensToCss } from "@indus/ui";
import {
  createContext,
  type ReactNode,
  useContext,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react";
import { deskFromPresetCss, deskTokenCss } from "../theme/deskTokens.ts";
import { appearance } from "../theme/themeStore.ts";
import { desktopBridge } from "./bridge.ts";

export type ThemeMode = "light" | "dark" | "system";
export const THEME_KEY = "indus-desktop-theme";

function storedMode(): ThemeMode {
  try {
    const value = window.localStorage.getItem(THEME_KEY);
    return value === "light" || value === "dark" || value === "system" ? value : "system";
  } catch {
    return "system";
  }
}

function prefersDark(): boolean {
  return typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-color-scheme: dark)").matches
    : false;
}

export function resolveTheme(mode: ThemeMode, systemDark: boolean): ThemeName {
  if (mode === "dark") return "indus-dark";
  if (mode === "light") return "indus-light";
  return systemDark ? "indus-dark" : "indus-light";
}

/** Apply the stored theme before React renders (no flash of the wrong theme). */
export function applyStoredTheme(): void {
  document.documentElement.dataset.theme = resolveTheme(storedMode(), prefersDark());
}

interface ThemeValue {
  mode: ThemeMode;
  resolved: ThemeName;
  setMode: (mode: ThemeMode) => void;
}

const ThemeContext = createContext<ThemeValue | null>(null);

export function ThemeStyles() {
  return (
    <>
      <style data-indus-tokens="">{themeNames.map((name) => tokensToCss(name)).join("\n")}</style>
      {/* W51-b: the desktop's look; after the base tokens, before any preset */}
      <style data-desk-tokens="">{deskTokenCss()}</style>
    </>
  );
}

export function ThemeProvider(props: { children: ReactNode }) {
  const [mode, setModeState] = useState<ThemeMode>(storedMode);
  const [systemDark, setSystemDark] = useState(prefersDark);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => setSystemDark(query.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  const resolved = resolveTheme(mode, systemDark);

  useEffect(() => {
    document.documentElement.dataset.theme = resolved;
    desktopBridge()?.windows.setTheme({ mode });
  }, [resolved, mode]);

  const value = useMemo<ThemeValue>(
    () => ({
      mode,
      resolved,
      setMode: (next) => {
        setModeState(next);
        try {
          window.localStorage.setItem(THEME_KEY, next);
        } catch {
          // storage unavailable: the choice lasts for this window only
        }
      },
    }),
    [mode, resolved],
  );

  // P24-33: a chosen preset or user theme paints over the built-in tokens (nothing when unchosen)
  const choices = useSyncExternalStore(appearance.subscribe, appearance.get, appearance.get);
  const presetCss = useMemo(() => {
    if (choices.theme === null) return "";
    const scheme = resolved === "indus-dark" ? "dark" : "light";
    const css = appearance.css(null, scheme);
    return css === "" ? "" : `${css}\n${deskFromPresetCss(scheme)}`;
  }, [choices, resolved]);

  return (
    <ThemeContext.Provider value={value}>
      <ThemeStyles />
      {presetCss === "" ? null : <style data-indus-appearance="">{presetCss}</style>}
      {props.children}
    </ThemeContext.Provider>
  );
}

export function useTheme(): ThemeValue {
  const value = useContext(ThemeContext);
  if (value === null) throw new Error("useTheme outside ThemeProvider");
  return value;
}
