import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import {
  DEFAULT_ACCELERATORS,
  defaultShortcutSettings,
  type GlobalShortcutLike,
  parseAccelerator,
  readShortcutSettings,
  ShortcutBoard,
  type ShortcutSettings,
  sanitizeShortcutSettings,
  writeShortcutSettings,
} from "./shortcuts.ts";

class FakeGlobalShortcut implements GlobalShortcutLike {
  readonly held = new Map<string, () => void>();
  /** Chords another application owns. */
  readonly foreign = new Set<string>();
  throwOn = new Set<string>();
  register(accelerator: string, callback: () => void): boolean {
    if (this.throwOn.has(accelerator)) throw new Error("native failure");
    if (this.foreign.has(accelerator)) return false;
    this.held.set(accelerator, callback);
    return true;
  }
  unregister(accelerator: string): void {
    this.held.delete(accelerator);
  }
  isRegistered(accelerator: string): boolean {
    return this.held.has(accelerator);
  }
}

function board(fake = new FakeGlobalShortcut()) {
  const fired: string[] = [];
  const logs: string[] = [];
  const b = new ShortcutBoard({
    globalShortcut: fake,
    handlers: {
      "hud-toggle": () => fired.push("hud-toggle"),
      "hud-snap": () => fired.push("hud-snap"),
      "quick-entry": () => fired.push("quick-entry"),
    },
    log: (line) => logs.push(line),
  });
  return { b, fake, fired, logs };
}

describe("accelerator grammar", () => {
  it.each([
    ["G", "no-modifier"],
    ["Shift", "no-key"],
    ["Ctrl+A+B", "invalid-key"],
    ["A+Shift", "invalid-modifier"],
    ["Ctrl+Escape", "reserved"],
    ["Ctrl+Esc", "reserved"],
    ["   ", "empty"],
    ["", "empty"],
    ["Ctrl+Bogus", "invalid-key"],
    ["Ctrl+F25", "invalid-key"],
  ])("refuses %j (%s)", (text, reason) => {
    expect(parseAccelerator(text)).toEqual({ ok: false, reason });
  });

  it("refuses what is not a string", () => {
    expect(parseAccelerator(42)).toEqual({ ok: false, reason: "empty" });
  });

  it.each([
    ["ctrl+shift+g", "Control+Shift+G"],
    ["Shift+CmdOrCtrl+space", "CommandOrControl+Shift+Space"],
    ["alt+alt+f12", "Alt+F12"],
    ["Ctrl+Shift++", "Control+Shift+Plus"],
    ["super+1", "Super+1"],
    ["Ctrl + Shift + /", "Control+Shift+/"],
  ])("normalises %j → %s", (text, normal) => {
    expect(parseAccelerator(text)).toEqual({ ok: true, accelerator: normal });
  });

  it("the defaults are valid and in normal form", () => {
    for (const accelerator of Object.values(DEFAULT_ACCELERATORS)) {
      expect(parseAccelerator(accelerator)).toEqual({ ok: true, accelerator });
    }
  });
});

describe("saved shortcut settings", () => {
  it("garbage falls back to the defaults; a bad chord falls back to its default", () => {
    expect(sanitizeShortcutSettings(null)).toEqual(defaultShortcutSettings());
    const settings = sanitizeShortcutSettings({
      "quick-entry": { enabled: false, accelerator: "G" },
      "hud-toggle": { accelerator: "ctrl+alt+h" },
    });
    expect(settings["quick-entry"]).toEqual({
      enabled: false,
      accelerator: DEFAULT_ACCELERATORS["quick-entry"],
    });
    expect(settings["hud-toggle"]).toEqual({ enabled: true, accelerator: "Control+Alt+H" });
  });

  it("round-trips through the file", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "sc-"));
    const file = path.join(dir, "shortcuts.json");
    expect(readShortcutSettings(file)).toEqual(defaultShortcutSettings());
    const settings = defaultShortcutSettings();
    settings["hud-snap"].enabled = false;
    writeShortcutSettings(file, settings);
    expect(readShortcutSettings(file)).toEqual(settings);
    fs.writeFileSync(file, "{not json");
    expect(readShortcutSettings(file)).toEqual(defaultShortcutSettings());
    fs.rmSync(dir, { recursive: true, force: true });
  });
});

describe("shortcut board", () => {
  it("registers every enabled action and runs its handler", () => {
    const { b, fake, fired } = board();
    const states = b.apply(defaultShortcutSettings());
    expect(states.every((s) => s.registered && s.error === null)).toBe(true);
    fake.held.get("CommandOrControl+Shift+Space")?.();
    expect(fired).toEqual(["quick-entry"]);
  });

  it("the snap chord waits until a HUD is open", () => {
    const { b, fake } = board();
    const states = b.apply(defaultShortcutSettings(), (action) => action !== "hud-snap");
    expect(states.find((s) => s.action === "hud-snap")).toMatchObject({
      registered: false,
      error: null,
    });
    expect(fake.isRegistered(DEFAULT_ACCELERATORS["hud-snap"])).toBe(false);
    b.apply(defaultShortcutSettings());
    expect(fake.isRegistered(DEFAULT_ACCELERATORS["hud-snap"])).toBe(true);
  });

  it("two of our actions on one chord: the second reports a conflict and is not registered", () => {
    const { b, fake } = board();
    const settings: ShortcutSettings = defaultShortcutSettings();
    settings["quick-entry"].accelerator = "CommandOrControl+Shift+H";
    const states = b.apply(settings);
    expect(states.find((s) => s.action === "hud-toggle")).toMatchObject({ registered: true });
    expect(states.find((s) => s.action === "quick-entry")).toMatchObject({
      registered: false,
      error: "conflict",
      conflictWith: "hud-toggle",
    });
    expect(fake.held.size).toBe(2);
  });

  it("a chord another app holds is reported taken; a throwing register is reported, never thrown", () => {
    const fake = new FakeGlobalShortcut();
    fake.foreign.add(DEFAULT_ACCELERATORS["quick-entry"]);
    fake.throwOn.add(DEFAULT_ACCELERATORS["hud-toggle"]);
    const { b, logs } = board(fake);
    const states = b.apply(defaultShortcutSettings());
    expect(states.find((s) => s.action === "quick-entry")).toMatchObject({
      registered: false,
      error: "taken",
    });
    expect(states.find((s) => s.action === "hud-toggle")).toMatchObject({
      registered: false,
      error: "failed",
    });
    expect(states.find((s) => s.action === "hud-snap")).toMatchObject({ registered: true });
    expect(logs.join("\n")).toMatch(/held by another application/);
  });

  it("disabling releases the chord; re-applying moves it; dispose releases all", () => {
    const { b, fake } = board();
    b.apply(defaultShortcutSettings());
    const off = defaultShortcutSettings();
    off["quick-entry"].enabled = false;
    off["hud-toggle"].accelerator = "Alt+Shift+H";
    const states = b.apply(off);
    expect(fake.isRegistered(DEFAULT_ACCELERATORS["quick-entry"])).toBe(false);
    expect(fake.isRegistered(DEFAULT_ACCELERATORS["hud-toggle"])).toBe(false);
    expect(fake.isRegistered("Alt+Shift+H")).toBe(true);
    expect(states.find((s) => s.action === "quick-entry")).toMatchObject({
      enabled: false,
      registered: false,
      error: null,
    });
    b.dispose();
    expect(fake.held.size).toBe(0);
    expect(b.current().every((s) => !s.registered)).toBe(true);
  });

  it("an invalid saved chord is reported invalid", () => {
    const { b } = board();
    const settings = defaultShortcutSettings();
    settings["hud-toggle"].accelerator = "H";
    expect(b.apply(settings).find((s) => s.action === "hud-toggle")).toMatchObject({
      error: "invalid",
      registered: false,
    });
  });
});
