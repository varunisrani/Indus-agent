import { describe, expect, it, vi } from "vitest";
import { type AppOrigin, fileAppOrigin } from "../appOrigin.ts";
import {
  denyAllWindowOpen,
  type GuardedContents,
  hardenWebContents,
  hardenWebviewAttach,
  PREVIEW_PARTITION,
  type PreventableEvent,
} from "./navigation.ts";

const DEV: AppOrigin = { kind: "dev", origin: "http://localhost:5173" };
const FILE = fileAppOrigin("/opt/Indus/resources/app.asar/dist/index.html", "linux");

function fakeContents(type = "window") {
  const handlers = new Map<string, Array<(...args: unknown[]) => void>>();
  let openHandler: ((details: { url: string }) => { action: "deny" }) | null = null;
  const contents: GuardedContents = {
    id: 7,
    getType: () => type,
    on: (event, listener) => {
      handlers.set(event, [
        ...(handlers.get(event) ?? []),
        listener as (...args: unknown[]) => void,
      ]);
      return contents;
    },
    setWindowOpenHandler: (handler) => {
      openHandler = handler;
    },
  };
  return {
    contents,
    emit: (event: string, ...args: unknown[]) => {
      for (const h of handlers.get(event) ?? []) h(...args);
    },
    openHandler: () => openHandler,
    has: (event: string) => handlers.has(event),
  };
}

function navEvent(url: string, isMainFrame = true): PreventableEvent & { prevented: boolean } {
  const event = {
    url,
    isMainFrame,
    prevented: false,
    preventDefault() {
      event.prevented = true;
    },
  };
  return event;
}

describe("window.open: deny from every frame, no side effect", () => {
  // The renderer reports the same handler for top frames, iframes (sandboxed or not) and
  // webview guests; the decision never depends on who asked.
  const requests = [
    "https://attacker.example/phish",
    "javascript:alert(1)",
    "file:///etc/passwd",
    "indus://open/x",
    "about:blank",
    "",
  ];
  for (const type of ["window", "webview"]) {
    it(`${type}: every request is denied and nothing is opened`, () => {
      const fake = fakeContents(type);
      const opener = vi.fn();
      const log = vi.fn();
      hardenWebContents(fake.contents, { appOrigin: FILE, guestPreloadPath: null, log });
      for (const url of requests) expect(fake.openHandler()?.({ url })).toEqual({ action: "deny" });
      expect(opener).not.toHaveBeenCalled();
      // Logs carry only the origin, never the path or query.
      expect(log.mock.calls.map((c) => String(c[0])).join("\n")).not.toContain("phish");
    });
  }

  it("a throwing logger does not change the decision", () => {
    const handler = denyAllWindowOpen(() => {
      throw new Error("log down");
    }, 1);
    expect(handler({ url: "https://x" })).toEqual({ action: "deny" });
  });
});

describe("navigation confinement", () => {
  const cases: Array<[AppOrigin, string, boolean]> = [
    [DEV, "http://localhost:5173/#/chat", true],
    [DEV, "http://localhost:5173/?win=secondary#/s/1", true],
    [DEV, "http://localhost:5174/", false],
    [DEV, "https://localhost:5173/", false],
    [DEV, "https://x/", false],
    [FILE, "file:///opt/Indus/resources/app.asar/dist/index.html#/x", true],
    [FILE, "file:///opt/Indus/resources/app.asar/dist/index.html?win=hud", true],
    [FILE, "file:///etc/passwd", false],
    [FILE, "file:///opt/Indus/resources/app.asar/dist/other.html", false],
    [FILE, "https://x/", false],
    [FILE, "javascript:alert(1)", false],
  ];
  for (const [origin, url, allowed] of cases) {
    it(`${origin.kind}: ${url} → ${allowed ? "allowed" : "cancelled"}`, () => {
      const fake = fakeContents();
      hardenWebContents(fake.contents, {
        appOrigin: origin,
        guestPreloadPath: null,
        log: () => {},
      });
      const event = navEvent(url);
      fake.emit("will-navigate", event, url, false, true);
      expect(event.prevented).toBe(!allowed);
      const redirect = navEvent(url);
      fake.emit("will-redirect", redirect, url, false, true);
      expect(redirect.prevented).toBe(!allowed);
    });
  }

  it("subframe navigations are left to the CSP", () => {
    const fake = fakeContents();
    hardenWebContents(fake.contents, { appOrigin: FILE, guestPreloadPath: null, log: () => {} });
    const event = navEvent("https://x/", false);
    fake.emit("will-navigate", event, "https://x/", false, false);
    expect(event.prevented).toBe(false);
  });

  it("guests are not confined to the app (they browse), but still get the deny-all popup handler", () => {
    const fake = fakeContents("webview");
    hardenWebContents(fake.contents, { appOrigin: FILE, guestPreloadPath: null, log: () => {} });
    expect(fake.has("will-navigate")).toBe(false);
    expect(fake.openHandler()).not.toBeNull();
  });
});

describe("webview attach", () => {
  const options = { appOrigin: FILE, guestPreloadPath: "/opt/Indus/guest.cjs", log: () => {} };

  it("only the preview partition, with our preload, Node off and no popups", () => {
    const event = navEvent("");
    const prefs: Record<string, unknown> = {
      preload: "/evil.js",
      nodeIntegration: true,
      sandbox: false,
      contextIsolation: false,
    };
    const params: Record<string, unknown> = {
      partition: PREVIEW_PARTITION,
      allowpopups: "true",
      src: "http://localhost:3000",
    };
    expect(hardenWebviewAttach(event, prefs, params, options)).toBe(true);
    expect(event.prevented).toBe(false);
    expect(prefs).toMatchObject({
      preload: "/opt/Indus/guest.cjs",
      nodeIntegration: false,
      sandbox: true,
      contextIsolation: true,
    });
    expect("allowpopups" in params).toBe(false);
  });

  it.each([[""], ["persist:other"], ["indus-preview"]])("partition %j is refused", (partition) => {
    const event = navEvent("");
    expect(hardenWebviewAttach(event, {}, { partition }, options)).toBe(false);
    expect(event.prevented).toBe(true);
  });

  it("refused until the guest preload exists", () => {
    const event = navEvent("");
    expect(
      hardenWebviewAttach(
        event,
        {},
        { partition: PREVIEW_PARTITION },
        { ...options, guestPreloadPath: null },
      ),
    ).toBe(false);
  });
});
