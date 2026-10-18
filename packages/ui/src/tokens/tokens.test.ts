import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { ansi, ansiNames } from "./ansi.ts";
import { cssPrefix, tokensToCss } from "./css.ts";
import { durations, motionDurations } from "./motion.ts";
import { palette, rampNames, rampSteps } from "./palette.ts";
import {
  type SemanticName,
  semanticNames,
  type ThemeName,
  themeNames,
  themes,
} from "./semantic.ts";
import { radii, shadows, space } from "./space.ts";
import { fontStacks, typeScale } from "./type.ts";

const HEX6 = /^#[0-9a-f]{6}$/;

function srgbToLinear(channel: number): number {
  const v = channel / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}

function hexChannels(hex: string): [number, number, number] {
  const n = Number.parseInt(hex.slice(1, 7), 16);
  return [(n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff];
}

/** WCAG 2.x relative luminance. */
function luminance(hex: string): number {
  const [r, g, b] = hexChannels(hex).map(srgbToLinear) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

/** Published OKLab constants: linear sRGB rows to LMS cone response, then the L row of LMS' to Lab. */
const LINEAR_RGB_TO_LMS = [
  [0.4122214708, 0.5363325363, 0.0514459929],
  [0.2119034982, 0.6806995451, 0.1073969566],
  [0.0883024619, 0.2817188376, 0.6299787005],
] as const;
const LMS_PRIME_TO_LIGHTNESS = [0.2104542553, 0.793617785, -0.0040720468] as const;

const dot = (row: readonly number[], v: readonly number[]): number =>
  row.reduce((sum, k, i) => sum + k * (v[i] ?? 0), 0);

/** OKLab lightness straight from hex, independent of the code that produced the hex. */
function hexToOklabL(hex: string): number {
  const linear = hexChannels(hex).map(srgbToLinear);
  const conesPrime = LINEAR_RGB_TO_LMS.map((row) => Math.cbrt(dot(row, linear)));
  return dot(LMS_PRIME_TO_LIGHTNESS, conesPrime);
}

const TEXT_PAIRS: readonly (readonly [SemanticName, SemanticName, number])[] = [
  ["fg", "bg", 4.5],
  ["fg", "bg-raised", 4.5],
  ["accent-fg", "accent", 4.5],
  ["fg-muted", "bg", 4.5],
  ["fg-muted", "bg-raised", 4.5],
  ["fg", "code-bg", 4.5],
  ["border", "bg", 3.0],
  ["success", "bg", 3.0],
  ["warning", "bg", 3.0],
  ["danger", "bg", 3.0],
  ["focus-ring", "bg", 3.0],
];

describe.each(themeNames)("theme %s", (theme: ThemeName) => {
  const colours = themes[theme];

  it("defines every semantic token", () => {
    expect(Object.keys(colours).sort()).toEqual([...semanticNames].sort());
  });

  it.each(TEXT_PAIRS)("%s on %s reaches %f:1", (fg, bg, minimum) => {
    expect(contrast(colours[fg].hex, colours[bg].hex)).toBeGreaterThanOrEqual(minimum);
  });

  it("keeps translucency only on selection", () => {
    for (const name of semanticNames) {
      const pattern = name === "selection" ? /^#[0-9a-f]{8}$/ : HEX6;
      expect(colours[name].hex, name).toMatch(pattern);
    }
  });

  it("emits stable CSS with only --indus- custom properties", () => {
    const css = tokensToCss(theme);
    expect(css).toMatchSnapshot();
    const names = [...css.matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]);
    expect(names.length).toBeGreaterThan(semanticNames.length);
    for (const name of names) expect(name?.startsWith(cssPrefix), name).toBe(true);
    expect(css).toContain(`:root[data-theme="${theme}"]`);
  });

  it("zeroes durations under reduced motion", () => {
    const css = tokensToCss(theme);
    const reduced = css.slice(css.indexOf("@media (prefers-reduced-motion: reduce)"));
    for (const name of Object.keys(durations)) {
      expect(reduced).toContain(`--indus-duration-${name}: 0ms;`);
    }
  });

  it("maps exactly 16 ANSI colours to valid hex", () => {
    const map = ansi[theme];
    expect(Object.keys(map)).toEqual([...ansiNames]);
    expect(Object.keys(map)).toHaveLength(16);
    for (const value of Object.values(map)) expect(value).toMatch(HEX6);
  });

  it("has three shadow levels", () => {
    expect(Object.keys(shadows[theme]).sort()).toEqual(["high", "low", "mid"]);
  });
});

describe("palette", () => {
  it.each(rampNames)("%s has 11 steps, each darker than the last", (name) => {
    const ramp = palette[name];
    expect(Object.keys(ramp)).toHaveLength(11);
    let prevL = Number.POSITIVE_INFINITY;
    let prevY = Number.POSITIVE_INFINITY;
    for (const step of rampSteps) {
      const swatch = ramp[step];
      expect(swatch.hex).toMatch(HEX6);
      expect(swatch.oklch.l).toBeLessThan(prevL);
      expect(luminance(swatch.hex)).toBeLessThan(prevY);
      prevL = swatch.oklch.l;
      prevY = luminance(swatch.hex);
    }
  });

  it("hex fallbacks match their OKLCH lightness", () => {
    for (const name of rampNames) {
      for (const step of rampSteps) {
        const swatch = palette[name][step];
        expect(Math.abs(hexToOklabL(swatch.hex) - swatch.oklch.l), `${name}.${step}`).toBeLessThan(
          0.005,
        );
      }
    }
  });

  it("rejects an unknown theme at runtime", () => {
    expect(() => tokensToCss("nope" as ThemeName)).toThrow(/unknown theme/);
  });
});

describe("scales", () => {
  it("type scale uses the agreed sizes", () => {
    expect(Object.values(typeScale).map((s) => s.size)).toEqual([12, 13, 14, 16, 18, 22, 28, 36]);
    for (const step of Object.values(typeScale)) {
      expect(step.lineHeight).toBeGreaterThan(step.size);
    }
  });

  it("font stacks lead with our families and end in a generic family", () => {
    expect(fontStacks.sans.startsWith('"Red Hat Text", system-ui')).toBe(true);
    expect(fontStacks.sans.endsWith("sans-serif")).toBe(true);
    expect(fontStacks.mono.startsWith('"Red Hat Mono", ui-monospace')).toBe(true);
    expect(fontStacks.mono.endsWith("monospace")).toBe(true);
  });

  it("spacing is a 4 px grid from 0 to 16", () => {
    expect(space).toHaveLength(17);
    space.forEach((px, n) => {
      expect(px).toBe(n * 4);
    });
  });

  it("radii and motion carry the agreed values", () => {
    expect(Object.values(radii)).toEqual(["0px", "4px", "6px", "10px", "16px", "9999px"]);
    expect(durations).toEqual({ quick: 120, base: 200, slow: 320 });
    expect(motionDurations(true)).toEqual({ quick: 0, base: 0, slow: 0 });
    expect(motionDurations(false)).toEqual(durations);
  });
});

describe("clean-room names", () => {
  interface BannedEntry {
    pattern: string;
    flags: string[];
  }
  const bannedFile = new URL("../../../../tooling/checks/banned_patterns.json", import.meta.url);
  const banned = (JSON.parse(readFileSync(bannedFile, "utf8")) as BannedEntry[]).map(
    (entry) => new RegExp(entry.pattern, entry.flags.includes("IGNORECASE") ? "i" : ""),
  );

  it("no token value or theme name matches the banned-name list", () => {
    const strings = [
      ...themeNames,
      ...rampNames,
      ...Object.values(fontStacks),
      ...themeNames.map((theme) => tokensToCss(theme)),
    ];
    expect(banned.length).toBeGreaterThan(0);
    for (const text of strings) {
      for (const pattern of banned) expect(pattern.test(text), `${pattern} in ${text}`).toBe(false);
    }
  });
});
