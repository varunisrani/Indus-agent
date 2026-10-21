// The ambient and update steps through the real IPC registry (P20-35..37 together): every channel
// they own has a handler; quick entry reaches the last-active session of the right window and
// answers; the HUD opens on that session and every window hears it; a bare-key shortcut is refused;
// notifications and settings round-trip; updates answer without touching the network.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { FakeScreen, FakeWindow } from "../../testing/fakeFloating.ts";
import { fileAppOrigin } from "../appOrigin.ts";
import type { MainContext } from "../context.ts";
import { CHANNELS } from "../ipc/channels.ts";
import type { Envelope } from "../ipc/envelope.ts";
import { type IpcMainLike, IpcRegistry } from "../ipc/registry.ts";
import type { FrameLike, IpcEventLike } from "../ipc/senderCheck.ts";
import { QuitGuard } from "../quitGuard.ts";
import { createUpdateGate, installUpdates } from "../updates/install.ts";
import type { WindowKind } from "../windows/factory.ts";
import { type AmbientElectron, installAmbient } from "./install.ts";

const APP_URL = "file:///opt/Indus/app/dist/index.html";
const OWNED = ["P20-35", "P20-36", "P20-37"];
type Listener = (event: IpcEventLike, ...args: unknown[]) => unknown;

class FakeManager {
  readonly all: Array<{ win: FakeWindow; kind: WindowKind; profile: string | null }> = [];
  private readonly created: Array<(win: FakeWindow, kind: WindowKind) => void> = [];
  raised: Array<[FakeWindow, string]> = [];

  add(kind: WindowKind, profile: string | null = null): FakeWindow {
    const win = new FakeWindow();
    win.visible = kind !== "hud" && kind !== "quick";
    this.all.push({ win, kind, profile });
    for (const listener of this.created) listener(win, kind);
    return win;
  }
  onWindowCreated(listener: (win: FakeWindow, kind: WindowKind) => void) {
    this.created.push(listener);
    return () => {};
  }
  windows() {
    return this.all.filter((w) => !w.win.destroyed).map((w) => w.win);
  }
  mainWindow() {
    return this.all.find((w) => w.kind === "main" && !w.win.destroyed)?.win ?? null;
  }
  openMain() {
    return this.add("main");
  }
  openHud() {
    return this.add("hud");
  }
  openQuick() {
    return this.add("quick");
  }
  private entry(id: number) {
    return this.all.find((w) => w.win.webContents.id === id && !w.win.destroyed);
  }
  kindOf(id: number) {
    return this.entry(id)?.kind ?? null;
  }
  windowOf(id: number) {
    return this.entry(id)?.win ?? null;
  }
  isKnownWebContents = (id: number) => this.entry(id) !== undefined;
  describe(id: number) {
    const entry = this.entry(id);
    return entry === undefined ? null : { kind: entry.kind, watch: false, profile: entry.profile };
  }
  raise(win: FakeWindow, reason: string) {
    this.raised.push([win, reason]);
  }
}

const dirs: string[] = [];
afterEach(() => {
  for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
  delete process.env.INDUS_DESKTOP_NO_UPDATE_CHECK;
});

function rig() {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "ambient-"));
  dirs.push(base);
  const handles = new Map<string, Listener>();
  const ons = new Map<string, Listener>();
  const ipc: IpcMainLike = {
    handle: (channel, listener) => {
      handles.set(channel, listener as Listener);
    },
    on: (channel, listener) => {
      ons.set(channel, listener as Listener);
      return ipc;
    },
  };
  const appOrigin = fileAppOrigin("/opt/Indus/app/dist/index.html", "linux");
  const windows = new FakeManager();
  const registry = new IpcRegistry({
    ipc,
    policy: { appOrigin, isKnownWebContents: (id) => windows.isKnownWebContents(id) },
    log: () => {},
  });
  const lines: string[] = [];
  const context = {
    registry,
    windows,
    log: { write: (line: string) => lines.push(line) },
    paths: { home: path.join(base, "home"), userData: path.join(base, "userData") },
    appOrigin,
    dev: false,
    packaged: false,
    platform: "darwin",
    appVersion: "1.0.0",
  } as unknown as MainContext;
  fs.mkdirSync(path.join(base, "userData"), { recursive: true });
  fs.mkdirSync(path.join(base, "home"), { recursive: true });

  const registered = new Map<string, () => void>();
  const appListeners = new Map<string, (...args: never[]) => void>();
  const notifications: unknown[] = [];
  const screen = new FakeScreen();
  const electron: AmbientElectron = {
    app: {
      on: ((event: string, listener: (...args: never[]) => void) => {
        appListeners.set(event, listener);
        return undefined;
      }) as AmbientElectron["app"]["on"],
      quit: () => {},
      dock: undefined,
    },
    globalShortcut: {
      register: (accelerator, callback) => {
        registered.set(accelerator, callback);
        return true;
      },
      unregister: (accelerator) => {
        registered.delete(accelerator);
      },
      isRegistered: (accelerator) => registered.has(accelerator),
    },
    screen,
    powerSaveBlocker: { start: () => 1, stop: () => {}, isStarted: () => false },
    powerMonitor: { on: () => undefined, getSystemIdleTime: () => 0 },
    createTray: () => {
      throw new Error("no tray in tests");
    },
    buildMenu: (items) => items,
    notification: {
      isSupported: () => true,
      create: (options) => {
        notifications.push(options);
        return { show() {}, close() {}, on: () => undefined };
      },
    },
    createLinkWindow: () => {
      throw new Error("no windows in tests");
    },
    fetch: () => Promise.reject(new Error("no network in tests")),
  };
  const quitGuard = new QuitGuard();
  process.env.INDUS_DESKTOP_NO_UPDATE_CHECK = "1";
  const gate = createUpdateGate(context);
  const ambient = installAmbient(context, electron, { quitGuard });
  const updates = installUpdates(
    context,
    {
      app: {
        getAppPath: () => base,
        on: ((event: string, listener: () => void) => {
          appListeners.set(`updates:${event}`, listener as never);
          return undefined;
        }) as never,
      },
      fetch: () => Promise.reject(new Error("no network in tests")),
      runtimeVersion: () => null,
    },
    { gate, stopAppWork: async () => {} },
  );
  registry.seal();

  const eventFor = (win: FakeWindow): IpcEventLike => {
    const frame: FrameLike = {
      url: `${APP_URL}#/`,
      processId: 1,
      routingId: win.webContents.id,
      frameTreeNodeId: 1,
    };
    return {
      sender: { id: win.webContents.id, mainFrame: frame, getType: () => "window" },
      senderFrame: frame,
    };
  };
  const invoke = async (win: FakeWindow, channel: string, payload?: unknown) => {
    const listener = handles.get(channel);
    if (listener === undefined) throw new Error(`no handler for ${channel}`);
    return (await listener(
      eventFor(win),
      ...(payload === undefined ? [] : [payload]),
    )) as Envelope<unknown>;
  };
  const send = (win: FakeWindow, channel: string, payload?: unknown) =>
    ons.get(channel)?.(eventFor(win), ...(payload === undefined ? [] : [payload]));
  const stop = () => {
    updates.gate.stop();
    updates.updater.stop();
  };
  return {
    handles,
    ons,
    windows,
    invoke,
    send,
    registered,
    notifications,
    ambient,
    screen,
    stop,
    lines,
  };
}

describe("ambient and updates through the registry", () => {
  it("every channel P20-35..37 owns has a handler (none answers not-available)", async () => {
    const r = rig();
    const main = r.windows.add("main");
    for (const def of CHANNELS) {
      if (!OWNED.includes(def.owner) || def.kind === "event") continue;
      expect(r.handles.has(def.name) || r.ons.has(def.name), def.name).toBe(true);
    }
    const status = await r.invoke(main, "indus:updates:status");
    expect(status).toMatchObject({ ok: true, value: { owner: "manual", currentVersion: "1.0.0" } });
    r.stop();
  });

  it("quick entry submits into the last-active session of the window used last", async () => {
    const r = rig();
    const main = r.windows.add("main");
    const second = r.windows.add("secondary");
    r.send(main, "indus:session:active", { sessionId: "s-main" });
    r.send(second, "indus:session:active", { sessionId: "s-second" });
    second.emit("blur");
    main.emit("focus"); // the person went back to the main window

    r.registered.get("CommandOrControl+Shift+Space")?.(); // the global chord
    const quick = r.windows.all.find((w) => w.kind === "quick")?.win as FakeWindow;
    quick.show();
    const pending = r.invoke(quick, "indus:quick:submit", { text: "summarise the log" });
    await new Promise((resolve) => setTimeout(resolve, 0));
    const delivered = main.webContents.sent.find(([channel]) => channel === "indus:quick:deliver");
    expect(delivered?.[1]).toMatchObject({ text: "summarise the log", sessionId: "s-main" });
    expect(second.webContents.sent.some(([c]) => c === "indus:quick:deliver")).toBe(false);
    const requestId = (delivered as [string, { requestId: string }])[1].requestId;
    r.send(main, "indus:quick:ack", { requestId, ok: true, sessionId: "s-main" });
    expect(await pending).toEqual({ ok: true, value: { ok: true, sessionId: "s-main" } });
    expect(quick.visible).toBe(false);
    expect(r.windows.raised.at(-1)).toEqual([main, "open-request"]);
    r.stop();
  });

  it("a quick submit from an app window is refused", async () => {
    const r = rig();
    const main = r.windows.add("main");
    expect(await r.invoke(main, "indus:quick:submit", { text: "x" })).toMatchObject({
      ok: true,
      value: { ok: false, code: "failed" },
    });
    r.stop();
  });

  it("the HUD toggle opens the last-active session; every window hears it; snap is held only meanwhile", async () => {
    const r = rig();
    const main = r.windows.add("main", "work");
    r.send(main, "indus:session:active", { sessionId: "s-9" });
    expect(r.registered.has("CommandOrControl+Shift+G")).toBe(false);
    r.registered.get("CommandOrControl+Shift+H")?.();
    expect(r.ambient.hud.state()).toEqual({ open: true, sessionId: "s-9", profile: "work" });
    expect(main.webContents.sent).toContainEqual([
      "indus:hud:changed",
      { open: true, sessionId: "s-9", profile: "work" },
    ]);
    expect(r.registered.has("CommandOrControl+Shift+G")).toBe(true);
    expect(await r.invoke(main, "indus:hud:close")).toMatchObject({
      ok: true,
      value: { open: false },
    });
    expect(r.registered.has("CommandOrControl+Shift+G")).toBe(false);
    r.stop();
  });

  it("a bare-key shortcut is refused with its reason and nothing is saved", async () => {
    const r = rig();
    const main = r.windows.add("main");
    const refused = await r.invoke(main, "indus:shortcuts:set", {
      action: "quick-entry",
      accelerator: "G",
    });
    expect(refused).toMatchObject({
      ok: true,
      value: { rejected: { action: "quick-entry", reason: "no-modifier" } },
    });
    expect(r.registered.has("CommandOrControl+Shift+Space")).toBe(true);
    const moved = await r.invoke(main, "indus:shortcuts:set", {
      action: "quick-entry",
      accelerator: "ctrl+alt+k",
    });
    expect(moved.ok).toBe(true);
    expect(r.registered.has("Control+Alt+K")).toBe(true);
    expect(r.registered.has("CommandOrControl+Shift+Space")).toBe(false);
    r.stop();
  });

  it("settings and notifications round-trip; a tray that cannot be made reads unavailable", async () => {
    const r = rig();
    const main = r.windows.add("main");
    const set = await r.invoke(main, "indus:ambient:set", { minimizeToTray: true, sounds: false });
    expect(set).toMatchObject({
      ok: true,
      value: {
        tray: { enabled: true, available: false },
        sounds: false,
        keepAwake: "while-working",
      },
    });
    expect(await r.invoke(main, "indus:ambient:set", { keepAwake: "sometimes" })).toMatchObject({
      ok: false,
      code: "bad-request",
    });
    const shown = await r.invoke(main, "indus:notify", { title: "Reply ready", tag: "turn:s1" });
    expect(shown).toEqual({ ok: true, value: { shown: true } });
    expect(r.notifications).toEqual([{ title: "Reply ready", body: "", silent: true }]);
    expect(
      await r.invoke(main, "indus:notify", {
        title: "x",
        actions: [1, 2, 3, 4, 5].map((n) => ({ id: `a${n}`, label: "A" })),
      }),
    ).toMatchObject({ ok: false, code: "bad-request" });
    r.stop();
  });

  it("an unknown window is refused before any handler runs", async () => {
    const r = rig();
    const stranger = new FakeWindow();
    expect(await r.invoke(stranger, "indus:hud:open", {})).toMatchObject({
      ok: false,
      code: "sender-rejected",
    });
    expect(r.ambient.hud.state().open).toBe(false);
    r.stop();
  });
});
