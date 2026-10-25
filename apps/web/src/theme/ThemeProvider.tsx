// Holds the chosen colour preset, writes it onto <html> (`data-preset` for the colours,
// `data-theme` for the light/dark side the chat terminal reads) and remembers it per browser.

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useLayoutEffect,
  useMemo,
  useState,
} from "react";
import { initialPreset, type Preset, type PresetId, presetById, presetCss } from "./presets.ts";

export const THEME_KEY = "indus-theme";

function readStored(): string | null {
  try {
    return window.localStorage.getItem(THEME_KEY);
  } catch {
    return null;
  }
}

function writeStored(id: PresetId): void {
  try {
    window.localStorage.setItem(THEME_KEY, id);
  } catch {
    // storage unavailable: the choice lasts for this page only
  }
}

function systemPrefersDark(): boolean {
  if (typeof window.matchMedia !== "function") return true;
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export function applyPreset(preset: Preset, root: HTMLElement = document.documentElement): void {
  root.dataset.preset = preset.id;
  root.dataset.theme = preset.scheme;
}

interface ThemeApi {
  preset: Preset;
  choose(id: PresetId): void;
}

const ThemeContext = createContext<ThemeApi | null>(null);

export function useTheme(): ThemeApi {
  const api = useContext(ThemeContext);
  if (!api) throw new Error("useTheme needs <ThemeProvider>");
  return api;
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [preset, setPreset] = useState<Preset>(() =>
    initialPreset(readStored(), systemPrefersDark()),
  );

  useLayoutEffect(() => {
    applyPreset(preset);
  }, [preset]);

  const choose = useCallback((id: PresetId) => {
    const next = presetById(id);
    if (!next) return;
    writeStored(next.id);
    setPreset(next);
  }, []);

  const value = useMemo(() => ({ preset, choose }), [preset, choose]);
  return (
    <ThemeContext.Provider value={value}>
      <style data-indus-presets="">{presetCss()}</style>
      {children}
    </ThemeContext.Provider>
  );
}
