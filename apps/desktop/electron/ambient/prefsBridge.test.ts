import { describe, expect, it } from "vitest";
import { DEFAULT_PREFS } from "../prefs.ts";
import {
  ambientFromPrefs,
  anyNoticeOn,
  NOTICE_KINDS,
  prefsPatchFromAmbient,
} from "./prefsBridge.ts";

describe("ambient view of the notification switches (W59-K1-1)", () => {
  it("the master switch is on while any one kind is on", () => {
    const off = Object.fromEntries(NOTICE_KINDS.map((kind) => [kind, false]));
    const none = { ...DEFAULT_PREFS.notifications, ...off };
    expect(anyNoticeOn(none)).toBe(false);
    expect(anyNoticeOn({ ...none, plugins: true })).toBe(true);
    expect(ambientFromPrefs({ ...DEFAULT_PREFS, notifications: none }, false).notifications).toBe(
      false,
    );
    expect(ambientFromPrefs(DEFAULT_PREFS, false).notifications).toBe(true);
  });

  it("the ambient on/off writes every kind and leaves the sound alone", () => {
    expect(prefsPatchFromAmbient({ notifications: false })).toEqual({
      notifications: {
        approvals: false,
        input: false,
        turnComplete: false,
        turnFailed: false,
        plugins: false,
      },
    });
    expect(prefsPatchFromAmbient({ sounds: false })).toEqual({ notifications: { sounds: false } });
  });
});
