import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FakeTimers } from "../../testing/fakeTimers.ts";
import { fileAppOrigin } from "../appOrigin.ts";
import {
  type BrowserWindowLike,
  REVEAL_FALLBACK_MS,
  type WebContentsLike,
  WindowManager,
} from "./manager.ts";

type Handler = (...args: unknown[]) => void;

let nextId = 100;
function fakeWindow(options: Record<string, unknown>) {
  const winHandlers = new Map<string, Handler[]>();
  const contentsHandlers = new Map<string, Handler[]>();
  let url = "";
  let zoom = 0;
  let destroyed = false;
  let maximized = false;
  const contents: WebContentsLike & { emit(e: string, ...a: unknown[]): void } = {
    id: nextId++,
    on: (event, listener) => {
      contentsHandlers.set(event, [...(contentsHandlers.get(event) ?? []), listener as Handler]);
      return contents;
    },
    reload: vi.fn(),
    getURL: () => url,
    getZoomLevel: () => zoom,
    setZoomLevel: (level) => {
      zoom = level;
    },
    isDestroyed: () => destroyed,
    send: vi.fn(),
    emit: (event, ...args) => {
      for (const h of contentsHandlers.get(event) ?? []) h(...args);
    },
  };
  const win = {
    options,
    webContents: contents,
    on: (event: string, listener: Handler) => {
      winHandlers.set(event, [...(winHandlers.get(event) ?? []), listener]);
      return win;
    },
    emit: (event: string, ...args: unknown[]) => {
      for (const h of winHandlers.get(event) ?? []) h(...args);
    },
    loadURL: vi.fn(async (next: string) => {
      url = next;
    }),
    show: vi.fn(),
    focus: vi.fn(),
    restore: vi.fn(),
    minimize: vi.fn(),
    maximize: vi.fn(() => {
      maximized = true;
    }),
    unmaximize: vi.fn(() => {
      maximized = false;
    }),
    close: vi.fn(() => {
      win.emit("close");
      destroyed = true;
      win.emit("closed");
    }),
    isDestroyed: () => destroyed,
    isVisible: () => false,
    isMinimized: () => false,
    isMaximized: () => maximized,
    isFullScreen: () => false,
    isFocused: () => false,
    getNormalBounds: () => ({ x: 10, y: 20, width: 1100, height: 800 }),
    setTitleBarOverlay: vi.fn(),
  };
  return win;
}

const dirs: string[] = [];
afterEach(() => {
  for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
});

function setup() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "indus-manager-"));
  dirs.push(dir);
  const timers = new FakeTimers();
  const created: Array<ReturnType<typeof fakeWindow>> = [];
  const prepared: string[] = [];
  const emitWindowState = vi.fn();
  const emitZoom = vi.fn();
  const log = vi.fn();
  const manager = new WindowManager({
    createWindow: (options) => {
      const win = fakeWindow(options);
      created.push(win);
      return win as unknown as BrowserWindowLike;
    },
    screen: {
      getAllDisplays: () => [{ workArea: { x: 0, y: 0, width: 1920, height: 1080 } }],
      getPrimaryDisplay: () => ({ workArea: { x: 0, y: 0, width: 1920, height: 1080 } }),
    },
    appOrigin: fileAppOrigin("/app/dist/index.html", "linux"),
    preloadPath: "/app/preload.cjs",
    dev: false,
    platform: "linux",
    title: "Indus",
    windowStatePath: path.join(dir, "window-state.json"),
    zoomStatePath: path.join(dir, "zoom-state.json"),
    timers,
    now: timers.clock,
    log,
    prepareContents: (_contents, kind) => prepared.push(kind),
    emitWindowState,
    emitZoom,
  });
  return { manager, timers, created, prepared, emitWindowState, emitZoom, log, dir };
}

describe("WindowManager", () => {
  it("main window: created hidden through the factory, shown once on the paint signal", () => {
    const { manager, created, prepared, timers } = setup();
    manager.openMain();
    const win = created[0];
    expect(win?.options).toMatchObject({ show: false, titleBarStyle: "hidden" });
    expect(prepared).toEqual(["main"]);
    expect(win?.loadURL).toHaveBeenCalledWith("file:///app/dist/index.html");
    manager.painted(win?.webContents.id ?? -1);
    manager.painted(win?.webContents.id ?? -1);
    timers.advance(REVEAL_FALLBACK_MS);
    expect(win?.show).toHaveBeenCalledTimes(1);
    expect(win?.focus).not.toHaveBeenCalled();
  });

  it("shows the window after the fallback when no paint signal comes", () => {
    const { manager, created, timers } = setup();
    manager.openMain();
    timers.advance(REVEAL_FALLBACK_MS - 1);
    expect(created[0]?.show).not.toHaveBeenCalled();
    timers.advance(1);
    expect(created[0]?.show).toHaveBeenCalledTimes(1);
  });

  it("knows its web contents (for the sender check) until the window closes", () => {
    const { manager, created } = setup();
    manager.openMain();
    const id = created[0]?.webContents.id ?? -1;
    expect(manager.isKnownWebContents(id)).toBe(true);
    expect(manager.isKnownWebContents(id + 1000)).toBe(false);
    created[0]?.close();
    expect(manager.isKnownWebContents(id)).toBe(false);
    expect(manager.mainWindow()).toBeNull();
  });

  it("saves geometry debounced while moving and at close", () => {
    const { manager, created, timers, dir } = setup();
    manager.openMain();
    created[0]?.emit("move");
    created[0]?.emit("resize");
    expect(fs.existsSync(path.join(dir, "window-state.json"))).toBe(false);
    timers.advance(500);
    expect(JSON.parse(fs.readFileSync(path.join(dir, "window-state.json"), "utf8"))).toEqual({
      x: 10,
      y: 20,
      width: 1100,
      height: 800,
      maximized: false,
    });
    created[0]?.maximize();
    created[0]?.close();
    expect(JSON.parse(fs.readFileSync(path.join(dir, "window-state.json"), "utf8")).maximized).toBe(
      true,
    );
  });

  it("restores a maximized main window", () => {
    const { manager, created, dir } = setup();
    fs.writeFileSync(
      path.join(dir, "window-state.json"),
      JSON.stringify({ x: 0, y: 0, width: 900, height: 700, maximized: true }),
    );
    manager.openMain();
    expect(created[0]?.maximize).toHaveBeenCalled();
    expect(created[0]?.options).toMatchObject({ x: 0, y: 0, width: 900, height: 700 });
  });

  it("one secondary window per session; opening again raises it (a user action)", () => {
    const { manager, created } = setup();
    manager.openSecondary({ sessionId: "s1", watch: true, profile: "work" });
    manager.openSecondary({ sessionId: "s1" });
    expect(created).toHaveLength(1);
    expect(created[0]?.loadURL).toHaveBeenCalledWith(
      "file:///app/dist/index.html?win=secondary&watch=1&profile=work#/s1",
    );
    expect(created[0]?.focus).toHaveBeenCalledTimes(1);
    expect(manager.describe(created[0]?.webContents.id ?? -1)).toEqual({
      kind: "secondary",
      watch: true,
      profile: "work",
    });
  });

  it("renderer crashes reload at most 3 times in 60 s", () => {
    const { manager, created, timers } = setup();
    manager.openMain();
    const contents = created[0]?.webContents as WebContentsLike & {
      emit(e: string, ...a: unknown[]): void;
    };
    for (let i = 0; i < 4; i += 1) {
      contents.emit("render-process-gone", {}, { reason: "crashed" });
      timers.advance(5_000);
    }
    expect(contents.reload).toHaveBeenCalledTimes(3);
    contents.emit("render-process-gone", {}, { reason: "clean-exit" });
    expect(contents.reload).toHaveBeenCalledTimes(3);
  });

  it("zoom is re-asserted after an in-page navigation and persisted", () => {
    const { manager, created, timers, dir, emitZoom } = setup();
    manager.openMain();
    manager.setZoom(1.5);
    const contents = created[0]?.webContents as WebContentsLike & {
      emit(e: string, ...a: unknown[]): void;
    };
    contents.setZoomLevel(0);
    contents.emit("did-navigate-in-page");
    timers.advance(100);
    expect(contents.getZoomLevel()).toBe(1.5);
    expect(JSON.parse(fs.readFileSync(path.join(dir, "zoom-state.json"), "utf8"))).toEqual({
      level: 1.5,
    });
    expect(emitZoom).toHaveBeenLastCalledWith(contents, { level: 1.5, percent: 131 });
    expect(manager.stepZoomBy("reset")).toEqual({ level: 0, percent: 100 });
  });

  it("window controls and state pushes", () => {
    const { manager, created, emitWindowState } = setup();
    manager.openMain();
    const id = created[0]?.webContents.id ?? -1;
    manager.control(id, "toggle-maximize");
    expect(created[0]?.maximize).toHaveBeenCalled();
    created[0]?.emit("maximize");
    expect(emitWindowState).toHaveBeenLastCalledWith(created[0]?.webContents, {
      maximized: true,
      fullscreen: false,
      focused: false,
    });
    manager.setTitleBar(id, { color: "#111111", symbolColor: "#eeeeee" });
    expect(created[0]?.setTitleBarOverlay).toHaveBeenCalledWith({
      color: "#111111",
      symbolColor: "#eeeeee",
      height: 34,
    });
    manager.control(id, "minimize");
    expect(created[0]?.minimize).toHaveBeenCalled();
  });
});
