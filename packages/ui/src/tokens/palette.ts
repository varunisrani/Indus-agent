/**
 * Raw colour ramps for Indus Agent. OKLCH is the source of truth; every step also carries an
 * sRGB hex fallback computed from it (chroma is reduced until the colour fits the sRGB gamut).
 */

export const rampNames = ["river", "silt", "clay", "reed", "ochre", "kiln"] as const;
export type RampName = (typeof rampNames)[number];

export const rampSteps = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950] as const;
export type RampStep = (typeof rampSteps)[number];

export interface Oklch {
  /** Perceptual lightness, 0..1. */
  readonly l: number;
  /** Chroma, 0..~0.4. */
  readonly c: number;
  /** Hue angle in degrees. */
  readonly h: number;
}

export interface Swatch {
  readonly oklch: Oklch;
  /** `oklch(...)` CSS text. */
  readonly css: string;
  /** `#rrggbb` fallback. */
  readonly hex: string;
}

export type Ramp = Readonly<Record<RampStep, Swatch>>;

/** Lightness per step, light to dark; shared by every ramp so steps line up across hues. */
const STEP_LIGHTNESS: readonly number[] = [
  0.975, 0.94, 0.885, 0.81, 0.725, 0.635, 0.545, 0.46, 0.38, 0.3, 0.22,
];

/** Share of a ramp's peak chroma at each step: muted at the ends, fullest in the middle. */
const STEP_CHROMA: readonly number[] = [
  0.16, 0.3, 0.52, 0.76, 0.93, 1, 0.96, 0.86, 0.72, 0.58, 0.46,
];

interface RampSpec {
  readonly hue: number;
  readonly peakChroma: number;
}

const RAMP_SPECS: Readonly<Record<RampName, RampSpec>> = {
  river: { hue: 222, peakChroma: 0.125 },
  silt: { hue: 72, peakChroma: 0.016 },
  clay: { hue: 44, peakChroma: 0.14 },
  reed: { hue: 152, peakChroma: 0.135 },
  ochre: { hue: 82, peakChroma: 0.145 },
  kiln: { hue: 24, peakChroma: 0.185 },
};

function linearToSrgb(x: number): number {
  return x <= 0.0031308 ? 12.92 * x : 1.055 * x ** (1 / 2.4) - 0.055;
}

/** OKLCH to linear-light sRGB channels (may fall outside 0..1 when out of gamut). */
function oklchToLinearRgb({ l, c, h }: Oklch): [number, number, number] {
  const rad = (h * Math.PI) / 180;
  const a = c * Math.cos(rad);
  const b = c * Math.sin(rad);
  const lp = (l + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const mp = (l - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const sp = (l - 0.0894841775 * a - 1.291485548 * b) ** 3;
  return [
    4.0767416621 * lp - 3.3077115913 * mp + 0.2309699292 * sp,
    -1.2684380046 * lp + 2.6097574011 * mp - 0.3413193965 * sp,
    -0.0041960863 * lp - 0.7034186147 * mp + 1.707614701 * sp,
  ];
}

function inGamut(rgb: readonly number[]): boolean {
  const eps = 1e-6;
  return rgb.every((v) => v >= -eps && v <= 1 + eps);
}

/** Largest chroma at or below `colour.c` that stays inside sRGB, keeping lightness and hue. */
export function fitToSrgb(colour: Oklch): Oklch {
  if (inGamut(oklchToLinearRgb(colour))) return colour;
  let lo = 0;
  let hi = colour.c;
  for (let i = 0; i < 32; i++) {
    const mid = (lo + hi) / 2;
    if (inGamut(oklchToLinearRgb({ ...colour, c: mid }))) lo = mid;
    else hi = mid;
  }
  return { ...colour, c: lo };
}

function channelToHex(v: number): string {
  const clamped = Math.min(1, Math.max(0, linearToSrgb(Math.min(1, Math.max(0, v)))));
  return Math.round(clamped * 255)
    .toString(16)
    .padStart(2, "0");
}

/** `#rrggbb` for an OKLCH colour, gamut-fitted first. */
export function oklchToHex(colour: Oklch): string {
  const [r, g, b] = oklchToLinearRgb(fitToSrgb(colour));
  return `#${channelToHex(r)}${channelToHex(g)}${channelToHex(b)}`;
}

function round(value: number, digits: number): number {
  const f = 10 ** digits;
  return Math.round(value * f) / f;
}

/** CSS `oklch()` text; `alpha` below 1 adds a slash alpha. */
export function oklchCss(colour: Oklch, alpha = 1): string {
  const body = `${round(colour.l * 100, 2)}% ${round(colour.c, 4)} ${round(colour.h, 1)}`;
  return alpha < 1 ? `oklch(${body} / ${alpha})` : `oklch(${body})`;
}

export function makeSwatch(colour: Oklch): Swatch {
  const fitted = fitToSrgb(colour);
  return { oklch: fitted, css: oklchCss(fitted), hex: oklchToHex(fitted) };
}

function buildRamp(spec: RampSpec): Ramp {
  const entries = rampSteps.map((step, i) => {
    const l = STEP_LIGHTNESS[i] ?? 0;
    const c = spec.peakChroma * (STEP_CHROMA[i] ?? 0);
    return [step, makeSwatch({ l, c, h: spec.hue })] as const;
  });
  return Object.fromEntries(entries) as Record<RampStep, Swatch>;
}

export const palette: Readonly<Record<RampName, Ramp>> = Object.fromEntries(
  rampNames.map((name) => [name, buildRamp(RAMP_SPECS[name])]),
) as Record<RampName, Ramp>;

/** Pure white and black, for text on strong fills and for overlays. */
export const white: Swatch = makeSwatch({ l: 1, c: 0, h: 0 });
export const black: Swatch = makeSwatch({ l: 0, c: 0, h: 0 });
