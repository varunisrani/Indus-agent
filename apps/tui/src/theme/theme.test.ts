import { EventEmitter } from "node:events";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { render } from "ink";
import { createElement as h, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { bootCachePath, bootTheme, readBootTheme, writeBootTheme } from "./bootCache.ts";
import { contrast } from "./palette.ts";
import {
  applyThemePayload,
  buildTheme,
  cancelPreview,
  commitPreview,
  contrastReport,
  currentTheme,
  previewTheme,
  setDetected,
  type Theme,
  ThemeProvider,
  themeStore,
  useTheme,
} from "./theme.ts";

let dir = "";
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "indus-theme-"));
  setDetected(false, null);
  themeStore.setState({ theme: buildTheme({ light: false }), preview: null });
});
afterEach(() => rmSync(dir, { recursive: true, force: true }));

function assertFloors(theme: Theme): void {
  const failing = contrastReport(theme).filter((row) => row.ratio + 1e-9 < row.floor);
  expect(failing).toEqual([]);
}

describe("palettes", () => {
  it("meets the contrast floor for every token pair in both defaults", () => {
    for (const light of [false, true]) {
      const theme = buildTheme({ light });
      expect(contrastReport(theme).length).toBeGreaterThan(20);
      assertFloors(theme);
    }
  });

  it("still meets the floors on backgrounds the terminal reports", () => {
    for (const [light, background] of [
      [false, "#2b2b2b"],
      [false, "#0a0f1e"],
      [true, "#f2efe6"],
      [true, "#e4e4e4"],
    ] as const) {
      assertFloors(buildTheme({ light, background }));
    }
  });

  it("lifts payload colours that would be unreadable", () => {
    const theme = buildTheme({ light: false, colors: { accent: "#202020", text: "#333333" } });
    expect(contrast(theme.color.accent ?? "", theme.background)).toBeGreaterThanOrEqual(3);
    assertFloors(theme);
  });

  it("uses a clay accent and our own brand marks", () => {
    const dark = buildTheme({ light: false });
    expect(dark.brand.name).toBe("Indus Agent");
    expect(dark.brand.prompt).toBe("›");
    expect(dark.brand.accent).toBe(dark.color.accent);
    const [r, , b] = [1, 3, 5].map((i) =>
      Number.parseInt((dark.color.accent ?? "").slice(i, i + 2), 16),
    );
    expect(r ?? 0).toBeGreaterThan(b ?? 0);
  });
});

describe("server payloads and preview", () => {
  it("merges a payload over the detected default", () => {
    setDetected(true, "#f5f5f5");
    const theme = applyThemePayload({
      name: "harbour",
      colors: { secondary: "#00897b", bogus: "nope" },
      brand: { prompt: "  >>  " },
    });
    expect(theme.light).toBe(true);
    expect(theme.name).toBe("harbour");
    expect(theme.color.bogus).toBeUndefined();
    expect(theme.brand.prompt).toBe(">>");
    expect(currentTheme()).toBe(theme);
  });

  it("resolves auto against detection and names to their polarity", () => {
    setDetected(true, null);
    expect(applyThemePayload("auto").light).toBe(true);
    expect(applyThemePayload("indus-dark").light).toBe(false);
    expect(applyThemePayload("dark").name).toBe("indus-dark");
  });

  it("previews live while browsing, then commits or reverts", () => {
    const before = currentTheme();
    expect(previewTheme("light")).toBe(true);
    expect(currentTheme().light).toBe(true);
    cancelPreview();
    expect(currentTheme()).toBe(before);
    expect(previewTheme("no-such-theme")).toBe(false);
    previewTheme("indus-light");
    commitPreview();
    expect(themeStore.getState().theme.light).toBe(true);
    expect(themeStore.getState().preview).toBeNull();
  });
});

describe("boot cache", () => {
  it("round-trips a resolved theme", () => {
    const path = join(dir, "tui", "theme-boot.json");
    const theme = buildTheme({ light: true, background: "#fbf7ee", colors: { accent: "#b4532a" } });
    expect(writeBootTheme(path, theme)).toBe(true);
    expect(readBootTheme(path)).toEqual(theme);
  });

  it("is read before the first render, so frame one paints the cached colours", () => {
    const path = join(dir, "theme-boot.json");
    const cached = buildTheme({
      light: true,
      background: "#fbf7ee",
      colors: { accent: "#9a3412" },
    });
    writeBootTheme(path, cached);
    bootTheme({}, path);
    const seen: string[] = [];
    const Probe = (): ReactNode => {
      seen.push(useTheme().color.accent ?? "");
      return null;
    };
    const out = Object.assign(new (class extends EventEmitter {})(), {
      write: () => true,
      columns: 80,
      rows: 24,
    });
    const app = render(h(Probe), {
      stdout: out as unknown as NodeJS.WriteStream,
      patchConsole: false,
    });
    app.unmount();
    expect(seen[0]).toBe(cached.color.accent);
  });

  it("ignores a corrupt or foreign cache file", () => {
    const path = join(dir, "theme-boot.json");
    writeFileSync(path, "{not json");
    expect(readBootTheme(path)).toBeNull();
    writeFileSync(path, JSON.stringify({ version: 99, theme: {} }));
    expect(readBootTheme(path)).toBeNull();
    expect(bootTheme({ COLORFGBG: "0;15" }, path).light).toBe(true);
  });

  it("lets an explicit env signal override a cache of the other polarity", () => {
    const path = join(dir, "theme-boot.json");
    writeBootTheme(path, buildTheme({ light: false }));
    expect(bootTheme({ INDUS_TUI_LIGHT: "1" }, path).light).toBe(true);
  });

  it("lives under INDUS_HOME", () => {
    expect(bootCachePath({ INDUS_HOME: dir })).toBe(join(dir, "tui", "theme-boot.json"));
    writeBootTheme(bootCachePath({ INDUS_HOME: dir }), buildTheme({ light: false }));
    expect(JSON.parse(readFileSync(bootCachePath({ INDUS_HOME: dir }), "utf8")).version).toBe(1);
  });

  it("pins a theme for a subtree with ThemeProvider", () => {
    const pinned = buildTheme({ light: true });
    let seen: Theme | null = null;
    const Probe = (): ReactNode => {
      seen = useTheme();
      return null;
    };
    const out = Object.assign(new (class extends EventEmitter {})(), {
      write: () => true,
      columns: 80,
      rows: 24,
    });
    const app = render(h(ThemeProvider, { theme: pinned }, h(Probe)), {
      stdout: out as unknown as NodeJS.WriteStream,
      patchConsole: false,
    });
    app.unmount();
    expect(seen).toBe(pinned);
  });
});
