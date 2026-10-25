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
