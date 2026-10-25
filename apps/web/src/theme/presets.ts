// Dashboard colour presets (W51-a). A preset is two colours: the canvas everything sits on and one
// ink used for text, rules and filled controls. Every other surface is a mix of the two, worked out
// in index.css, so a preset only has to name its pair (plus its light/dark side, which the chat
// terminal and the status colours follow).
//
// A preset may add a third colour, its accent (W63, the Indus AGI rebrand): the brand moments (the
// logo mark, filled buttons, the active nav marker, focus rings, links, picked tabs and toggles,
// the chart's input series) paint in it, with `onAccent` for text set on it. Body text stays ink.
// Without an accent those moments keep using the ink (and the canvas on it), so the presets from
// before the rebrand look exactly as they did.
//
// Eight presets, four on each side, before the rebrand (W56-G2-1). The four added in wave 56 are our own pairs, chosen
// so ink and the muted and soft ink mixes all clear WCAG AA (4.5:1) on the canvas and the raised
// surface; presets.test.ts measures every preset against the mixes in index.css.
//
// The three Indus presets (W63) come first and are the defaults: the dark lockup's black with the
// mark's ember, the same with larger type, and a light paper one with a deeper ember that keeps
// AA as text on it. The brand's colours come from @indus/ui.

import { BRAND_COLORS, type ThemeName } from "@indus/ui";

export type PresetId =
  | "indus-ember"
  | "indus-ember-large"
  | "indus-daylight"
  | "deep-teal"
  | "deep-teal-large"
  | "clear-blue"
  | "graphite"
  | "monsoon"
  | "sandstone"
  | "neem"
  | "indigo-paper";

export interface Preset {
  id: PresetId;
  label: string;
  description: string;
  /** Which side of the light/dark pair the preset belongs to (`data-theme` on <html>). */
  scheme: ThemeName;
  canvas: string;
  ink: string;
  /** Brand colour for marks, filled controls, focus and links; the ink when absent. */
  accent?: string;
  /** Text and icons on the accent; the canvas when absent. */
  onAccent?: string;
  /** Root font size in px; the large preset scales every rem-based size up. */
  rootPx: number;
}

/** Warm white ink for the dark Indus presets (17.6:1 on the lockup black). */
const EMBER_INK = "#f5f1eb";
/** Warm paper canvas and deeper ember for the light Indus preset (ember text on it: 5.17:1). */
const DAYLIGHT_CANVAS = "#faf6ef";
const DAYLIGHT_ACCENT = "#c23300";

export const PRESETS: readonly Preset[] = [
  {
    id: "indus-ember",
    label: "Indus Ember",
    description: "The Indus AGI lockup: ember on black with warm white ink",
    scheme: "indus-dark",
    canvas: BRAND_COLORS.black,
    ink: EMBER_INK,
    accent: BRAND_COLORS.ember,
    // White on ember is only 3.8:1; the lockup black on it is 5.2:1.
    onAccent: BRAND_COLORS.black,
    rootPx: 16,
  },
  {
    id: "indus-ember-large",
    label: "Indus Ember Large",
    description: "The same lockup colours with bigger type and more room",
    scheme: "indus-dark",
    canvas: BRAND_COLORS.black,
    ink: EMBER_INK,
    accent: BRAND_COLORS.ember,
    onAccent: BRAND_COLORS.black,
    rootPx: 18,
  },
  {
    id: "indus-daylight",
    label: "Indus Daylight",
    description: "Light mode: black ink and deep ember on warm paper",
    scheme: "indus-light",
    canvas: DAYLIGHT_CANVAS,
    ink: BRAND_COLORS.black,
    accent: DAYLIGHT_ACCENT,
    onAccent: BRAND_COLORS.white,
    rootPx: 16,
  },
  {
    id: "deep-teal",
    label: "Deep Teal",
    description: "Dark teal canvas with warm cream ink",
    scheme: "indus-dark",
    canvas: "#041c1c",
    ink: "#ffe6cb",
    rootPx: 16,
  },
  {
    id: "deep-teal-large",
    label: "Deep Teal Large",
    description: "The same pair with bigger type and more room",
    scheme: "indus-dark",
    canvas: "#041c1c",
    ink: "#ffe6cb",
    rootPx: 18,
  },
  {
    id: "clear-blue",
    label: "Clear Blue",
    description: "Light mode: strong blue ink on a pale sky canvas",
    scheme: "indus-light",
    canvas: "#e8f2fd",
    ink: "#0053fd",
    rootPx: 16,
  },
  {
    id: "graphite",
    label: "Graphite",
    description: "Neutral greys for a quiet dark screen",
    scheme: "indus-dark",
    canvas: "#121314",
    ink: "#e6e6e3",
    rootPx: 16,
  },
  {
    id: "monsoon",
    label: "Monsoon",
    description: "Rain-cloud blue canvas with pale mist ink",
    scheme: "indus-dark",
    canvas: "#0f1a24",
    ink: "#d6e6f2",
    rootPx: 16,
  },
  {
    id: "sandstone",
    label: "Sandstone",
    description: "Light mode: dark umber ink on warm sand paper",
    scheme: "indus-light",
    canvas: "#fdfaf4",
    ink: "#1a1208",
    rootPx: 16,
  },
  {
    id: "neem",
    label: "Neem",
    description: "Light mode: deep leaf-green ink on a pale green wash",
    scheme: "indus-light",
    canvas: "#fcfdfa",
    ink: "#03180a",
    rootPx: 16,
  },
  {
    id: "indigo-paper",
    label: "Indigo Paper",
    description: "Light mode: indigo-dye ink on cool white paper",
    scheme: "indus-light",
    canvas: "#fbfaff",
    ink: "#110e3e",
    rootPx: 16,
  },
];

export const DARK_DEFAULT: PresetId = "indus-ember";
export const LIGHT_DEFAULT: PresetId = "indus-daylight";

export function presetById(id: string | null | undefined): Preset | undefined {
  return PRESETS.find((preset) => preset.id === id);
}

/** The preset to start with: the stored one if known, else the system's light or dark default. */
export function initialPreset(stored: string | null, prefersDark: boolean): Preset {
  const known = presetById(stored);
  if (known) return known;
  // Choices saved by the earlier light/dark toggle still count.
  if (stored === "indus-light") return presetById(LIGHT_DEFAULT) as Preset;
  if (stored === "indus-dark") return presetById(DARK_DEFAULT) as Preset;
  return presetById(prefersDark ? DARK_DEFAULT : LIGHT_DEFAULT) as Preset;
}

/** The accent a preset paints its brand moments in (its ink when it names none). */
export function accentOf(preset: Preset): string {
  return preset.accent ?? preset.ink;
}

/** The colour of text set on the accent (the canvas when the preset names none). */
export function onAccentOf(preset: Preset): string {
  return preset.onAccent ?? preset.canvas;
}

/**
 * One rule per preset, keyed on `data-preset`, setting the pair the derived tokens mix from and
 * the accent pair. A preset that names its own accent also sets `--indus-brand-accent`, which the
 * few places that kept a fixed colour before the accent existed (the light chart series, the chat
 * cursor) switch to.
 */
export function presetCss(presets: readonly Preset[] = PRESETS): string {
  return presets
    .map((p) => {
      const brand = p.accent ? ` --indus-brand-accent: ${p.accent};` : "";
      return `:root[data-preset="${p.id}"] { --indus-canvas: ${p.canvas}; --indus-ink: ${p.ink}; --indus-accent: ${accentOf(p)}; --indus-on-accent: ${onAccentOf(p)};${brand} font-size: ${p.rootPx}px; color-scheme: ${p.scheme === "indus-dark" ? "dark" : "light"}; }`;
    })
    .join("\n");
}
