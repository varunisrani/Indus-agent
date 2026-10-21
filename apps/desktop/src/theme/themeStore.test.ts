import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { presetTheme } from "@indus/ui";
import { describe, expect, it, vi } from "vitest";
import { fakeRest } from "../settings/fakeRest.ts";
import { installMarketplaceTheme } from "./AppearancePage.tsx";
import {
  APPEARANCE_KEY,
  AppearanceStore,
  PROFILE_APPEARANCE_KEY,
  syncTui,
  userThemeFrom,
} from "./themeStore.ts";
import { convertVscodeTheme, type VscodeTheme } from "./vscodeImport.ts";

function memory(): Storage {
  const data = new Map<string, string>();
  return {
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    getItem: (k) => data.get(k) ?? null,
    key: (i) => [...data.keys()][i] ?? null,
    removeItem: (k) => {
      data.delete(k);
    },
    setItem: (k, v) => {
      data.set(k, v);
    },
  };
}

const FIXTURE = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../../tests/fixtures/desktop/vscode_theme.json",
);

describe("appearance store", () => {
  it("paints nothing until something is chosen", () => {
    const store = new AppearanceStore(memory());
    expect(store.resolve(null, "dark")).toBeNull();
    expect(store.css(null, "dark")).toBe("");
  });

  it("presets, accent and per-profile choices resolve and persist in two keys", () => {
    const storage = memory();
    const store = new AppearanceStore(storage);
    store.choose("lotus");
    store.setAccent(380);
    store.setProfile("work", { theme: "basalt", chatFont: "Inter, sans-serif" });
    expect(store.resolve(null, "light")).toEqual(presetTheme("lotus", "light", { accentHue: 20 }));
    expect(store.resolve("work", "dark")).toEqual(presetTheme("basalt", "dark", { accentHue: 20 }));
    expect(store.css("work", "dark")).toContain("--indus-chat-font: Inter, sans-serif;");
    expect(JSON.parse(storage.getItem(PROFILE_APPEARANCE_KEY) ?? "{}")).toEqual({
      work: { theme: "basalt", chatFont: "Inter, sans-serif" },
    });
    expect(JSON.parse(storage.getItem(APPEARANCE_KEY) ?? "{}")).not.toHaveProperty("profiles");
    const again = new AppearanceStore(storage);
    expect(again.get()).toEqual(store.get());
    store.setProfile("work", { theme: "", chatFont: "" });
    expect(store.get().profiles.work).toEqual({});
  });

  it("bad stored data and unsafe fonts are dropped", () => {
    const storage = memory();
    storage.setItem(APPEARANCE_KEY, "{not json");
    storage.setItem(PROFILE_APPEARANCE_KEY, JSON.stringify({ work: { chatFont: "x; } body {" } }));
    const store = new AppearanceStore(storage);
    expect(store.get().theme).toBeNull();
    expect(store.get().profiles.work).toEqual({});
  });

  it("user themes from VS Code install once, re-activate, and can be removed", async () => {
    const store = new AppearanceStore(memory());
    const fixture = JSON.parse(fs.readFileSync(FIXTURE, "utf8")) as VscodeTheme;
    store.addUserTheme(userThemeFrom(convertVscodeTheme(fixture), "acme.river"));
    const download = vi.fn(async () => ({ vsixBase64: "" }));
    const label = await installMarketplaceTheme(
      {
        publisher: "acme",
        extension: "river",
        version: "1.0.0",
        displayName: "River",
        description: "",
        installs: 1,
      },
      { downloadMarketplace: download },
      store,
    );
    expect(label).toBe("Riverbank Night");
    expect(download).not.toHaveBeenCalled(); // already installed: just used
    expect(store.get().theme).toBe("user:vscode-riverbank-night");
    expect(store.resolve(null, "light")?.bg.hex).toBe("#16202a");
    store.removeUserTheme("vscode-riverbank-night");
    expect(store.get().theme).toBeNull();
    expect(store.get().userThemes).toEqual([]);
  });

  it("the TUI sync writes the polarity through the config route", async () => {
    const api = fakeRest();
    api.route("PUT", "/api/config", { ok: true });
    expect(await syncTui(api, "work", "light")).toBe(true);
    expect(api.calls[0]).toMatchObject({
      method: "PUT",
      path: "/api/config",
      body: { config: { tui: { theme: "light" } } },
      profile: "work",
    });
  });
});
