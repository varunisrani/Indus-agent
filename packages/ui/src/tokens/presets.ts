/**
 * Twelve colour presets of our own (P24-33), each a light and a dark theme over the same semantic
 * tokens as `semantic.ts`. A preset is a recipe — an accent hue and chroma, a neutral hue and
 * tint — and both sides are built from it in OKLCH, then fitted so every text pair reaches WCAG AA
 * (4.5:1) and every UI pair 3:1 against its background (`CONTRAST_PAIRS`). Fitting only moves
 * lightness, one step at a time, so the result is deterministic.
 *
 * `presetTheme(id, mode, { accentHue })` gives one side, with an optional accent override that is
 * fitted the same way. `userTheme` turns a person's own hex colours into themes (refusing anything
 * that is not `#rrggbb`), and `themeCss` writes a theme's colours as `--indus-color-*` variables.
 */

import { makeSwatch, type Oklch, oklchCss, type Swatch } from "./palette.ts";
import {
  type SemanticColour,
  type SemanticName,
  type SemanticTheme,
  semanticNames,
} from "./semantic.ts";

export type PresetMode = "light" | "dark";

export interface PresetRecipe {
  readonly id: string;
  readonly label: string;
  readonly accentHue: number;
  readonly accentChroma: number;
  readonly neutralHue: number;
  /** Tint of the neutrals (0 is grey). */
  readonly neutralChroma: number;
}

export const presets: readonly PresetRecipe[] = [
  {
    id: "indus",
    label: "Indus",
    accentHue: 222,
    accentChroma: 0.125,
    neutralHue: 72,
    neutralChroma: 0.016,
  },
  {
    id: "monsoon",
    label: "Monsoon",
    accentHue: 252,
    accentChroma: 0.13,
    neutralHue: 240,
    neutralChroma: 0.02,
  },
  {
    id: "laterite",
    label: "Laterite",
    accentHue: 36,
    accentChroma: 0.15,
    neutralHue: 50,
    neutralChroma: 0.022,
  },
  {
    id: "saffron",
    label: "Saffron",
    accentHue: 68,
    accentChroma: 0.15,
    neutralHue: 80,
    neutralChroma: 0.02,
  },
  {
    id: "indigo",
    label: "Indigo",
    accentHue: 276,
    accentChroma: 0.16,
    neutralHue: 268,
    neutralChroma: 0.018,
  },
  {
    id: "basalt",
    label: "Basalt",
    accentHue: 200,
    accentChroma: 0.1,
    neutralHue: 250,
    neutralChroma: 0.006,
  },
  {
    id: "lotus",
    label: "Lotus",
    accentHue: 352,
    accentChroma: 0.14,
    neutralHue: 340,
    neutralChroma: 0.016,
  },
  {
    id: "mangrove",
    label: "Mangrove",
    accentHue: 158,
    accentChroma: 0.12,
    neutralHue: 150,
    neutralChroma: 0.018,
  },
  {
    id: "dune",
    label: "Dune",
    accentHue: 84,
    accentChroma: 0.11,
    neutralHue: 76,
    neutralChroma: 0.03,
  },
  {
    id: "harbour",
    label: "Harbour",
    accentHue: 236,
    accentChroma: 0.11,
    neutralHue: 214,
    neutralChroma: 0.024,
  },
  {
    id: "terrace",
    label: "Terrace",
    accentHue: 128,
    accentChroma: 0.12,
    neutralHue: 108,
    neutralChroma: 0.02,
  },
  {
    id: "jade",
    label: "Jade",
    accentHue: 172,
    accentChroma: 0.11,
    neutralHue: 182,
    neutralChroma: 0.014,
  },
];

export const presetIds: readonly string[] = presets.map((p) => p.id);
export const DEFAULT_PRESET = "indus";

/** `[foreground, background, minimum ratio]`: text pairs at 4.5:1, UI pairs at 3:1. */
export const CONTRAST_PAIRS: readonly (readonly [SemanticName, SemanticName, number])[] = [
  ["fg", "bg", 4.5],
  ["fg", "bg-raised", 4.5],
  ["fg", "code-bg", 4.5],
  ["fg-muted", "bg", 4.5],
  ["fg-muted", "bg-raised", 4.5],
  ["accent-fg", "accent", 4.5],
  ["border", "bg", 3],
  ["success", "bg", 3],
  ["warning", "bg", 3],
  ["danger", "bg", 3],
  ["focus-ring", "bg", 3],
];

// -- contrast -------------------------------------------------------------------------------------

function channel(value: number): number {
  const v = value / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}

/** WCAG 2.x relative luminance of `#rrggbb`. */
export function relativeLuminance(hex: string): number {
  const n = Number.parseInt(hex.slice(1, 7), 16);
  return (
    0.2126 * channel((n >> 16) & 0xff) +
    0.7152 * channel((n >> 8) & 0xff) +
    0.0722 * channel(n & 0xff)
  );
}

export function contrastRatio(a: string, b: string): number {
  const [hi, lo] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x) as [
    number,
    number,
  ];
  return (hi + 0.05) / (lo + 0.05);
}

/**
 * `colour` with its lightness moved (towards `direction`, 0.005 per step) until it reaches
 * `minimum` against every hex in `against`; the original when it already does.
 */
export function fitLightness(
  colour: Oklch,
  against: readonly string[],
  minimum: number,
  direction: "darker" | "lighter",
): Swatch {
  let current = colour;
  for (let step = 0; step < 200; step++) {
    const swatch = makeSwatch(current);
    if (against.every((hex) => contrastRatio(swatch.hex, hex) >= minimum)) return swatch;
    const l = direction === "darker" ? current.l - 0.005 : current.l + 0.005;
    current = { ...current, l: Math.min(1, Math.max(0, l)) };
  }
  return makeSwatch(current);
}

// -- building a side ------------------------------------------------------------------------------

const colour = (swatch: Swatch, source: string): SemanticColour => ({
  css: swatch.css,
  hex: swatch.hex,
  source,
});

function translucent(swatch: Swatch, alpha: number, source: string): SemanticColour {
  const a = Math.round(alpha * 255)
    .toString(16)
    .padStart(2, "0");
  return {
    css: oklchCss(swatch.oklch, alpha),
    hex: `${swatch.hex}${a}`,
    source: `${source}/${alpha}`,
  };
}

/** Fixed hues for the status colours, so they read the same in every preset. */
const STATUS = { success: 150, warning: 75, danger: 27 } as const;

function build(recipe: PresetRecipe, mode: PresetMode, accentHue: number): SemanticTheme {
  const nh = recipe.neutralHue;
  const nc = recipe.neutralChroma;
  const ac = recipe.accentChroma;
  const dark = mode === "dark";
  const away = dark ? "lighter" : "darker";
  const tag = `${recipe.id}.${mode}`;

  const bg = makeSwatch({ l: dark ? 0.2 : 0.975, c: nc * (dark ? 0.8 : 0.6), h: nh });
  const raised = makeSwatch({ l: dark ? 0.255 : 1, c: dark ? nc * 0.8 : 0, h: nh });
  const code = makeSwatch({ l: dark ? 0.255 : 0.94, c: nc, h: nh });
  const surfaces = [bg.hex, raised.hex, code.hex];
  const fg = fitLightness({ l: dark ? 0.95 : 0.27, c: nc * 0.8, h: nh }, surfaces, 7, away);
  const muted = fitLightness(
    { l: dark ? 0.78 : 0.47, c: nc * 1.4, h: nh },
    [bg.hex, raised.hex],
    4.5,
    away,
  );
  const border = fitLightness({ l: dark ? 0.55 : 0.6, c: nc * 2, h: nh }, [bg.hex], 3, away);

  // the accent carries text: light mode white on the accent, dark mode the background on it
  const accentText = dark ? bg : makeSwatch({ l: 1, c: 0, h: 0 });
  const accent = fitLightness(
    { l: dark ? 0.72 : 0.52, c: ac, h: accentHue },
    [accentText.hex, bg.hex],
    dark ? 4.5 : 4.5,
    dark ? "lighter" : "darker",
  );
  const focus = fitLightness({ l: dark ? 0.78 : 0.6, c: ac, h: accentHue }, [bg.hex], 3, away);
  const status = (hue: number) =>
    fitLightness({ l: dark ? 0.75 : 0.5, c: 0.13, h: hue }, [bg.hex], 3, away);

  return {
    bg: colour(bg, `${tag}.bg`),
    "bg-raised": colour(raised, `${tag}.bg-raised`),
    fg: colour(fg, `${tag}.fg`),
    "fg-muted": colour(muted, `${tag}.fg-muted`),
    border: colour(border, `${tag}.border`),
    accent: colour(accent, `${tag}.accent`),
    "accent-fg": colour(accentText, `${tag}.accent-fg`),
    success: colour(status(STATUS.success), `${tag}.success`),
    warning: colour(status(STATUS.warning), `${tag}.warning`),
    danger: colour(status(STATUS.danger), `${tag}.danger`),
    "focus-ring": colour(focus, `${tag}.focus-ring`),
    "code-bg": colour(code, `${tag}.code-bg`),
    selection: translucent(
      makeSwatch({ l: dark ? 0.55 : 0.8, c: ac, h: accentHue }),
      dark ? 0.45 : 0.4,
      `${tag}.selection`,
    ),
  };
}

export function presetRecipe(id: string): PresetRecipe {
  return presets.find((p) => p.id === id) ?? (presets[0] as PresetRecipe);
}

/** One side of a preset; an unknown id gives the default preset. */
export function presetTheme(
  id: string,
  mode: PresetMode,
  options: { accentHue?: number } = {},
): SemanticTheme {
  const recipe = presetRecipe(id);
  const hue =
    options.accentHue !== undefined && Number.isFinite(options.accentHue)
      ? ((options.accentHue % 360) + 360) % 360
      : recipe.accentHue;
  return build(recipe, mode, hue);
}

// -- user themes ----------------------------------------------------------------------------------

export const HEX_COLOUR = /^#[0-9a-f]{6}$/i;

export interface UserThemeInput {
  readonly id: string;
  readonly label: string;
  readonly light: Readonly<Record<SemanticName, string>>;
  readonly dark: Readonly<Record<SemanticName, string>>;
}

export class UserThemeError extends Error {}

/** The person's own colours as two themes; every token must be `#rrggbb`. */
export function userTheme(input: UserThemeInput): Record<PresetMode, SemanticTheme> {
  const side = (mode: PresetMode): SemanticTheme => {
    const raw = input[mode];
    const out: Partial<Record<SemanticName, SemanticColour>> = {};
    for (const name of semanticNames) {
      const value = raw[name];
      if (typeof value !== "string" || !HEX_COLOUR.test(value)) {
        throw new UserThemeError(`${mode}.${name} must be a #rrggbb colour`);
      }
      const hex = value.toLowerCase();
      out[name] = { css: hex, hex, source: `user.${input.id}` };
    }
    return out as SemanticTheme;
  };
  return { light: side("light"), dark: side("dark") };
}

/** Which contrast pairs a theme misses (empty when it passes AA). */
export function contrastFailures(theme: SemanticTheme): string[] {
  return CONTRAST_PAIRS.filter(
    ([fg, bg, min]) => contrastRatio(theme[fg].hex, theme[bg].hex) < min,
  ).map(([fg, bg, min]) => `${fg} on ${bg} below ${min}:1`);
}

/** `--indus-color-*` declarations for `theme` under `selector` (hex, then OKLCH where supported). */
export function themeCss(theme: SemanticTheme, selector: string): string {
  const hex = semanticNames.map((name) => `  --indus-color-${name}: ${theme[name].hex};`);
  const modern = semanticNames.map((name) => `    --indus-color-${name}: ${theme[name].css};`);
  return [
    `${selector} {`,
    ...hex,
    "}",
    "@supports (color: oklch(0% 0 0)) {",
    `  ${selector} {`,
    ...modern,
    "  }",
    "}",
    "",
  ].join("\n");
}
