// Dashboard colour presets (W51-a). A preset is two colours: the canvas everything sits on and one
// ink used for text, rules and filled controls. Every other surface is a mix of the two, worked out
// in index.css, so a preset only has to name its pair (plus its light/dark side, which the chat
// terminal and the status colours follow).

import type { ThemeName } from "@indus/ui";

export type PresetId = "deep-teal" | "deep-teal-large" | "clear-blue" | "graphite";

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
