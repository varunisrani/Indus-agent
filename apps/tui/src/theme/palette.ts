// Colour arithmetic for the terminal theme: hex parsing, WCAG contrast, mixing, and lifting a
// colour away from the background until it is readable.

export type Rgb = readonly [number, number, number];

const HEX6 = /^#?([0-9a-f]{6})$/i;
const HEX3 = /^#?([0-9a-f]{3})$/i;

/** `#rgb` / `#rrggbb` (hash optional) → channels 0..255, or null for anything else. */
export function parseHex(value: string): Rgb | null {
  const text = value.trim();
  const long = HEX6.exec(text);
  if (long?.[1] !== undefined) {
    const n = Number.parseInt(long[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  const short = HEX3.exec(text);
  if (short?.[1] !== undefined) {
    const [r, g, b] = [...short[1]].map((d) => Number.parseInt(d + d, 16));
    return [r ?? 0, g ?? 0, b ?? 0];
  }
  return null;
}

export function toHex([r, g, b]: Rgb): string {
  const part = (v: number): string =>
    Math.round(Math.min(255, Math.max(0, v)))
      .toString(16)
      .padStart(2, "0");
  return `#${part(r)}${part(g)}${part(b)}`;
}

/** A normalised `#rrggbb`, or null when the text is not a hex colour. */
export function normaliseHex(value: string): string | null {
  const rgb = parseHex(value);
  return rgb === null ? null : toHex(rgb);
}

function channelLinear(v: number): number {
  const s = v / 255;
  return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}

/** WCAG relative luminance, 0 (black) .. 1 (white). */
export function luminance(hex: string): number {
  const rgb = parseHex(hex);
  if (rgb === null) return 0;
  const [r, g, b] = rgb.map(channelLinear) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** WCAG contrast ratio between two colours, 1..21. */
export function contrast(a: string, b: string): number {
  const la = luminance(a);
  const lb = luminance(b);
  const [hi, lo] = la >= lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

/** `amount` 0 keeps `from`, 1 gives `to`; mixed per sRGB channel. */
export function mix(from: string, to: string, amount: number): string {
  const a = parseHex(from) ?? [0, 0, 0];
  const b = parseHex(to) ?? [0, 0, 0];
  const t = Math.min(1, Math.max(0, amount));
  const blend = (i: 0 | 1 | 2): number => a[i] * (1 - t) + b[i] * t;
  return toHex([blend(0), blend(1), blend(2)]);
}

/**
 * Move `colour` toward white (dark background) or black (light background) in small steps until it
 * reaches `floor` against `background`. A colour already past the floor is returned unchanged.
 */
export function liftToContrast(colour: string, background: string, floor: number): string {
  if (contrast(colour, background) >= floor) return colour;
  const target = luminance(background) > 0.18 ? "#000000" : "#ffffff";
  for (let step = 1; step <= 40; step++) {
    const candidate = mix(colour, target, step / 40);
    if (contrast(candidate, background) >= floor) return candidate;
  }
  return target;
}

/**
 * Pull a panel fill back toward the background until `ink` on it reaches `floor`. The background
 * itself always qualifies because body text was lifted against it first.
 */
export function fillFor(fill: string, background: string, ink: string, floor: number): string {
  if (contrast(ink, fill) >= floor) return fill;
  for (let step = 1; step <= 20; step++) {
    const candidate = mix(fill, background, step / 20);
    if (contrast(ink, candidate) >= floor) return candidate;
  }
  return background;
}
