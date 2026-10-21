import { describe, expect, it } from "vitest";
import {
  type DeepLink,
  type DeepLinkHost,
  DeepLinkRouter,
  type LinkWindow,
  parseDeepLink,
} from "./deepLinks.ts";

describe("parseDeepLink", () => {
  const schemes = ["indus"];

  it("reads kind, decoded name and params", () => {
    expect(parseDeepLink("indus://skill/team%2Freview-helper?source=hub", schemes)).toEqual({
      kind: "skill",
      name: "team/review-helper",
      params: { source: "hub" },
    });
    expect(parseDeepLink("indus://open/settings", schemes)).toEqual({
      kind: "open",
      name: "settings",
      params: {},
    });
    expect(parseDeepLink("INDUS://Close-Preview", schemes)?.kind).toBe("close-preview");
    expect(parseDeepLink("indus://mcp/docs?url=https%3A%2F%2Fmcp.example%2Fsse", schemes)).toEqual({
      kind: "mcp",
      name: "docs",
      params: { url: "https://mcp.example/sse" },
    });
  });

  it("refuses other schemes, unknown kinds, malformed, credentials and oversized links", () => {
    expect(parseDeepLink("indus-dev://open/x", schemes)).toBeNull();
    expect(parseDeepLink("https://open/x", schemes)).toBeNull();
    expect(parseDeepLink("indus://blueprint/x", schemes)).toBeNull();
    expect(parseDeepLink("indus://open/%zz", schemes)).toBeNull();
    expect(parseDeepLink("indus://user:pw@open/x", schemes)).toBeNull();
    expect(parseDeepLink("indus://open:99/x", schemes)).toBeNull();
    expect(parseDeepLink(`indus://open/${"a".repeat(5000)}`, schemes)).toBeNull();
    expect(parseDeepLink("indus://open/a%0Ab", schemes)).toBeNull();
    expect(parseDeepLink(42, schemes)).toBeNull();
    const many = Array.from({ length: 17 }, (_, i) => `k${i}=v`).join("&");
    expect(parseDeepLink(`indus://open/x?${many}`, schemes)).toBeNull();
  });

  it("a dev build answers its own scheme only", () => {
    expect(parseDeepLink("indus-dev://open/x", ["indus-dev"])?.name).toBe("x");
    expect(parseDeepLink("indus://open/x", ["indus-dev"])).toBeNull();
  });
});

interface FakeWindow extends LinkWindow {
  calls: string[];
  visible: boolean;
  minimized: boolean;
  fullscreen: boolean;
  destroyed: boolean;
  fire(event: string): void;
}

function fakeWindow(id: number): FakeWindow {
  const listeners = new Map<string, Array<() => void>>();
  const calls: string[] = [];
  const win: FakeWindow = {
    calls,
    visible: true,
    minimized: false,
    fullscreen: false,
    destroyed: false,
    webContents: {
      id,
      send: () => undefined,
      on: (event: string, listener: (...args: never[]) => void) => {
        listeners.set(event, [...(listeners.get(event) ?? []), listener as () => void]);
      },
    },
    isDestroyed: () => win.destroyed,
    isMinimized: () => win.minimized,
    isVisible: () => win.visible,
    isFullScreen: () => win.fullscreen,
    restore: () => {
      calls.push("restore");
      win.minimized = false;
    },
    show: () => {
      calls.push("show");
      win.visible = true;
    },
    showInactive: () => {
      calls.push("showInactive");
      win.visible = true;
    },
    focus: () => calls.push("focus"),
    setFullScreen: (flag: boolean) => {
      calls.push(`fullscreen:${flag}`);
      win.fullscreen = flag;
    },
    fire: (event: string) => {
      for (const listener of listeners.get(event) ?? []) listener();
    },
  };
  return win;
}

function fakeHost(win: FakeWindow | null) {
  const delivered: DeepLink[] = [];
  const closeRequests: number[] = [];
  const opened: number[] = [];
  const state = { win, sendOk: true };
  const host: DeepLinkHost = {
    mainWindow: () => state.win,
    openMain: () => opened.push(1),
    sendLink: (_contents, link) => {
      if (!state.sendOk) return false;
      delivered.push(link);
      return true;
    },
    sendClosePreview: (contents) => {
      closeRequests.push(contents.id);
      return true;
    },
    log: () => undefined,
  };
  return { host, delivered, closeRequests, opened, state };
}

describe("DeepLinkRouter", () => {
  it("two links before ready: only the latest is delivered, exactly once", () => {
    const router = new DeepLinkRouter("indus");
    router.receive("indus://open/first");
    router.receive("indus://skill/second");
    const win = fakeWindow(7);
    const { host, delivered } = fakeHost(win);
    router.attach(host); // main is ready, the page is not listening yet
    expect(delivered).toEqual([]);
    router.rendererReady(7);
    expect(delivered.map((l) => l.name)).toEqual(["second"]);
    router.rendererReady(7); // a repeated signal never delivers twice
    expect(delivered).toHaveLength(1);
    expect(router.waiting()).toBeNull();
  });

  it("second-instance and open-url links arriving after ready are delivered at once", () => {
    const router = new DeepLinkRouter("indus");
    const win = fakeWindow(3);
    const { host, delivered } = fakeHost(win);
    router.attach(host);
    router.rendererReady(3);
    router.receive("indus://open/abc");
    router.receive("indus://mcp/docs?url=https://x.example");
    expect(delivered.map((l) => `${l.kind}:${l.name}`)).toEqual(["open:abc", "mcp:docs"]);
  });

  it("a ready signal from another window does not count", () => {
    const router = new DeepLinkRouter("indus");
    const win = fakeWindow(3);
    const { host, delivered } = fakeHost(win);
    router.attach(host);
    router.receive("indus://open/x");
    router.rendererReady(99);
    expect(delivered).toEqual([]);
    router.rendererReady(3);
    expect(delivered).toHaveLength(1);
  });

  it("a reload or crash waits for a fresh ready signal", () => {
    const router = new DeepLinkRouter("indus");
    const win = fakeWindow(5);
    const { host, delivered } = fakeHost(win);
    router.attach(host);
    router.rendererReady(5);
    win.fire("did-navigate");
    router.receive("indus://open/after-reload");
    expect(delivered).toEqual([]);
    router.rendererReady(5);
    expect(delivered.map((l) => l.name)).toEqual(["after-reload"]);
    win.fire("render-process-gone");
    router.receive("indus://open/after-crash");
    expect(delivered).toHaveLength(1);
  });

  it("delivery shows a tray-hidden window inactive and never focuses it (R08.21)", () => {
    const router = new DeepLinkRouter("indus");
    const win = fakeWindow(2);
    win.visible = false;
    const { host, delivered } = fakeHost(win);
    router.attach(host);
    router.rendererReady(2);
    router.receive("indus://open/x");
    expect(delivered).toHaveLength(1);
    expect(win.calls).toEqual(["showInactive"]);
  });

  it("close-preview is handled in main: restore, show, focus, leave full screen, close the pane", () => {
    const router = new DeepLinkRouter("indus");
    const win = fakeWindow(4);
    win.minimized = true;
    win.visible = false;
    win.fullscreen = true;
    const { host, delivered, closeRequests } = fakeHost(win);
    router.attach(host);
    // the page never said it listens: close-preview still works
    router.receive("indus://close-preview");
    expect(win.calls).toEqual(["restore", "show", "focus", "fullscreen:false"]);
    expect(closeRequests).toEqual([4]);
    expect(delivered).toEqual([]);
    expect(router.waiting()).toBeNull();
  });

  it("close-preview before main is ready runs once main attaches; it never replaces a queued link", () => {
    const router = new DeepLinkRouter("indus");
    router.receive("indus://open/keep");
    router.receive("indus://close-preview");
    const win = fakeWindow(1);
    const { host, delivered, closeRequests } = fakeHost(win);
    router.attach(host);
    expect(closeRequests).toEqual([1]);
    router.rendererReady(1);
    expect(delivered.map((l) => l.name)).toEqual(["keep"]);
  });

  it("no main window: one is opened and the link waits for its page", () => {
    const router = new DeepLinkRouter("indus");
    const fake = fakeHost(null);
    router.attach(fake.host);
    router.receive("indus://open/x");
    expect(fake.opened).toEqual([1]);
    const win = fakeWindow(9);
    fake.state.win = win;
    router.rendererReady(9);
    expect(fake.delivered.map((l) => l.name)).toEqual(["x"]);
  });

  it("a failed send keeps the link for the next ready signal", () => {
    const router = new DeepLinkRouter("indus");
    const win = fakeWindow(6);
    const fake = fakeHost(win);
    router.attach(fake.host);
    fake.state.sendOk = false;
    router.rendererReady(6);
    router.receive("indus://open/x");
    expect(router.waiting()?.name).toBe("x");
    fake.state.sendOk = true;
    win.fire("did-navigate");
    router.rendererReady(6);
    expect(fake.delivered.map((l) => l.name)).toEqual(["x"]);
  });

  it("junk is dropped without touching the queue", () => {
    const router = new DeepLinkRouter("indus");
    router.receive("indus://open/x");
    router.receive("javascript:alert(1)");
    router.receive("indus://unknown/y");
    expect(router.waiting()?.name).toBe("x");
  });
});
