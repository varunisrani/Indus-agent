// Dashboard colour presets (W51-a). A preset is two colours: the canvas everything sits on and one
// ink used for text, rules and filled controls. Every other surface is a mix of the two, worked out
// in index.css, so a preset only has to name its pair (plus its light/dark side, which the chat
// terminal and the status colours follow).
//
// Eight presets, four on each side (W56-G2-1). The four added in wave 56 are our own pairs, chosen
// so ink and the muted and soft ink mixes all clear WCAG AA (4.5:1) on the canvas and the raised
// surface; presets.test.ts measures every preset against the mixes in index.css.

import type { ThemeName } from "@indus/ui";

export type PresetId =
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
  /** Root font size in px; the large preset scales every rem-based size up. */
  rootPx: number;
}

export const PRESETS: readonly Preset[] = [
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

export const DARK_DEFAULT: PresetId = "deep-teal";
export const LIGHT_DEFAULT: PresetId = "clear-blue";

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

/** One rule per preset, keyed on `data-preset`, setting the pair the derived tokens mix from. */
export function presetCss(presets: readonly Preset[] = PRESETS): string {
  return presets
    .map(
      (p) =>
        `:root[data-preset="${p.id}"] { --indus-canvas: ${p.canvas}; --indus-ink: ${p.ink}; font-size: ${p.rootPx}px; color-scheme: ${p.scheme === "indus-dark" ? "dark" : "light"}; }`,
    )
    .join("\n");
}
