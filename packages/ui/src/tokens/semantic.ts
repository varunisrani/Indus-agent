import { oklchCss, palette, type RampName, type RampStep, type Swatch, white } from "./palette.ts";

export const themeNames = ["indus-light", "indus-dark"] as const;
export type ThemeName = (typeof themeNames)[number];

export const semanticNames = [
  "bg",
  "bg-raised",
  "fg",
  "fg-muted",
  "border",
  "accent",
  "accent-fg",
  "success",
  "warning",
  "danger",
  "focus-ring",
  "code-bg",
  "selection",
] as const;
export type SemanticName = (typeof semanticNames)[number];

export interface SemanticColour {
  /** CSS colour in OKLCH, alpha included when below 1. */
  readonly css: string;
  /** Hex fallback: `#rrggbb`, or `#rrggbbaa` when translucent. */
  readonly hex: string;
  /** Where the colour comes from, e.g. `silt.50`, `white`. */
  readonly source: string;
}

export type SemanticTheme = Readonly<Record<SemanticName, SemanticColour>>;

function alphaHex(alpha: number): string {
  return Math.round(alpha * 255)
    .toString(16)
    .padStart(2, "0");
}

function fromSwatch(swatch: Swatch, source: string, alpha = 1): SemanticColour {
  if (alpha >= 1) return { css: swatch.css, hex: swatch.hex, source };
  return {
    css: oklchCss(swatch.oklch, alpha),
    hex: `${swatch.hex}${alphaHex(alpha)}`,
    source: `${source}/${alpha}`,
  };
}

function ramp(name: RampName, step: RampStep, alpha = 1): SemanticColour {
  return fromSwatch(palette[name][step], `${name}.${step}`, alpha);
}

const light: SemanticTheme = {
  bg: ramp("silt", 50),
  "bg-raised": fromSwatch(white, "white"),
  fg: ramp("silt", 900),
  "fg-muted": ramp("silt", 700),
  border: ramp("silt", 500),
  accent: ramp("river", 600),
  "accent-fg": fromSwatch(white, "white"),
  success: ramp("reed", 700),
  warning: ramp("ochre", 700),
  danger: ramp("kiln", 600),
  "focus-ring": ramp("river", 500),
  "code-bg": ramp("silt", 100),
  selection: ramp("river", 300, 0.4),
};

const dark: SemanticTheme = {
  bg: ramp("silt", 950),
  "bg-raised": ramp("silt", 900),
  fg: ramp("silt", 50),
  "fg-muted": ramp("silt", 300),
  border: ramp("silt", 600),
  accent: ramp("river", 400),
  "accent-fg": ramp("silt", 950),
  success: ramp("reed", 400),
  warning: ramp("ochre", 400),
  danger: ramp("kiln", 400),
  "focus-ring": ramp("river", 300),
  "code-bg": ramp("silt", 900),
  selection: ramp("river", 600, 0.45),
};

export const themes: Readonly<Record<ThemeName, SemanticTheme>> = {
  "indus-light": light,
  "indus-dark": dark,
};

export function isThemeName(value: string): value is ThemeName {
  return (themeNames as readonly string[]).includes(value);
}
