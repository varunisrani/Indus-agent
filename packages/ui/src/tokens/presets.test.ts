import { describe, expect, it } from "vitest";
import {
  CONTRAST_PAIRS,
  contrastFailures,
  DEFAULT_PRESET,
  presetIds,
  presets,
  presetTheme,
  themeCss,
  UserThemeError,
  userTheme,
} from "./presets.ts";
import { type SemanticName, semanticNames } from "./semantic.ts";

/** Our own WCAG computation, independent of the module under test. */
function luminance(hex: string): number {
  const n = Number.parseInt(hex.slice(1, 7), 16);
  const lin = (c: number) => {
    const v = c / 255;
    return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin((n >> 16) & 255) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255);
}
function ratio(a: string, b: string): number {
  const x = luminance(a);
  const y = luminance(b);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

const AA: readonly (readonly [SemanticName, SemanticName, number])[] = [
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

describe("theme presets", () => {
  it("are twelve, with our own unique names", () => {
    expect(presetIds).toEqual([
      "indus",
      "monsoon",
      "laterite",
      "saffron",
      "indigo",
      "basalt",
      "lotus",
      "mangrove",
      "dune",
      "harbour",
      "terrace",
      "jade",
    ]);
    expect(new Set(presets.map((p) => p.label)).size).toBe(12);
    expect(presetIds).toContain(DEFAULT_PRESET);
    expect(CONTRAST_PAIRS).toEqual(AA);
  });

  for (const id of presetIds) {
    for (const mode of ["light", "dark"] as const) {
      it(`${id} ${mode} passes WCAG AA`, () => {
        const theme = presetTheme(id, mode);
        expect(Object.keys(theme).sort()).toEqual([...semanticNames].sort());
        for (const [fg, bg, min] of AA) {
          expect(ratio(theme[fg].hex, theme[bg].hex), `${fg} on ${bg}`).toBeGreaterThanOrEqual(min);
        }
        expect(contrastFailures(theme)).toEqual([]);
        for (const name of semanticNames) {
          expect(theme[name].hex).toMatch(
            name === "selection" ? /^#[0-9a-f]{8}$/ : /^#[0-9a-f]{6}$/,
          );
        }
      });
    }
  }

  it("light and dark are different, and the same input gives the same colours", () => {
    const a = presetTheme("monsoon", "light");
    expect(presetTheme("monsoon", "light")).toEqual(a);
    expect(presetTheme("monsoon", "dark").bg.hex).not.toBe(a.bg.hex);
    expect(luminance(presetTheme("monsoon", "dark").bg.hex)).toBeLessThan(luminance(a.bg.hex));
  });

  it("accent overrides keep AA for every hue", () => {
    for (let hue = 0; hue < 360; hue += 15) {
      for (const mode of ["light", "dark"] as const) {
        const theme = presetTheme("basalt", mode, { accentHue: hue });
        expect(contrastFailures(theme), `hue ${hue} ${mode}`).toEqual([]);
      }
    }
    expect(presetTheme("indus", "light", { accentHue: 400 })).toEqual(
      presetTheme("indus", "light", { accentHue: 40 }),
    );
  });

  it("an unknown id falls back to the default preset", () => {
    expect(presetTheme("nope", "dark")).toEqual(presetTheme(DEFAULT_PRESET, "dark"));
  });

  it("user themes take #rrggbb only and are checked like presets", () => {
    const base = presetTheme("jade", "light");
    const hexes = Object.fromEntries(
      semanticNames.map((n) => [n, base[n].hex.slice(0, 7)]),
    ) as Record<SemanticName, string>;
    const made = userTheme({ id: "mine", label: "Mine", light: hexes, dark: hexes });
    expect(made.light.fg.hex).toBe(hexes.fg);
    expect(() =>
      userTheme({ id: "bad", label: "Bad", light: { ...hexes, fg: "red" }, dark: hexes }),
    ).toThrow(UserThemeError);
    const poor = userTheme({
      id: "poor",
      label: "Poor",
      light: { ...hexes, fg: "#eeeeee" },
      dark: hexes,
    });
    expect(contrastFailures(poor.light)).toContain("fg on bg below 4.5:1");
  });

  it("writes only colour variables", () => {
    const css = themeCss(presetTheme("dune", "dark"), ':root[data-preset="dune"]');
    const names = [...css.matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1] ?? "");
    expect(names.length).toBe(semanticNames.length * 2);
    expect(names.every((n) => n.startsWith("--indus-color-"))).toBe(true);
    expect(css).toContain("@supports (color: oklch(0% 0 0))");
  });
});
