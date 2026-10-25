// The TUI theme: colour tokens plus brand marks. Two seed sets (dark, light) built from the
// `@indus/ui` brand colours — ember accent, sand secondary, gold warnings, warm near-black and
// near-white neutrals — every secondary tone derived from the seeds and the real background, and
// every foreground lifted to a contrast floor.

import { BRAND_COLORS, BRAND_NAME, makeSwatch, palette } from "@indus/ui";
import { createContext, createElement, type ReactNode, useContext } from "react";
import { useStore } from "zustand";
import { createStore, type StoreApi } from "zustand/vanilla";
import { contrast, fillFor, liftToContrast, mix, normaliseHex } from "./palette.ts";

export interface ThemeBrand {
  /** Product name shown in the banner and title. */
  name: string;
  /** The brand accent colour (same as `color.accent`). */
  accent: string;
  /** The composer's prompt glyph. */
  prompt: string;
  /** The gutter mark in front of tool rows. */
  tool: string;
  welcome: string;
  goodbye: string;
}

/** Every colour token a theme defines. */
export const COLOR_TOKENS = [
  "background",
  "text",
  "prompt",
  "statusFg",
  "muted",
  "label",
  "accent",
  "secondary",
  "border",
  "ok",
  "warn",
  "error",
  "tool",
  "thinking",
  "link",
  "diffAdded",
  "diffRemoved",
  "syntaxString",
  "syntaxNumber",
  "syntaxKeyword",
  "syntaxComment",
  "shellPrompt",
  "surface",
  "statusBg",
  "activeRow",
  "selection",
] as const;
export type ColorToken = (typeof COLOR_TOKENS)[number];

/** Known tokens are always present; payloads may add more. */
export type ThemeColors = Record<ColorToken, string> & Record<string, string>;

export interface Theme {
  /** Theme name (`indus-dark`, `indus-light`, or a payload's name). */
  name: string;
  light: boolean;
  /** The background the colours were fitted against. */
  background: string;
  color: ThemeColors;
  brand: ThemeBrand;
}

export const THEME_DARK = "indus-dark";
export const THEME_LIGHT = "indus-light";

/** Warm, almost-neutral greys: the brand lockup is ember on black and white. */
const neutral = (l: number): string => makeSwatch({ l, c: 0.006, h: 75 }).hex;
/** A muted earth brown for the light secondary (sand is too pale on a light background). */
const umber = (l: number): string => makeSwatch({ l, c: 0.035, h: 60 }).hex;

interface Seeds {
  background: string;
  text: string;
  accent: string;
  secondary: string;
  ok: string;
  warn: string;
  error: string;
}

const DARK_SEEDS: Seeds = {
  background: BRAND_COLORS.black,
  text: mix(BRAND_COLORS.cream, BRAND_COLORS.white, 0.4),
  accent: BRAND_COLORS.ember,
  secondary: BRAND_COLORS.sand,
  ok: palette.reed[400].hex,
  // Brand gold, well clear of the ember accent.
  warn: BRAND_COLORS.gold,
  error: palette.kiln[400].hex,
};

const LIGHT_SEEDS: Seeds = {
  background: neutral(0.985),
  text: neutral(0.22),
  // Halfway to the deep ember: the bright ember is too thin a stroke on white.
  accent: mix(BRAND_COLORS.ember, BRAND_COLORS.emberDeep, 0.5),
  secondary: umber(0.45),
  ok: palette.reed[700].hex,
  warn: palette.ochre[600].hex,
  error: palette.kiln[700].hex,
};

const BRAND: Omit<ThemeBrand, "accent"> = {
  name: BRAND_NAME,
  prompt: "›",
  tool: "│",
  welcome: "Ask anything, or type / for commands.",
  goodbye: "Session saved. See you next time.",
};

/** Foreground tokens and the contrast each needs against the background. */
export const FOREGROUND_FLOORS: Readonly<Record<string, number>> = {
  text: 4.5,
  prompt: 4.5,
  statusFg: 4.5,
  muted: 3,
  label: 3,
  accent: 3,
  secondary: 3,
  border: 3,
  ok: 3,
  warn: 3,
  error: 3,
  tool: 3,
  thinking: 3,
  link: 3,
  diffAdded: 3,
  diffRemoved: 3,
  syntaxString: 3,
  syntaxNumber: 3,
  syntaxKeyword: 3,
  syntaxComment: 3,
  shellPrompt: 3,
};

/** Fill tokens: body text on them must still reach 4.5:1. */
export const FILL_TOKENS: readonly string[] = ["surface", "statusBg", "activeRow", "selection"];
export const TEXT_ON_FILL = 4.5;

function derive(seeds: Seeds, light: boolean): ThemeColors {
  const { background: bg, text, accent, secondary } = seeds;
  const surface = mix(bg, accent, light ? 0.05 : 0.08);
  return {
    background: bg,
    text,
    prompt: accent,
    statusFg: mix(text, bg, 0.15),
    muted: mix(text, bg, light ? 0.4 : 0.45),
    label: mix(text, secondary, 0.35),
    accent,
    secondary,
    border: mix(secondary, bg, 0.3),
    ok: seeds.ok,
    warn: seeds.warn,
    error: seeds.error,
    tool: secondary,
    thinking: mix(text, bg, 0.35),
    link: secondary,
    diffAdded: seeds.ok,
    diffRemoved: seeds.error,
    syntaxString: mix(seeds.ok, text, 0.2),
    syntaxNumber: mix(accent, text, 0.3),
    syntaxKeyword: secondary,
    syntaxComment: mix(text, bg, 0.45),
    shellPrompt: seeds.warn,
    surface,
    statusBg: surface,
    activeRow: mix(surface, accent, 0.2),
    selection: mix(bg, secondary, light ? 0.22 : 0.3),
  };
}

/** Lift every foreground to its floor against `bg`, then keep body text readable on every fill. */
export function fitToBackground(color: ThemeColors, bg: string): ThemeColors {
  const out: ThemeColors = { ...color, background: bg };
  for (const [token, floor] of Object.entries(FOREGROUND_FLOORS)) {
    const value = out[token];
    if (value !== undefined) out[token] = liftToContrast(value, bg, floor);
  }
  const ink = out.text;
  for (const token of FILL_TOKENS) {
    const value = out[token];
    if (value !== undefined) out[token] = fillFor(value, bg, ink, TEXT_ON_FILL);
  }
  return out;
}

export interface BuildOptions {
  light: boolean;
  /** The terminal's real background when known (OSC 11 or a hint); the seed background otherwise. */
  background?: string | null;
  name?: string;
  /** Colours from a theme payload, merged over the derived ones before fitting. */
  colors?: Record<string, string>;
  brand?: Partial<Omit<ThemeBrand, "accent">>;
}

export function buildTheme(options: BuildOptions): Theme {
  const seeds = options.light ? LIGHT_SEEDS : DARK_SEEDS;
  const background = normaliseHex(options.background ?? "") ?? seeds.background;
  const derived = derive({ ...seeds, background }, options.light);
  for (const [token, raw] of Object.entries(options.colors ?? {})) {
    const hex = normaliseHex(raw);
    if (hex !== null && token !== "background") derived[token] = hex;
  }
  const color = fitToBackground(derived, background);
  const brand: ThemeBrand = { ...BRAND, ...cleanBrand(options.brand), accent: color.accent };
  return {
    name: options.name ?? (options.light ? THEME_LIGHT : THEME_DARK),
    light: options.light,
    background,
    color,
    brand,
  };
}

function cleanBrand(brand: BuildOptions["brand"]): Partial<Omit<ThemeBrand, "accent">> {
  const out: Partial<Omit<ThemeBrand, "accent">> = {};
  for (const [key, value] of Object.entries(brand ?? {})) {
    if (typeof value !== "string") continue;
    const text = value.replace(/\s+/g, " ").trim();
    if (text !== "") out[key as keyof typeof out] = text;
  }
  return out;
}

/** Every (foreground, background) and (text, fill) pair with the ratio it reaches. */
export function contrastReport(theme: Theme): { pair: string; ratio: number; floor: number }[] {
  const rows: { pair: string; ratio: number; floor: number }[] = [];
  const bg = theme.background;
  for (const [token, floor] of Object.entries(FOREGROUND_FLOORS)) {
    const value = theme.color[token];
    if (value !== undefined)
      rows.push({ pair: `${token}/background`, ratio: contrast(value, bg), floor });
  }
  const ink = theme.color.text;
  for (const token of FILL_TOKENS) {
    const value = theme.color[token];
    if (value !== undefined)
      rows.push({ pair: `text/${token}`, ratio: contrast(ink, value), floor: TEXT_ON_FILL });
  }
  return rows;
}

// -- payloads and names -------------------------------------------------------------------------

/** What a server may send: a theme name, or a name with colour and brand overrides. */
export type ThemePayload =
  | string
  | { name?: string; colors?: Record<string, unknown>; brand?: Record<string, unknown> };

/** `auto` → the detected polarity; known names and their short aliases → that polarity. */
export function polarityForName(name: string, detectedLight: boolean): boolean | null {
  const key = name.trim().toLowerCase();
  if (key === "" || key === "auto" || key === "default") return detectedLight;
  if (key === "light" || key === THEME_LIGHT) return true;
  if (key === "dark" || key === THEME_DARK) return false;
  return null;
}

export const THEME_NAMES: readonly string[] = [THEME_DARK, THEME_LIGHT, "auto"];

function stringRecord(value: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  if (typeof value !== "object" || value === null) return out;
  for (const [k, v] of Object.entries(value)) if (typeof v === "string") out[k] = v;
  return out;
}

/** Merge a payload over the default for the detected polarity and background. */
export function themeFromPayload(
  payload: ThemePayload,
  detected: { light: boolean; background?: string | null },
): Theme {
  const body = typeof payload === "string" ? { name: payload } : payload;
  const name = body.name ?? "auto";
  const light = polarityForName(name, detected.light) ?? detected.light;
  const known = polarityForName(name, detected.light) !== null;
  return buildTheme({
    light,
    background: detected.background ?? null,
    name: known ? (light ? THEME_LIGHT : THEME_DARK) : name,
    colors: stringRecord(body.colors),
    brand: stringRecord(body.brand),
  });
}

// -- live store, preview and React access -------------------------------------------------------

export interface ThemeState {
  theme: Theme;
  /** A theme shown while `/theme` browses; null when none. */
  preview: Theme | null;
  /** What detection decided; payloads and names resolve against it. */
  detected: { light: boolean; background: string | null };
}

export const themeStore: StoreApi<ThemeState> = createStore<ThemeState>()(() => ({
  theme: buildTheme({ light: false }),
  preview: null,
  detected: { light: false, background: null },
}));

/** The theme on screen: the preview while browsing, else the committed one. */
export function currentTheme(): Theme {
  const s = themeStore.getState();
  return s.preview ?? s.theme;
}

export function setDetected(light: boolean, background: string | null): void {
  themeStore.setState({ detected: { light, background } });
}

/**
 * The terminal reported its colours changed (W47: the dashboard's Chat tab switching between its
 * light and dark themes): detect again and re-apply `requested` (the theme name the server or
 * `/theme` asked for, `auto` by default), so an automatic theme follows the terminal.
 */
export function followTerminalScheme(
  light: boolean,
  background: string | null,
  requested: string,
): Theme {
  const { detected } = themeStore.getState();
  // A report without a colour keeps the known background only when the polarity is unchanged.
  setDetected(light, background ?? (detected.light === light ? detected.background : null));
  return applyThemePayload(requested);
}

/** Apply a server theme (ready frame or a change event) as the committed theme. */
export function applyThemePayload(payload: ThemePayload): Theme {
  const theme = themeFromPayload(payload, themeStore.getState().detected);
  themeStore.setState({ theme, preview: null });
  return theme;
}

/** Show `name` without committing it; unknown names leave the screen as it is. */
export function previewTheme(name: string): boolean {
  const { detected } = themeStore.getState();
  if (polarityForName(name, detected.light) === null) return false;
  themeStore.setState({ preview: themeFromPayload(name, detected) });
  return true;
}

export function commitPreview(): Theme {
  const s = themeStore.getState();
  const theme = s.preview ?? s.theme;
  themeStore.setState({ theme, preview: null });
  return theme;
}

export function cancelPreview(): void {
  themeStore.setState({ preview: null });
}

const ThemeContext = createContext<Theme | null>(null);

/** Pins a theme for a subtree (tests, previews of one card). */
export function ThemeProvider(props: { theme: Theme; children?: ReactNode }): ReactNode {
  return createElement(ThemeContext.Provider, { value: props.theme }, props.children);
}

export function useTheme(): Theme {
  const pinned = useContext(ThemeContext);
  const live = useStore(themeStore, (s) => s.preview ?? s.theme);
  return pinned ?? live;
}
