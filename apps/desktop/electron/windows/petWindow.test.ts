import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FakeTimers } from "../../testing/fakeTimers.ts";
import { fileAppOrigin } from "../appOrigin.ts";
import type { MainContext } from "../context.ts";
import { type IpcMainLike, IpcRegistry } from "../ipc/registry.ts";
import type { FrameLike, IpcEventLike } from "../ipc/senderCheck.ts";
import type { BrowserWindowLike, WebContentsLike } from "./manager.ts";
import { WindowManager } from "./manager.ts";
import {
  installPetWindow,
  PET_BUBBLE_MS,
  PET_FULLSCREEN_POLL_MS,
  PetController,
  type PetWindowLike,
} from "./petWindow.ts";
import { installWakeIndicator } from "./wakeIndicator.ts";

type Handler = (...args: unknown[]) => void;
const AREA = { x: 0, y: 25, width: 1440, height: 875 };
const PNG = "data:image/png;base64,iVBORw0KGgo=";

let nextId = 500;
function fakeWindow(options: Record<string, unknown> = {}) {
  const handlers = new Map<string, Handler[]>();
  let url = "";
  let destroyed = false;
  let fullscreen = false;
  let focused = false;
  const contents: WebContentsLike = {
    id: nextId++,
    on: () => contents,
    reload: vi.fn(),
    getURL: () => url,
    getZoomLevel: () => 0,
    setZoomLevel: vi.fn(),
    isDestroyed: () => destroyed,
    send: vi.fn(),
  };
  const win = {
    options,
    webContents: contents,
    on: (event: string, fn: Handler) => {
      handlers.set(event, [...(handlers.get(event) ?? []), fn]);
      return win;
    },
    emit: (event: string) => {
      for (const fn of handlers.get(event) ?? []) fn();
    },
    loadURL: vi.fn(async (next: string) => {
      url = next;
    }),
    show: vi.fn(),
    showInactive: vi.fn(),
    hide: vi.fn(),
    focus: vi.fn(),
    restore: vi.fn(),
    minimize: vi.fn(),
    maximize: vi.fn(),
    unmaximize: vi.fn(),
    close: vi.fn(() => {
      destroyed = true;
      win.emit("closed");
    }),
    setBounds: vi.fn(),
    setIgnoreMouseEvents: vi.fn(),
    setAlwaysOnTop: vi.fn(),
    setVisibleOnAllWorkspaces: vi.fn(),
    isDestroyed: () => destroyed,
    isVisible: () => false,
    isMinimized: () => false,
    isMaximized: () => false,
    isFullScreen: () => fullscreen,
    isFocused: () => focused,
    setFullscreen: (on: boolean) => {
      fullscreen = on;
      focused = on;
    },
    getNormalBounds: () => ({ x: 0, y: 0, width: 800, height: 600 }),
  };
  return win;
}

type FakeWin = ReturnType<typeof fakeWindow>;

class Timers extends FakeTimers {
  setInterval = (fn: () => void, ms: number): unknown => {
    const tick = () => {
      fn();
      handle.id = this.setTimeout(tick, ms);
    };
    const handle = { id: this.setTimeout(tick, ms) };
    return handle;
  };
  clearInterval = (handle: unknown): void => {
    this.clearTimeout((handle as { id: unknown }).id);
  };
}

function controller(options: { fullscreen?: () => boolean } = {}) {
  const timers = new Timers();
  const made: FakeWin[] = [];
  const views: unknown[] = [];
  const kinds = new Map<number, string>();
  const pet = new PetController({
    create: (bounds) => {
      const win = fakeWindow({ bounds });
      made.push(win);
      kinds.set(win.webContents.id, "pet");
      return win as unknown as PetWindowLike;
    },
    screen: { getPrimaryDisplay: () => ({ workArea: AREA }) },
    kindOf: (id) => kinds.get(id) ?? null,
    fullscreenFocused: options.fullscreen ?? (() => false),
    emit: (_win, view) => views.push(view),
    timers,
    platform: "darwin",
    log: vi.fn(),
  });
  return { pet, timers, made, views, kinds };
}

const pictures = { name: "Otto", scale: 1, revision: "r1", moods: { rest: PNG, walk: PNG } };

describe("PetController", () => {
  it("opens one click-through overlay over the work area and pushes the pictures", () => {
    const { pet, made, views } = controller();
    expect(pet.show(pictures)).toEqual({ visible: true });
    expect(pet.show(pictures)).toEqual({ visible: true });
    expect(made).toHaveLength(1);
    const win = made[0] as FakeWin;
    expect(win.options).toEqual({ bounds: AREA });
    expect(win.setIgnoreMouseEvents).toHaveBeenCalledWith(true, { forward: true });
    expect(win.setVisibleOnAllWorkspaces).toHaveBeenCalledWith(true, {
      visibleOnFullScreen: false,
    });
    expect(views.at(-1)).toMatchObject({ visible: true, name: "Otto", moods: pictures.moods });
    expect(win.focus).not.toHaveBeenCalled();
  });

  it("only the figure takes clicks", () => {
    const { pet, made } = controller();
    pet.show(pictures);
    const win = made[0] as FakeWin;
    pet.pointer(true);
    pet.pointer(true);
    expect(win.setIgnoreMouseEvents).toHaveBeenLastCalledWith(false, {});
    pet.pointer(false);
    expect(win.setIgnoreMouseEvents).toHaveBeenLastCalledWith(true, { forward: true });
    expect(win.setIgnoreMouseEvents).toHaveBeenCalledTimes(3);
  });

  it("hides while a full-screen window is focused and comes back after", () => {
    let full = false;
    const { pet, made, timers, views } = controller({ fullscreen: () => full });
    pet.show(pictures);
    const win = made[0] as FakeWin;
    full = true;
    timers.advance(PET_FULLSCREEN_POLL_MS);
    expect(win.hide).toHaveBeenCalledTimes(1);
    expect(views.at(-1)).toMatchObject({ visible: false });
    full = false;
    timers.advance(PET_FULLSCREEN_POLL_MS);
    expect(win.showInactive).toHaveBeenCalledTimes(1);
    expect(views.at(-1)).toMatchObject({ visible: true });
  });

  it("the bubble clears itself; hide closes the overlay and stops polling", () => {
    const { pet, made, timers, views } = controller();
    pet.show(pictures);
    pet.say("  Reading the tests…  ");
    expect(views.at(-1)).toMatchObject({ bubble: "Reading the tests…" });
    timers.advance(PET_BUBBLE_MS);
    expect(views.at(-1)).toMatchObject({ bubble: null });
    pet.hide();
    expect(made[0]?.close).toHaveBeenCalled();
    expect(timers.pending()).toBe(0);
    expect(pet.view()).toMatchObject({ visible: false, name: null });
  });
});

// -- the channels behind the registry's sender checks (D-16.18) ------------------------------------

const ORIGIN = fileAppOrigin("/opt/Indus/dist/index.html", "linux");
type Listener = (event: IpcEventLike, ...args: unknown[]) => unknown;

function frame(url: string): FrameLike {
  return { url, processId: 7, routingId: 1, frameTreeNodeId: 1 };
}

function eventFrom(win: BrowserWindowLike, type = "window"): IpcEventLike {
  const main = frame(win.webContents.getURL());
  return {
    sender: { id: win.webContents.id, mainFrame: main, getType: () => type },
    senderFrame: main,
  };
}

const dirs: string[] = [];
afterEach(() => {
  for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
});

function app() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "indus-pet-"));
  dirs.push(dir);
  const timers = new Timers();
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
  let windows: WindowManager | null = null;
  const registry = new IpcRegistry({
    ipc,
    policy: {
      appOrigin: ORIGIN,
      isKnownWebContents: (id) => windows?.isKnownWebContents(id) ?? false,
    },
    log: vi.fn(),
  });
  const created: FakeWin[] = [];
  windows = new WindowManager({
    createWindow: (options) => {
      const win = fakeWindow(options);
      created.push(win);
      return win as unknown as BrowserWindowLike;
    },
    screen: {
      getAllDisplays: () => [{ workArea: AREA }],
      getPrimaryDisplay: () => ({ workArea: AREA }),
    },
    appOrigin: ORIGIN,
    preloadPath: "/opt/Indus/preload.cjs",
    dev: false,
    platform: "linux",
    title: "Indus",
    windowStatePath: path.join(dir, "w.json"),
    zoomStatePath: path.join(dir, "z.json"),
    timers,
    now: timers.clock,
    log: vi.fn(),
    prepareContents: () => {},
    emitWindowState: () => {},
    emitZoom: () => {},
  });
  const context = {
    registry,
    windows,
    platform: "linux",
    log: { write: vi.fn() },
  } as unknown as MainContext;
  const screen = { getPrimaryDisplay: () => ({ workArea: AREA }) };
  const pet = installPetWindow(context, { screen });
  const wake = installWakeIndicator(context, { screen });
  registry.seal();
  const main = windows.openMain();
  return { handles, ons, windows, pet, wake, main, created };
}

describe("pet and wake channels pass the sender checks", () => {
  it("an app window shows the pet but may not read the pet window's state", async () => {
    const { handles, main } = app();
    const show = handles.get("indus:pet:show") as Listener;
    expect(await show(eventFrom(main), pictures)).toEqual({ ok: true, value: { visible: true } });
    const get = handles.get("indus:pet:get") as Listener;
    expect(await get(eventFrom(main))).toMatchObject({ ok: false, code: "sender-rejected" });
  });

  it("from the real pet window: get works, pointer works, show is refused", async () => {
    const { handles, ons, main, pet, created } = app();
    await (handles.get("indus:pet:show") as Listener)(eventFrom(main), pictures);
    const petWin = created.find((w) => w.webContents.id === pet.contentsId()) as FakeWin;
    expect(petWin.webContents.getURL()).toContain("win=pet");
    const fromPet = eventFrom(petWin as unknown as BrowserWindowLike);
    const got = await (handles.get("indus:pet:get") as Listener)(fromPet);
    expect(got).toMatchObject({ ok: true, value: { visible: true, name: "Otto" } });
    (ons.get("indus:pet:pointer") as Listener)(fromPet, { over: true });
    expect(petWin.setIgnoreMouseEvents).toHaveBeenLastCalledWith(false, {});
    const refused = await (handles.get("indus:pet:show") as Listener)(fromPet, pictures);
    expect(refused).toMatchObject({ ok: false, code: "sender-rejected" });
    const guest = eventFrom(petWin as unknown as BrowserWindowLike, "webview");
    expect(await (handles.get("indus:pet:get") as Listener)(guest)).toMatchObject({
      ok: false,
      code: "sender-rejected",
    });
  });

  it("a bad mood picture is a bad request", async () => {
    const { handles, main } = app();
    const answer = await (handles.get("indus:pet:show") as Listener)(eventFrom(main), {
      ...pictures,
      moods: { rest: "javascript:alert(1)" },
    });
    expect(answer).toMatchObject({ ok: false, code: "bad-request" });
  });

  it("overlays close with the last app window", async () => {
    const { handles, ons, main, pet, wake } = app();
    await (handles.get("indus:pet:show") as Listener)(eventFrom(main), pictures);
    (ons.get("indus:wake:indicator") as Listener)(eventFrom(main), { state: "listening" });
    expect(wake.state()).toBe("listening");
    main.close();
    expect(pet.contentsId()).toBeNull();
    expect(wake.state()).toBe("hidden");
  });
});
