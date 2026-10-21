import { describe, expect, it } from "vitest";
import { KIND_TRAITS, type WindowKind, webPreferencesFor, windowOptionsFor } from "./factory.ts";

const kinds = Object.keys(KIND_TRAITS) as WindowKind[];

describe("window factory", () => {
  for (const kind of kinds) {
    for (const dev of [false, true]) {
      it(`${kind} (${dev ? "dev" : "packaged"}) preferences`, () => {
        expect(webPreferencesFor(kind, { preloadPath: "/app/preload.cjs", dev })).toMatchSnapshot();
      });
    }
  }

  it("every kind keeps the security floor", () => {
    for (const kind of kinds) {
      for (const dev of [false, true]) {
        const prefs = webPreferencesFor(kind, { preloadPath: "/p", dev });
        expect(prefs).toMatchObject({
          contextIsolation: true,
          sandbox: true,
          nodeIntegration: false,
          nodeIntegrationInSubFrames: false,
          webSecurity: true,
          allowRunningInsecureContent: false,
          focusOnNavigation: false,
        });
        if (!dev) expect(prefs.devTools).toBe(false);
      }
    }
  });

  it("webview only where panes live; helper windows have no preload", () => {
    expect(webPreferencesFor("main", { preloadPath: "/p", dev: false }).webviewTag).toBe(true);
    expect(webPreferencesFor("hud", { preloadPath: "/p", dev: false }).webviewTag).toBe(false);
    expect(webPreferencesFor("hidden", { preloadPath: "/p", dev: true })).not.toHaveProperty(
      "preload",
    );
    expect(webPreferencesFor("hidden", { preloadPath: "/p", dev: true }).devTools).toBe(false);
  });

  const input = {
    preloadPath: "/p",
    dev: false,
    bounds: { width: 1000, height: 700, x: 10, y: 20 },
    title: "Indus",
    backgroundColor: "#141518",
    titleBar: { color: "#00000000", symbolColor: "#ffffff" },
  };

  it("main window: hidden at creation, overlay title bar 34 px off macOS, traffic lights on macOS", () => {
    const linux = windowOptionsFor("main", { ...input, platform: "linux" });
    expect(linux).toMatchObject({
      show: false,
      titleBarStyle: "hidden",
      minWidth: 400,
      minHeight: 620,
    });
    expect(linux.titleBarOverlay).toEqual({
      color: "#00000000",
      symbolColor: "#ffffff",
      height: 34,
    });
    const mac = windowOptionsFor("main", { ...input, platform: "darwin" });
    expect(mac.trafficLightPosition).toEqual({ x: 16, y: 10 });
    expect(mac).not.toHaveProperty("titleBarOverlay");
  });

  it("transparent kinds start transparent with no frame", () => {
    for (const kind of ["hud", "quick"] as const) {
      expect(windowOptionsFor(kind, { ...input, platform: "linux" })).toMatchObject({
        transparent: true,
        frame: false,
        backgroundColor: "#00000000",
        show: false,
      });
    }
  });
});
