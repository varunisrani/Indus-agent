// Our light/dark pair (L16, P2 part). The token CSS for both themes comes from @indus/ui and is
// written once into <head>; switching only flips `data-theme` on <html>. The choice is remembered
// per browser; without one we follow the system preference.

import { type ThemeName, themeNames, tokensToCss } from "@indus/ui";
import { useEffect, useState } from "react";

export const THEME_KEY = "indus-theme";

function stored(): ThemeName | null {
  try {
    const value = window.localStorage.getItem(THEME_KEY);
    return value === "indus-light" || value === "indus-dark" ? value : null;
  } catch {
    return null;
  }
}

function systemTheme(): ThemeName {
  const query =
    typeof window.matchMedia === "function"
      ? window.matchMedia("(prefers-color-scheme: dark)")
      : null;
  return query?.matches ? "indus-dark" : "indus-light";
}

/** One <style> with every theme's tokens; mounted once at the root. */
export function ThemeStyles() {
  return (
    <style data-indus-tokens="">{themeNames.map((name) => tokensToCss(name)).join("\n")}</style>
  );
}

export function ThemeSwitch({ compact }: { compact: boolean }) {
  const [theme, setTheme] = useState<ThemeName>(() => stored() ?? systemTheme());

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  const next: ThemeName = theme === "indus-dark" ? "indus-light" : "indus-dark";
  const label = next === "indus-dark" ? "Switch to dark theme" : "Switch to light theme";
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className="rounded-md border border-line px-2 py-1 text-xs text-fg"
      onClick={() => {
        setTheme(next);
        try {
          window.localStorage.setItem(THEME_KEY, next);
        } catch {
          // storage unavailable: the choice lasts for this page only
        }
      }}
    >
      {compact ? (theme === "indus-dark" ? "☾" : "☀") : theme === "indus-dark" ? "Dark" : "Light"}
    </button>
  );
}
