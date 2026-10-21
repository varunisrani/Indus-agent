import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { type PaneIpcHarness, paneIpcHarness } from "../../testing/paneIpc.ts";
import { PathGuard } from "../fs/pathGuard.ts";
import {
  ExternalOpener,
  installExternalOpenIpc,
  UserActivationTracker,
} from "../security/externalOpen.ts";
import { PREVIEW_PARTITION } from "../security/navigation.ts";
import { type RawListener, WATCH_DEBOUNCE_MS, WatchHub } from "./fileWatch.ts";
import { type GuestContents, guestKeyAction, installPreviewIpc, PreviewGuests } from "./guest.ts";

class FakeGuest implements GuestContents {
  readonly id: number;
  readonly hostWebContents: { id: number } | null;
  private readonly type: string;
  private destroyed = false;
  readonly listeners = new Map<string, Array<(...args: unknown[]) => void>>();
  image = { isEmpty: () => false, toDataURL: () => "data:image/png;base64,AAAA" };

  constructor(id: number, hostId: number | null, type = "webview") {
    this.id = id;
    this.hostWebContents = hostId === null ? null : { id: hostId };
    this.type = type;
  }
  getType() {
    return this.type;
  }
  isDestroyed() {
    return this.destroyed;
  }
  on(event: string, listener: (...args: never[]) => void) {
    const list = this.listeners.get(event) ?? [];
    list.push(listener as (...args: unknown[]) => void);
    this.listeners.set(event, list);
    return this;
  }
  fire(event: string, ...args: unknown[]) {
    for (const l of this.listeners.get(event) ?? []) l(...args);
  }
  destroy() {
    this.destroyed = true;
    this.fire("destroyed");
  }
  async capturePage() {
    return this.image;
  }
}

class FakeHostWindow {
  fullscreen = false;
  isFullScreen() {
    return this.fullscreen;
  }
  setFullScreen(flag: boolean) {
    this.fullscreen = flag;
  }
  isDestroyed() {
    return false;
  }
}

let h: PaneIpcHarness;
let guests: PreviewGuests;
let host: FakeHostWindow;
let opened: string[];
let activation: UserActivationTracker;
let now = 0;

beforeEach(() => {
  h = paneIpcHarness([11, 12]);
  host = new FakeHostWindow();
  opened = [];
  now = 1_000;
  activation = new UserActivationTracker(() => now);
  guests = new PreviewGuests({
    hostWindow: (id) => (id === 11 ? host : null),
    hostContents: (id) => h.contents(id),
    registry: h.registry,
    log: () => {},
  });
  installExternalOpenIpc({
    registry: h.registry,
    opener: new ExternalOpener({
      openExternal: async (url) => {
        opened.push(url);
      },
      activation,
      log: () => {},
    }),
    isGuestOf: (guest, hostId) => guests.isGuestOf(guest, hostId),
  });
});

function keyDown(key: string, extra: Record<string, unknown> = {}) {
  return { type: "keyDown", key, ...extra };
}

describe("escape hatch routing", () => {
  it("Escape leaves fullscreen only while fullscreen; Ctrl/Cmd+Shift+W closes", () => {
    expect(guestKeyAction(keyDown("Escape"), true)).toBe("exit-fullscreen");
    expect(guestKeyAction(keyDown("Escape"), false)).toBeNull();
    expect(guestKeyAction(keyDown("Escape", { isAutoRepeat: true }), true)).toBeNull();
    expect(guestKeyAction({ type: "keyUp", key: "Escape" }, true)).toBeNull();
    expect(guestKeyAction(keyDown("W", { control: true, shift: true }), false)).toBe(
      "close-preview",
    );
    expect(guestKeyAction(keyDown("w", { meta: true, shift: true }), true)).toBe("close-preview");
    expect(guestKeyAction(keyDown("w", { control: true }), false)).toBeNull();
    expect(
      guestKeyAction(keyDown("w", { control: true, shift: true, alt: true }), false),
    ).toBeNull();
    expect(guestKeyAction(keyDown("a"), true)).toBeNull();
  });

  it("the guest's key hook acts before the page: fullscreen left, close requested", () => {
    const guest = new FakeGuest(40, 11);
    expect(guests.adopt(guest, PREVIEW_PARTITION)).toBe(true);
    let prevented = 0;
    const event = { preventDefault: () => (prevented += 1) };
    host.fullscreen = true;
    guest.fire("before-input-event", event, keyDown("Escape"));
    expect(host.fullscreen).toBe(false);
    guest.fire("before-input-event", event, keyDown("Escape")); // not fullscreen: the page keeps it
    guest.fire("before-input-event", event, keyDown("w", { control: true, shift: true }));
    expect(prevented).toBe(2);
    expect(h.window(11).events("indus:preview:close-requested")).toEqual([{}]);
  });
});

describe("guest ownership", () => {
  it("adopts only webview guests on the preview partition with a host", () => {
    expect(guests.adopt(new FakeGuest(41, 11), "persist:other")).toBe(false);
    expect(guests.adopt(new FakeGuest(42, 11, "window"), PREVIEW_PARTITION)).toBe(false);
    expect(guests.adopt(new FakeGuest(43, null), PREVIEW_PARTITION)).toBe(false);
    const guest = new FakeGuest(44, 11);
    guests.adopt(guest, PREVIEW_PARTITION);
    expect(guests.isGuestOf(44, 11)).toBe(true);
    expect(guests.isGuestOf(44, 12)).toBe(false);
    guest.destroy();
    expect(guests.isGuestOf(44, 11)).toBe(false);
    expect(guests.count()).toBe(0);
  });

  it("an untrusted guest click cannot open; a trusted one can, http/https only", async () => {
    guests.adopt(new FakeGuest(45, 11), PREVIEW_PARTITION);
    // the page called click() / window.open: no input event reached the guest
    expect(
      await h.invoke("indus:external:open", { url: "https://example.com/", guestContentsId: 45 }),
    ).toEqual({ ok: false, reason: "no-user-activation" });
    // a real click in the guest
    activation.noteInput(45, "mouseUp");
    expect(
      await h.invoke("indus:external:open", { url: "file:///etc/passwd", guestContentsId: 45 }),
    ).toEqual({ ok: false, reason: "scheme" });
    activation.noteInput(45, "mouseUp");
    expect(
      await h.invoke("indus:external:open", { url: "https://example.com/a", guestContentsId: 45 }),
    ).toEqual({ ok: true });
    // the activation is used up
    expect(
      await h.invoke("indus:external:open", { url: "https://example.com/b", guestContentsId: 45 }),
    ).toEqual({ ok: false, reason: "no-user-activation" });
    // another window cannot borrow the guest's activation
    activation.noteInput(45, "mouseUp");
    expect(
      await h.invoke(
        "indus:external:open",
        { url: "https://example.com/c", guestContentsId: 45 },
        12,
      ),
    ).toEqual({ ok: false, reason: "no-user-activation" });
    expect(opened).toEqual(["https://example.com/a"]);
  });
});

describe("preview channels", () => {
  let base = "";
  let raws: Array<{ listener: RawListener }> = [];
  let timers: Array<{ at: number; fn: () => void }> = [];
  let clock = 0;

  beforeEach(async () => {
    base = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "indus-preview-")));
    fs.writeFileSync(path.join(base, "page.html"), "<p>hi</p>");
    fs.writeFileSync(path.join(base, ".env"), "X=1");
    raws = [];
    timers = [];
    clock = 0;
    const guard = new PathGuard({ home: base });
    installPreviewIpc({
      registry: h.registry,
      guests,
      guard,
      contents: (id) => h.contents(id),
      watches: new WatchHub({
        newId: () => "pw1",
        timers: {
          setTimeout: (fn, ms) => {
            timers.push({ at: clock + ms, fn });
            return timers.length;
          },
          clearTimeout: () => {
            timers = [];
          },
          setInterval: () => 0,
          clearInterval: () => {},
          now: () => clock,
        },
        fs: {
          watch: (_dir, listener) => {
            raws.push({ listener });
            return { close: () => {} };
          },
          snapshot: () => null,
          exists: (p) => fs.existsSync(p),
        },
      }),
    });
  });
  afterEach(() => fs.rmSync(base, { recursive: true, force: true }));

  it("capture refuses a guest of another window and an empty image", async () => {
    const guest = new FakeGuest(46, 11);
    guests.adopt(guest, PREVIEW_PARTITION);
    expect(await h.invoke("indus:preview:capture", { guestContentsId: 46 })).toEqual({
      dataUrl: "data:image/png;base64,AAAA",
    });
    await expect(h.invoke("indus:preview:capture", { guestContentsId: 46 }, 12)).rejects.toThrow(
      /^not-found/,
    );
    await expect(h.invoke("indus:preview:capture", { guestContentsId: 99 })).rejects.toThrow(
      /^not-found/,
    );
    guest.image = { isEmpty: () => true, toDataURL: () => "data:," };
    await expect(h.invoke("indus:preview:capture", { guestContentsId: 46 })).rejects.toThrow(
      /^capture-failed/,
    );
  });

  it("live reload: a debounced change event to the owning window; secrets and outside refused", async () => {
    const file = path.join(base, "page.html");
    expect(await h.invoke("indus:preview:watch-file", { path: file })).toEqual({ watchId: "pw1" });
    raws[0]?.listener("change", "page.html");
    raws[0]?.listener("change", "page.html");
    clock += WATCH_DEBOUNCE_MS;
    for (const t of timers.filter((x) => x.at <= clock)) t.fn();
    expect(h.window(11).events("indus:preview:file-changed")).toEqual([
      { watchId: "pw1", path: file },
    ]);
    expect(h.window(12).events("indus:preview:file-changed")).toEqual([]);
    await expect(
      h.invoke("indus:preview:watch-file", { path: path.join(base, ".env") }),
    ).rejects.toThrow(/^sensitive-file/);
    await expect(h.invoke("indus:preview:watch-file", { path: "/etc/hosts" })).rejects.toThrow(
      /^outside-roots/,
    );
    expect(await h.invoke("indus:preview:unwatch-file", { watchId: "pw1" })).toEqual({ ok: true });
  });
});
