import { describe, expect, it, vi } from "vitest";
import {
  ExternalOpener,
  externalUrlProblem,
  USER_ACTIVATION_WINDOW_MS,
  UserActivationTracker,
} from "./externalOpen.ts";

function setup() {
  let now = 1_000;
  const activation = new UserActivationTracker(() => now);
  const openExternal = vi.fn(async (_url: string) => {});
  const opener = new ExternalOpener({ openExternal, activation, log: () => {} });
  return {
    activation,
    openExternal,
    opener,
    tick: (ms: number) => {
      now += ms;
    },
  };
}

describe("external opener", () => {
  it.each([
    ["javascript:alert(1)", "scheme"],
    ["file:///etc/passwd", "scheme"],
    ["indus://open/x", "scheme"],
    ["data:text/html,hi", "scheme"],
    ["blob:https://x/1", "scheme"],
    ["mailto:a@b.c", "scheme"],
    ["https://user:pw@example.com/", "credentials"],
    ["not a url", "invalid"],
    ["", "invalid"],
    [`https://example.com/${"a".repeat(9000)}`, "too-long"],
  ])("%s is refused (%s) and nothing opens", async (url, reason) => {
    const { activation, openExternal, opener } = setup();
    activation.noteInput(1, "mouseUp");
    expect(await opener.open({ url, contentsId: 1 })).toEqual({ ok: false, reason });
    expect(openExternal).not.toHaveBeenCalled();
  });

  it("https after a real click opens once; a second open needs a new click", async () => {
    const { activation, openExternal, opener } = setup();
    activation.noteInput(1, "mouseUp");
    expect(await opener.open({ url: "https://example.com/docs", contentsId: 1 })).toEqual({
      ok: true,
    });
    expect(openExternal).toHaveBeenCalledWith("https://example.com/docs");
    expect(await opener.open({ url: "https://example.com/again", contentsId: 1 })).toEqual({
      ok: false,
      reason: "no-user-activation",
    });
    expect(openExternal).toHaveBeenCalledTimes(1);
  });

  it("no input (a synthetic click from page script) → refused", async () => {
    const { openExternal, opener } = setup();
    expect(await opener.open({ url: "https://example.com", contentsId: 1 })).toEqual({
      ok: false,
      reason: "no-user-activation",
    });
    expect(openExternal).not.toHaveBeenCalled();
  });

  it("input on another web contents does not count; hovering does not count", async () => {
    const { activation, opener } = setup();
    activation.noteInput(2, "mouseUp");
    activation.noteInput(1, "mouseMove");
    activation.noteInput(1, "mouseWheel");
    expect((await opener.open({ url: "http://example.com", contentsId: 1 })).ok).toBe(false);
  });

  it("an activation expires after the window", async () => {
    const { activation, opener, tick } = setup();
    activation.noteInput(1, "keyDown");
    tick(USER_ACTIVATION_WINDOW_MS + 1);
    expect((await opener.open({ url: "https://example.com", contentsId: 1 })).ok).toBe(false);
  });

  it("an OS failure is reported, not thrown", async () => {
    const { activation, opener, openExternal } = setup();
    openExternal.mockRejectedValueOnce(new Error("no handler"));
    activation.noteInput(1, "mouseUp");
    expect(await opener.open({ url: "https://example.com", contentsId: 1 })).toEqual({
      ok: false,
      reason: "failed",
    });
  });

  it("problem table", () => {
    expect(externalUrlProblem("https://example.com")).toBeNull();
    expect(externalUrlProblem("http://127.0.0.1:3000/x?y=1")).toBeNull();
    expect(externalUrlProblem(42)).toBe("invalid");
  });
});
