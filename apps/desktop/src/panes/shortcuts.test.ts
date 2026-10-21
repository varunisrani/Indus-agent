import { describe, expect, it } from "vitest";
import { actionForKey, isModifierKey, type KeyLike } from "./shortcuts.ts";

function key(partial: Partial<KeyLike> & { key: string }): KeyLike {
  return { metaKey: false, ctrlKey: false, shiftKey: false, altKey: false, ...partial };
}

describe("tab shortcuts", () => {
  it.each([
    ["linux", key({ key: "t", ctrlKey: true }), { kind: "new-chat" }],
    ["darwin", key({ key: "t", metaKey: true }), { kind: "new-chat" }],
    ["win32", key({ key: "w", ctrlKey: true }), { kind: "close" }],
    ["linux", key({ key: "T", ctrlKey: true, shiftKey: true }), { kind: "reopen" }],
    ["darwin", key({ key: "N", metaKey: true, shiftKey: true }), { kind: "new-window" }],
    ["linux", key({ key: "b", ctrlKey: true }), { kind: "toggle-left" }],
    [
      "linux",
      key({ key: "∫", code: "KeyB", ctrlKey: true, altKey: true }),
      { kind: "toggle-right" },
    ],
    ["linux", key({ key: "3", code: "Digit3", ctrlKey: true }), { kind: "jump", slot: 3 }],
    ["darwin", key({ key: "9", metaKey: true }), { kind: "jump", slot: 9 }],
    ["darwin", key({ key: "Tab", ctrlKey: true }), { kind: "cycle", step: 1 }],
    ["linux", key({ key: "Tab", ctrlKey: true, shiftKey: true }), { kind: "cycle", step: -1 }],
  ])("%s %j", (platform, event, action) => {
    expect(actionForKey(event, platform)).toEqual(action);
  });

  it("the other platform's modifier does nothing", () => {
    expect(actionForKey(key({ key: "t", ctrlKey: true }), "darwin")).toBeNull();
    expect(actionForKey(key({ key: "t", metaKey: true }), "linux")).toBeNull();
    expect(actionForKey(key({ key: "t" }), "linux")).toBeNull();
    expect(actionForKey(key({ key: "0", ctrlKey: true }), "linux")).toBeNull();
  });

  it("knows the modifier key itself", () => {
    expect(isModifierKey(key({ key: "Meta", metaKey: true }), "darwin")).toBe(true);
    expect(isModifierKey(key({ key: "Control", ctrlKey: true }), "linux")).toBe(true);
    expect(isModifierKey(key({ key: "Control", ctrlKey: true }), "darwin")).toBe(false);
  });
});
