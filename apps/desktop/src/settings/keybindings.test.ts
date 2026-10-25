import { describe, expect, it } from "vitest";
import {
  acceleratorFromEvent,
  conflictsFor,
  effectiveBindings,
  normalizeAccelerator,
} from "./keybindings.ts";

describe("key bindings", () => {
  it("normalises accelerators and refuses ones without a modifier", () => {
    expect(normalizeAccelerator("shift+cmdorctrl+k")).toBe("CommandOrControl+Shift+K");
    expect(normalizeAccelerator("Ctrl+Alt+F5")).toBe("Control+Alt+F5");
    expect(normalizeAccelerator("CommandOrControl+,")).toBe("CommandOrControl+,");
    expect(normalizeAccelerator("G")).toBeNull();
    expect(normalizeAccelerator("Ctrl+A+B")).toBeNull();
    expect(normalizeAccelerator("Ctrl+Nonsense")).toBeNull();
    expect(normalizeAccelerator("")).toBeNull();
  });

  it("reads a key press", () => {
    const press = { ctrlKey: true, metaKey: false, altKey: false, shiftKey: true };
    expect(acceleratorFromEvent({ key: "k", ...press })).toBe("CommandOrControl+Shift+K");
    expect(acceleratorFromEvent({ key: "Shift", ...press })).toBe("");
    expect(
      acceleratorFromEvent({
        key: "k",
        ctrlKey: false,
        metaKey: false,
        altKey: false,
        shiftKey: false,
      }),
    ).toBe("");
  });

  it("detects a conflict with another action, folding Control into CommandOrControl", () => {
    const bindings = {
      ...effectiveBindings({}),
      quickEntryShortcut: "CommandOrControl+Shift+Space",
    };
    expect(conflictsFor("Ctrl+N", "sidebar.toggle", bindings)).toEqual(["chat.new"]);
    expect(conflictsFor("CommandOrControl+N", "chat.new", bindings)).toEqual([]);
    expect(conflictsFor("CmdOrCtrl+Shift+Space", "chat.new", bindings)).toEqual([
      "quickEntryShortcut",
    ]);
    expect(conflictsFor("CommandOrControl+J", "chat.new", bindings)).toEqual([]);
  });

  it("an override replaces the default and an empty one unbinds", () => {
    const bindings = effectiveBindings({ "chat.new": "CommandOrControl+Shift+N", find: "" });
    expect(bindings["chat.new"]).toBe("CommandOrControl+Shift+N");
    expect(bindings.find).toBe("");
    expect(conflictsFor("CommandOrControl+F", "chat.new", bindings)).toEqual([]);
  });
});

describe("key chips (W52-j)", () => {
  it("splits an accelerator into one chip per key, per platform", async () => {
    const { acceleratorKeys, keyGroupOf } = await import("./keybindings.ts");
    expect(acceleratorKeys("CommandOrControl+Shift+T", "linux")).toEqual(["Ctrl", "Shift", "T"]);
    expect(acceleratorKeys("CommandOrControl+Shift+T", "darwin")).toEqual(["⌘", "⇧", "T"]);
    expect(acceleratorKeys("Control+Tab", "win32")).toEqual(["Ctrl", "Tab"]);
    expect(acceleratorKeys("Alt+Enter", "linux")).toEqual(["Alt", "↵"]);
    expect(acceleratorKeys("CommandOrControl+,", "linux")).toEqual(["Ctrl", ","]);
    expect(acceleratorKeys("", "linux")).toEqual([]);
    expect(keyGroupOf("tab.next")).toBe("tabs");
    expect(keyGroupOf("composer.focus")).toBe("composer");
    expect(keyGroupOf("unknown")).toBe("general");
  });
});
