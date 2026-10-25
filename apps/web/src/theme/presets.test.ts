import { readFileSync } from "node:fs";
import { join } from "node:path";
import { BRAND_COLORS } from "@indus/ui";
import { describe, expect, it } from "vitest";
import {
  accentOf,
  DARK_DEFAULT,
  initialPreset,
  LIGHT_DEFAULT,
  onAccentOf,
  PRESETS,
  presetById,
  presetCss,
} from "./presets.ts";

describe("theme presets", () => {
  it("keeps a stored preset", () => {
    expect(initialPreset("graphite", false).id).toBe("graphite");
  });

  it("follows the system side when nothing usable is stored", () => {
    expect(initialPreset(null, true).id).toBe(DARK_DEFAULT);
    expect(initialPreset(null, false).id).toBe(LIGHT_DEFAULT);
    expect(initialPreset("no-such-preset", false).id).toBe(LIGHT_DEFAULT);
  });

  it("maps the earlier light/dark choice onto the default presets", () => {
    expect(initialPreset("indus-light", true).id).toBe(LIGHT_DEFAULT);
    expect(initialPreset("indus-dark", false).id).toBe(DARK_DEFAULT);
  });

  it("offers at least one light and one dark preset, with unique ids", () => {
    expect(new Set(PRESETS.map((p) => p.id)).size).toBe(PRESETS.length);
    expect(PRESETS.some((p) => p.scheme === "indus-light")).toBe(true);
    expect(PRESETS.some((p) => p.scheme === "indus-dark")).toBe(true);
  });

  it("writes one rule per preset with its colour pair", () => {
    const css = presetCss();
    for (const p of PRESETS) {
      expect(css).toContain(`:root[data-preset="${p.id}"]`);
      expect(css).toContain(`--indus-canvas: ${p.canvas};`);
    }
  });

  it("starts on the Indus presets (W63)", () => {
    expect(DARK_DEFAULT).toBe("indus-ember");
    expect(LIGHT_DEFAULT).toBe("indus-daylight");
    const ember = presetById("indus-ember");
    expect(ember).toMatchObject({
      scheme: "indus-dark",
      canvas: BRAND_COLORS.black,
      accent: BRAND_COLORS.ember,
      onAccent: BRAND_COLORS.black,
      rootPx: 16,
    });
    expect(presetById("indus-ember-large")).toMatchObject({
      ...{ canvas: ember?.canvas, ink: ember?.ink, accent: ember?.accent },
      rootPx: 18,
    });
    expect(presetById("indus-daylight")).toMatchObject({
      scheme: "indus-light",
      ink: BRAND_COLORS.black,
      onAccent: BRAND_COLORS.white,
    });
  });

  it("gives every preset an accent pair, the ink and canvas when it names none", () => {
    const css = presetCss();
    for (const p of PRESETS) {
      const rule = css.split("\n").find((line) => line.includes(`"${p.id}"`)) ?? "";
      expect(rule).toContain(`--indus-accent: ${accentOf(p)};`);
      expect(rule).toContain(`--indus-on-accent: ${onAccentOf(p)};`);
      if (p.accent) {
        expect(rule).toContain(`--indus-brand-accent: ${p.accent};`);
      } else {
        // The presets from before the rebrand paint exactly as they did.
        expect(accentOf(p)).toBe(p.ink);
        expect(onAccentOf(p)).toBe(p.canvas);
        expect(rule).not.toContain("--indus-brand-accent");
      }
    }
  });

  it("keeps the earlier presets selectable", () => {
    for (const id of ["deep-teal", "deep-teal-large", "clear-blue", "graphite", "sandstone"])
      expect(initialPreset(id, true).id).toBe(id);
  });
});

// -- WCAG AA text contrast (W56-G2-1) -----------------------------------------------------------
// Every surface and ink token is `color-mix(in srgb, ink N%, canvas)` (index.css), so the colours a
// preset really paints can be worked out from its pair. The mixes are read from index.css itself so
// a change there is measured too. `color-mix(in srgb)` interpolates the encoded channels.

type Rgb = [number, number, number];

function hexRgb(hex: string): Rgb {
  const n = Number.parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function mix(ink: Rgb, canvas: Rgb, inkShare: number): Rgb {
  return ink.map((c, i) => c * inkShare + (canvas[i] as number) * (1 - inkShare)) as Rgb;
}

function luminance([r, g, b]: Rgb): number {
  const lin = (c: number) => {
    const s = c / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

function contrast(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

const INDEX_CSS = readFileSync(join(import.meta.dirname, "..", "index.css"), "utf8");

/** The ink share of `--indus-mix-<name>`, from index.css. */
function inkShare(name: string): number {
  const rule = new RegExp(
    `--indus-mix-${name}:\\s*color-mix\\(in srgb, var\\(--indus-ink\\) (\\d+)%, var\\(--indus-canvas\\)\\)`,
  ).exec(INDEX_CSS);
  if (!rule) throw new Error(`index.css has no --indus-mix-${name} mix of ink into canvas`);
  return Number(rule[1]) / 100;
}

const AA_TEXT = 4.5;
/** Non-text contrast for controls, focus indicators and graphics (WCAG 1.4.11). */
const AA_UI = 3;
/** Body text: the ink and its muted and soft mixes. */
const TEXT_TOKENS = {
  ink: 1,
  "ink-muted": inkShare("ink-muted"),
  "ink-soft": inkShare("ink-soft"),
};
/** Where text sits: the page, raised cards and inputs. */
const GROUNDS = { canvas: 0, surface: inkShare("surface"), wash: inkShare("wash") };
/** Rows that are hovered or picked keep their main and muted text readable too. */
const ROW_GROUNDS = { hover: inkShare("hover"), selected: inkShare("selected") };
/**
 * Clear Blue mirrors the reference's light look (blue ink on a sky canvas, 5.07:1 at full ink); its
 * muted and soft mixes fall below AA. It keeps that look and is held only to its ink on the canvas.
 */
const REFERENCE_LOOK = new Set(["clear-blue"]);

function shortfalls(preset: (typeof PRESETS)[number]): string[] {
  const ink = hexRgb(preset.ink);
  const canvas = hexRgb(preset.canvas);
  const failed: string[] = [];
  const check = (text: string, share: number, ground: string, groundShare: number) => {
    const ratio = contrast(mix(ink, canvas, share), mix(ink, canvas, groundShare));
    if (ratio < AA_TEXT) failed.push(`${text} on ${ground}: ${ratio.toFixed(2)}`);
  };
  for (const [text, share] of Object.entries(TEXT_TOKENS))
    for (const [ground, groundShare] of Object.entries(GROUNDS))
      check(text, share, ground, groundShare);
  for (const [ground, groundShare] of Object.entries(ROW_GROUNDS)) {
    check("ink", 1, ground, groundShare);
    check("ink-muted", TEXT_TOKENS["ink-muted"], ground, groundShare);
  }
  return failed;
}

describe("preset contrast", () => {
  it("offers eleven presets: the three Indus ones and the eight before them", () => {
    expect(PRESETS).toHaveLength(11);
    expect(PRESETS.filter((p) => p.scheme === "indus-light")).toHaveLength(5);
    expect(PRESETS.filter((p) => p.scheme === "indus-dark")).toHaveLength(6);
    expect(PRESETS.filter((p) => p.accent).map((p) => p.id)).toEqual([
      "indus-ember",
      "indus-ember-large",
      "indus-daylight",
    ]);
  });

  it("measures black on white as 21:1 and equal colours as 1:1", () => {
    expect(contrast(hexRgb("#000000"), hexRgb("#ffffff"))).toBeCloseTo(21, 5);
    expect(contrast(hexRgb("#777777"), hexRgb("#777777"))).toBe(1);
  });

  it.each(
    PRESETS.filter((p) => !REFERENCE_LOOK.has(p.id)).map((p) => [p.id, p.scheme, p] as const),
  )("%s (%s) keeps every text token at WCAG AA", (_id, _scheme, preset) => {
    expect(shortfalls(preset)).toEqual([]);
  });

  it("holds the reference-look preset to AA for its ink on the canvas", () => {
    for (const id of REFERENCE_LOOK) {
      const preset = PRESETS.find((p) => p.id === id);
      if (!preset) throw new Error(`no preset ${id}`);
      expect(contrast(hexRgb(preset.ink), hexRgb(preset.canvas))).toBeGreaterThanOrEqual(AA_TEXT);
      // Still below AA on its mixes; when that changes, drop it from REFERENCE_LOOK.
      expect(shortfalls(preset).length).toBeGreaterThan(0);
    }
  });

  it("filled controls (canvas text on ink) clear AA in every preset", () => {
    for (const preset of PRESETS)
      expect(contrast(hexRgb(preset.canvas), hexRgb(preset.ink))).toBeGreaterThanOrEqual(AA_TEXT);
  });

  // The accent (W63) paints marks, rules, focus rings and picked controls (WCAG 1.4.11: 3:1
  // against what sits next to it) and is also link and badge text (1.4.3: 4.5:1 on the page).
  it.each(PRESETS.map((p) => [p.id, p] as const))(
    "%s: the accent clears 3:1 as UI on every ground and AA as text on the canvas",
    (_id, preset) => {
      const ink = hexRgb(preset.ink);
      const canvas = hexRgb(preset.canvas);
      const accent = hexRgb(accentOf(preset));
      for (const share of [...Object.values(GROUNDS), ...Object.values(ROW_GROUNDS)])
        expect(contrast(accent, mix(ink, canvas, share))).toBeGreaterThanOrEqual(AA_UI);
      expect(contrast(accent, canvas)).toBeGreaterThanOrEqual(AA_TEXT);
    },
  );

  it.each(PRESETS.map((p) => [p.id, p] as const))(
    "%s: text on the accent (filled buttons, picked tabs) clears AA",
    (_id, preset) => {
      expect(contrast(hexRgb(onAccentOf(preset)), hexRgb(accentOf(preset)))).toBeGreaterThanOrEqual(
        AA_TEXT,
      );
    },
  );

  it("measures the Indus presets' accents (W63)", () => {
    const ember = presetById("indus-ember");
    const daylight = presetById("indus-daylight");
    if (!ember || !daylight) throw new Error("no Indus presets");
    // White on the mark's ember falls short of AA, which is why the lockup black sits on it.
    expect(contrast(hexRgb(BRAND_COLORS.white), hexRgb(BRAND_COLORS.ember))).toBeLessThan(AA_TEXT);
    expect(contrast(hexRgb(accentOf(ember)), hexRgb(ember.canvas))).toBeCloseTo(5.19, 1);
    expect(contrast(hexRgb(onAccentOf(ember)), hexRgb(accentOf(ember)))).toBeCloseTo(5.19, 1);
    expect(contrast(hexRgb(accentOf(daylight)), hexRgb(daylight.canvas))).toBeCloseTo(5.17, 1);
    expect(contrast(hexRgb(onAccentOf(daylight)), hexRgb(accentOf(daylight)))).toBeCloseTo(5.57, 1);
  });
});
