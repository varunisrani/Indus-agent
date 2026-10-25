import { contrastRatio } from "@indus/ui";
import { describe, expect, it } from "vitest";
import {
  deskColourNames,
  deskFromPresetCss,
  deskPalettes,
  deskSchemeCss,
  deskSizes,
  deskTokenCss,
} from "./deskTokens.ts";

const HEX = /^#[0-9a-f]{6}$/;

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
      expect(contrastRatio(p["send-fg"], p["send-bg"])).toBeGreaterThanOrEqual(7);
      expect(contrastRatio(p.danger, p["bg-canvas"])).toBeGreaterThanOrEqual(4.5);
    },
  );

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
