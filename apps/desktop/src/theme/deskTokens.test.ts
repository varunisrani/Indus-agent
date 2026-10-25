import { BRAND_COLORS, contrastRatio } from "@indus/ui";
import { describe, expect, it } from "vitest";
import {
  BRAND_ACCENT_HUE,
  deskColourNames,
  deskFromPresetCss,
  deskPalettes,
  deskSchemeCss,
  deskSizes,
  deskTokenCss,
} from "./deskTokens.ts";

const HEX = /^#[0-9a-f]{6}$/;

/** Hue of a `#rrggbb` colour in degrees. */
function hue(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((at) => Number.parseInt(hex.slice(at, at + 2), 16) / 255) as [
    number,
    number,
    number,
  ];
  const max = Math.max(r, g, b);
  const d = max - Math.min(r, g, b);
  if (d === 0) return 0;
  const h = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return (h * 60 + 360) % 360;
}

function hueDistance(a: string, b: string): number {
  const d = Math.abs(hue(a) - hue(b));
  return Math.min(d, 360 - d);
}

describe("desk tokens", () => {
  it.each(["light", "dark"] as const)(
    "%s defines every colour, all solid ones as hex",
    (scheme) => {
      const palette = deskPalettes[scheme];
      for (const name of deskColourNames) {
        const value = palette[name];
        expect(value, name).toBeTruthy();
        if (!value.startsWith("rgb(")) expect(value, name).toMatch(HEX);
      }
    },
  );

  it.each(["light", "dark"] as const)(
    "%s text reads on the canvas, the band and the bubble",
    (scheme) => {
      const p = deskPalettes[scheme];
      for (const surface of [
        "bg-canvas",
        "bg-band",
        "bg-bubble",
        "bg-input",
        "bg-raised",
      ] as const) {
        expect(contrastRatio(p["fg-1"], p[surface]), `fg-1 on ${surface}`).toBeGreaterThanOrEqual(
          7,
        );
        expect(contrastRatio(p["fg-2"], p[surface]), `fg-2 on ${surface}`).toBeGreaterThanOrEqual(
          4.5,
        );
        expect(contrastRatio(p["fg-3"], p[surface]), `fg-3 on ${surface}`).toBeGreaterThanOrEqual(
          3,
        );
      }
      expect(contrastRatio(p["fg-1"], p["row-active"])).toBeGreaterThanOrEqual(7);
      expect(contrastRatio(p["accent-fg"], p.accent)).toBeGreaterThanOrEqual(4.5);
      // the send button's glyph on the ember fill: AA, as for text (ember cannot reach 7:1)
      expect(contrastRatio(p["send-fg"], p["send-bg"])).toBeGreaterThanOrEqual(4.5);
      expect(contrastRatio(p.danger, p["bg-canvas"])).toBeGreaterThanOrEqual(4.5);
    },
  );

  it.each(["light", "dark"] as const)(
    "%s accent reads as text, and the states read and stay apart from it",
    (scheme) => {
      const p = deskPalettes[scheme];
      for (const surface of ["bg-canvas", "bg-band", "bg-raised", "accent-soft"] as const) {
        expect(contrastRatio(p.accent, p[surface]), `accent on ${surface}`).toBeGreaterThanOrEqual(
          4.5,
        );
      }
      // the mark is a graphic: 3:1 against the canvas and the band (WCAG non-text contrast)
      expect(contrastRatio(p.wordmark, p["bg-canvas"])).toBeGreaterThanOrEqual(3);
      expect(contrastRatio(p.wordmark, p["bg-band"])).toBeGreaterThanOrEqual(3);
      for (const state of ["danger", "warning", "success", "attention"] as const) {
        expect(
          contrastRatio(p[state], p["bg-canvas"]),
          `${state} on bg-canvas`,
        ).toBeGreaterThanOrEqual(4.5);
        // an approval waiting (attention) or a failure must never look like the brand accent
        expect(hueDistance(p[state], p.accent), `${state} vs accent`).toBeGreaterThanOrEqual(20);
      }
    },
  );

  it("paints the brand: the ember mark on both, ember accent on the brand black (Indus Ember)", () => {
    const { dark, light } = deskPalettes;
    expect(dark["bg-canvas"]).toBe(BRAND_COLORS.black);
    expect(dark.accent).toBe(BRAND_COLORS.ember);
    expect(dark["accent-fg"]).toBe(BRAND_COLORS.black);
    expect(dark.wordmark).toBe(BRAND_COLORS.ember);
    expect(light.wordmark).toBe(BRAND_COLORS.ember);
    expect(dark["send-bg"]).toBe(BRAND_COLORS.ember);
    // daylight takes a deeper ember: the brand one is under AA for text on paper
    expect(contrastRatio(BRAND_COLORS.ember, light["bg-canvas"])).toBeLessThan(4.5);
    expect(hueDistance(light.accent, BRAND_COLORS.ember)).toBeLessThanOrEqual(3);
    expect(light["send-bg"]).toBe(light.accent);
    expect(Math.abs(hue(BRAND_COLORS.ember) - BRAND_ACCENT_HUE)).toBeLessThan(1);
  });

  it("writes both schemes, with the older names pointed at the desk ones", () => {
    const css = deskTokenCss();
    expect(css).toContain(':root[data-theme="indus-light"] {');
    expect(css).toContain(':root[data-theme="indus-dark"] {');
    const dark = deskSchemeCss("dark");
    for (const name of deskColourNames) expect(dark).toContain(`--desk-${name}: `);
    for (const name of Object.keys(deskSizes)) expect(dark).toContain(`--desk-${name}: `);
    expect(dark).toContain("--indus-color-bg: var(--desk-bg-canvas);");
    expect(dark).toContain("--indus-color-accent-fg: var(--desk-accent-fg);");
  });

  it("derives the desk surfaces from a preset without pointing back at itself", () => {
    const css = deskFromPresetCss("light");
    expect(css.startsWith(":root:root:root {")).toBe(true);
    expect(css).toContain("--desk-bg-canvas: var(--indus-color-bg);");
    // a preset rule never reads a desk token (the older names would loop back)
    expect(css).not.toMatch(/var\(--desk-/);
  });
});
