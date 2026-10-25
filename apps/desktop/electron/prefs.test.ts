import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { IpcError } from "./ipc/envelope.ts";
import { applyPatch, DEFAULT_PREFS, DesktopPrefs, readPrefs } from "./prefs.ts";

describe("desktop preferences", () => {
  it("defaults: tray off, keep awake while working, chat text 110 %", () => {
    expect(readPrefs(null)).toEqual(DEFAULT_PREFS);
    expect(DEFAULT_PREFS.minimizeToTray).toBe(false);
    expect(DEFAULT_PREFS.keepAwake).toBe("while-working");
    expect(DEFAULT_PREFS.chatTextScale).toBe(110);
  });

  it("a bad field is dropped alone; good fields survive", () => {
    const prefs = readPrefs({
      minimizeToTray: true,
      keepAwake: "sometimes",
      chatTextScale: 999,
      notifications: { sounds: false, bogus: 1 },
      keybindings: { "chat.new": "CommandOrControl+N" },
      unknown: 1,
    });
    expect(prefs.minimizeToTray).toBe(true);
    expect(prefs.keepAwake).toBe("while-working");
    expect(prefs.chatTextScale).toBe(110);
    expect(prefs.notifications).toEqual({
      turnComplete: true,
      approvals: true,
      sounds: false,
      input: true,
      turnFailed: true,
      plugins: true,
    });
    expect(prefs.keybindings).toEqual({ "chat.new": "CommandOrControl+N" });
    expect(readPrefs([1, 2])).toEqual(DEFAULT_PREFS);
  });

  it("display choices (W55-F3): today's look by default; a bad value falls back alone", () => {
    expect(DEFAULT_PREFS).toMatchObject({
      introSplash: true,
      reopenLastChat: false,
      sessionDensity: "compact",
      tabStrip: "always",
      chatBackdrop: false,
      bubbleTransparency: 0,
      textDirection: "auto",
      hideTimeline: false,
      toolCallDisplay: "technical",
      collapseThinking: false,
    });
    const prefs = readPrefs({ sessionDensity: "detailed", bubbleTransparency: 140, tabStrip: "x" });
    expect(prefs.sessionDensity).toBe("detailed");
    expect(prefs.bubbleTransparency).toBe(0);
    expect(prefs.tabStrip).toBe("always");
    expect(applyPatch(DEFAULT_PREFS, { textDirection: "rtl" }).textDirection).toBe("rtl");
    expect(() => applyPatch(DEFAULT_PREFS, { bubbleTransparency: 101 })).toThrow();
  });

  it("window choices (W56-G1): actions right, no file browser, docked composer by default", () => {
    expect(DEFAULT_PREFS).toMatchObject({
      appActionsSide: "right",
      fileBrowser: false,
      floatingComposer: false,
    });
    // an older file without the fields, and one with a bad side
    expect(readPrefs({ minimizeToTray: true }).appActionsSide).toBe("right");
    const prefs = readPrefs({ appActionsSide: "top", fileBrowser: true, floatingComposer: "yes" });
    expect(prefs.appActionsSide).toBe("right");
    expect(prefs.fileBrowser).toBe(true);
    expect(prefs.floatingComposer).toBe(false);
    const next = applyPatch(DEFAULT_PREFS, { appActionsSide: "left", floatingComposer: true });
    expect(next.appActionsSide).toBe("left");
    expect(next.floatingComposer).toBe(true);
    expect(() =>
      applyPatch(DEFAULT_PREFS, { appActionsSide: "middle" as unknown as "left" }),
    ).toThrow();
  });

  it("alert kinds split out later (W59-K1-1) follow the switch that covered them", () => {
    // an older file: questions were under "approvals", failed turns under "turnComplete"
    const old = readPrefs({ notifications: { approvals: false, turnComplete: false } });
    expect(old.notifications).toMatchObject({
      approvals: false,
      input: false,
      turnComplete: false,
      turnFailed: false,
      plugins: true,
    });
    // a file that names the new kinds keeps them
    const now = readPrefs({ notifications: { approvals: false, input: true, turnFailed: true } });
    expect(now.notifications).toMatchObject({ approvals: false, input: true, turnFailed: true });
    expect(DEFAULT_PREFS.notifications).toMatchObject({
      input: true,
      turnFailed: true,
      plugins: true,
    });
  });

  it("a patch merges notifications and replaces the binding overrides", () => {
    const start = applyPatch(DEFAULT_PREFS, { keybindings: { a: "X" } });
    const next = applyPatch(start, { notifications: { sounds: false }, keybindings: { b: "Y" } });
    expect(next.notifications).toEqual({
      turnComplete: true,
      approvals: true,
      sounds: false,
      input: true,
      turnFailed: true,
      plugins: true,
    });
    expect(next.keybindings).toEqual({ b: "Y" });
    expect(DEFAULT_PREFS.notifications.sounds).toBe(true); // defaults never mutated
  });

  it("set writes the file atomically, tells listeners and survives a reload", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "prefs-"));
    try {
      const file = path.join(dir, "desktop-prefs.json");
      const store = new DesktopPrefs({ file });
      const seen: boolean[] = [];
      store.onChange((value) => seen.push(value.minimizeToTray));
      store.set({ minimizeToTray: true });
      expect(seen).toEqual([true]);
      expect(new DesktopPrefs({ file }).get().minimizeToTray).toBe(true);
      expect(fs.readdirSync(dir)).toEqual(["desktop-prefs.json"]);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("a failed write keeps the old value and answers save-failed", () => {
    const store = new DesktopPrefs({
      file: "/nowhere/prefs.json",
      read: () => null,
      write: () => {
        throw new Error("disk full");
      },
    });
    expect(() => store.set({ minimizeToTray: true })).toThrow(IpcError);
    expect(store.get().minimizeToTray).toBe(false);
  });
});
