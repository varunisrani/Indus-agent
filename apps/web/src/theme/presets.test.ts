import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { DARK_DEFAULT, initialPreset, LIGHT_DEFAULT, PRESETS, presetCss } from "./presets.ts";

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
  it("offers eight presets, four light and four dark", () => {
    expect(PRESETS).toHaveLength(8);
    expect(PRESETS.filter((p) => p.scheme === "indus-light")).toHaveLength(4);
    expect(PRESETS.filter((p) => p.scheme === "indus-dark")).toHaveLength(4);
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
});
