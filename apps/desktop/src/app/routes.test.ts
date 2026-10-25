import { describe, expect, it } from "vitest";
import {
  backgroundOf,
  classifyPath,
  closeTarget,
  OVERLAYS,
  overlayStateFrom,
  PAGES,
  sessionPath,
} from "./routes.ts";

describe("routes", () => {
  it.each([
    ["/", { kind: "chat", sessionId: null }],
    ["", { kind: "chat", sessionId: null }],
    ["/abc", { kind: "chat", sessionId: "abc" }],
    ["/a%2Fb", { kind: "chat", sessionId: "a/b" }],
    ["/abc?x=1", { kind: "chat", sessionId: "abc" }],
    ["/capabilities", { kind: "page", page: "capabilities" }],
    ["/messaging", { kind: "page", page: "messaging" }],
    ["/artifacts", { kind: "page", page: "artifacts" }],
    ["/settings", { kind: "overlay", overlay: "settings" }],
    ["/cron", { kind: "overlay", overlay: "cron" }],
    ["/profiles", { kind: "overlay", overlay: "profiles" }],
    ["/agents", { kind: "overlay", overlay: "agents" }],
    ["/webhooks", { kind: "overlay", overlay: "webhooks" }],
    ["/command-center", { kind: "overlay", overlay: "command-center" }],
    ["/command-center?section=usage", { kind: "overlay", overlay: "command-center" }],
    ["/learning-map", { kind: "overlay", overlay: "learning-map" }],
    ["/session-import", { kind: "overlay", overlay: "session-import" }],
    ["/a/b", { kind: "unknown" }],
    ["/%zz", { kind: "unknown" }],
    ["/%20", { kind: "unknown" }],
    ["/settings%2F", { kind: "chat", sessionId: "settings/" }],
  ])("%s", (path, view) => {
    expect(classifyPath(path)).toEqual(view);
  });

  it("never reads a reserved name as a session id, even encoded", () => {
    for (const name of [...PAGES, ...OVERLAYS]) {
      const view = classifyPath(`/${encodeURIComponent(name)}`);
      expect(view.kind === "chat" && view.sessionId === name).toBe(false);
    }
  });

  it("session paths round-trip", () => {
    for (const id of ["s-1", "a b", "x/y", "ünï"]) {
      expect(classifyPath(sessionPath(id))).toEqual({ kind: "chat", sessionId: id });
    }
  });

  it("an overlay returns to the route it was opened from; cold, it returns home", () => {
    expect(closeTarget(overlayStateFrom("/abc", null))).toBe("/abc");
    expect(closeTarget(overlayStateFrom("/messaging", null))).toBe("/messaging");
    expect(closeTarget(null)).toBe("/");
    expect(closeTarget({ background: "/settings" })).toBe("/");
    // the management overlays (W52-l-1) never become a background either
    expect(closeTarget({ background: "/command-center" })).toBe("/");
    expect(closeTarget(overlayStateFrom("/webhooks", overlayStateFrom("/artifacts", null)))).toBe(
      "/artifacts",
    );
    // Opened from another overlay: the first background is kept.
    const first = overlayStateFrom("/abc", null);
    expect(closeTarget(overlayStateFrom("/settings", first))).toBe("/abc");
    expect(backgroundOf({ background: 42 })).toBeNull();
  });
});
