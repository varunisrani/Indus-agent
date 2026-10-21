import { describe, expect, it } from "vitest";
import { FakeWindow } from "../../testing/fakeFloating.ts";
import { FakeTimers } from "../../testing/fakeTimers.ts";
import type { HostCheck } from "./dbus.ts";
import { TRAY_HOST_POLL_MS, TrayController, type TrayLike, type TrayStatus } from "./tray.ts";
import { trayBitmap } from "./trayIcon.ts";

class FakeTray implements TrayLike {
  destroyed = false;
  menu: Array<{ label?: string; click?: () => void }> = [];
  private readonly handlers = new Map<string, () => void>();
  setToolTip(): void {}
  setContextMenu(menu: unknown): void {
    this.menu = menu as typeof this.menu;
  }
  on(event: "click" | "double-click", listener: () => void): unknown {
    this.handlers.set(event, listener);
    return this;
  }
  click(): void {
    this.handlers.get("click")?.();
  }
  destroy(): void {
    this.destroyed = true;
  }
  isDestroyed(): boolean {
    return this.destroyed;
  }
}

function setup(platform: string, host: HostCheck = { ok: true }) {
  const timers = new FakeTimers();
  const trays: FakeTray[] = [];
  const changes: TrayStatus[] = [];
  const state = { host, quits: 0, ensured: 0, createFails: false, dockHidden: 0, dockShown: 0 };
  const tray = new TrayController({
    platform,
    createTray: () => {
      if (state.createFails) throw new Error("no tray here");
      const made = new FakeTray();
      trays.push(made);
      return made;
    },
    buildMenu: (items) => items,
    hostCheck: async () => state.host,
    quit: () => {
      state.quits += 1;
    },
    ensureMainWindow: () => {
      state.ensured += 1;
    },
    dock: {
      hide: () => {
        state.dockHidden += 1;
      },
      show: () => {
        state.dockShown += 1;
      },
    },
    timers,
    log: () => {},
    onChange: (status) => changes.push(status),
    labels: { show: "Show Indus", quit: "Quit Indus", tooltip: "Indus" },
  });
  return { tray, timers, trays, changes, state };
}

function visibleWindow(): FakeWindow {
  const win = new FakeWindow();
  win.visible = true;
  return win;
}

async function flush(): Promise<void> {
  for (let i = 0; i < 5; i += 1) await Promise.resolve();
}

describe("minimise to tray", () => {
  it("off by default: nothing hides", async () => {
    const { tray, timers } = setup("win32");
    const win = visibleWindow();
    tray.register(win, { closeToTray: true });
    win.minimize();
    timers.advance(0);
    expect(win.visible).toBe(true);
    expect(win.emitPreventable("close")).toBe(false);
    expect(tray.status()).toEqual({ enabled: false, available: false, reason: "turned off" });
  });

  it("Linux without a StatusNotifier host: unavailable and windows are never hidden", async () => {
    const { tray, timers, trays } = setup("linux", {
      ok: false,
      reason: "no system tray host is registered",
    });
    const win = visibleWindow();
    tray.register(win, { closeToTray: true });
    const status = await tray.setEnabled(true);
    expect(status).toEqual({
      enabled: true,
      available: false,
      reason: "no system tray host is registered",
    });
    expect(trays).toHaveLength(0);
    win.minimize();
    timers.advance(0);
    expect(win.calls).not.toContain("hide");
    expect(win.emitPreventable("close")).toBe(false);
  });

  it("with a host: minimise hides (after the native minimise), close of main hides, Show restores all", async () => {
    const { tray, timers, trays, state } = setup("linux");
    const main = visibleWindow();
    const other = visibleWindow();
    tray.register(main, { closeToTray: true });
    tray.register(other, { closeToTray: false });
    expect(await tray.setEnabled(true)).toMatchObject({ available: true, reason: null });

    other.minimize();
    expect(other.calls).not.toContain("hide"); // deferred
    timers.advance(0);
    expect(other.calls).toContain("hide");
    expect(main.emitPreventable("close")).toBe(true);
    expect(main.visible).toBe(false);
    expect(tray.hiddenCount()).toBe(2);

    trays[0]?.menu.find((item) => item.label === "Show Indus")?.click?.();
    expect(main.visible && other.visible).toBe(true);
    expect(main.calls).toContain("showInactive");
    expect(tray.hiddenCount()).toBe(0);
    expect(state.ensured).toBe(1);
  });

  it("a window restored before the deferred hide runs is not hidden", async () => {
    const { tray, timers } = setup("darwin");
    const win = visibleWindow();
    tray.register(win, { closeToTray: false });
    await tray.setEnabled(true);
    win.minimize();
    win.restore();
    timers.advance(0);
    expect(win.calls).not.toContain("hide");
  });

  it("Quit goes through app.quit (the quit guard); once quitting, closing is not hidden", async () => {
    const { tray, trays, state } = setup("win32");
    const main = visibleWindow();
    tray.register(main, { closeToTray: true });
    await tray.setEnabled(true);
    trays[0]?.menu.find((item) => item.label === "Quit Indus")?.click?.();
    expect(state.quits).toBe(1);
    tray.beginQuit();
    expect(main.emitPreventable("close")).toBe(false);
  });

  it("Windows: a hidden window leaves the taskbar and comes back activated", async () => {
    const { tray, trays } = setup("win32");
    const win = visibleWindow();
    tray.register(win, { closeToTray: true });
    await tray.setEnabled(true);
    win.close();
    expect(win.skipTaskbar).toBe(true);
    expect(win.calls).toContain("blur");
    trays[0]?.click();
    expect(win.skipTaskbar).toBe(false);
    expect(win.focused).toBe(1);
  });

  it("a hidden window shown by something else (a deep link) stops counting and is activated on Windows", async () => {
    const { tray, timers } = setup("win32");
    const win = visibleWindow();
    tray.register(win, { closeToTray: true });
    await tray.setEnabled(true);
    win.close();
    expect(tray.hiddenCount()).toBe(1);
    win.show(); // P20-34's delivery un-hides it
    expect(tray.hiddenCount()).toBe(0);
    expect(win.skipTaskbar).toBe(false);
    timers.advance(0);
    expect(win.focused).toBe(1);
  });

  it("the host going away restores hidden windows and marks the tray unavailable", async () => {
    const { tray, timers, state, changes } = setup("linux");
    const win = visibleWindow();
    tray.register(win, { closeToTray: true });
    await tray.setEnabled(true);
    win.close();
    expect(win.visible).toBe(false);
    state.host = { ok: false, reason: "no system tray watcher is running" };
    timers.advance(TRAY_HOST_POLL_MS);
    await flush();
    expect(win.visible).toBe(true);
    expect(changes.at(-1)).toEqual({
      enabled: true,
      available: false,
      reason: "no system tray watcher is running",
    });
    expect(win.emitPreventable("close")).toBe(false);
  });

  it("turning it off restores windows (Linux keeps a parked tray for re-use)", async () => {
    const { tray, trays } = setup("linux");
    const win = visibleWindow();
    tray.register(win, { closeToTray: true });
    await tray.setEnabled(true);
    win.close();
    await tray.setEnabled(false);
    expect(win.visible).toBe(true);
    expect(trays[0]?.destroyed).toBe(false);
    await tray.setEnabled(true);
    expect(trays).toHaveLength(1);
  });

  it("a desktop that cannot make a tray: unavailable, nothing hidden", async () => {
    const { tray, state } = setup("win32");
    state.createFails = true;
    const win = visibleWindow();
    tray.register(win, { closeToTray: true });
    expect(await tray.setEnabled(true)).toMatchObject({ enabled: true, available: false });
    expect(win.emitPreventable("close")).toBe(false);
  });

  it("macOS hides the Dock icon only while every app window is hidden", async () => {
    const { tray, state, timers } = setup("darwin");
    const a = visibleWindow();
    const b = visibleWindow();
    tray.register(a, { closeToTray: true });
    tray.register(b, { closeToTray: false });
    await tray.setEnabled(true);
    a.close();
    expect(state.dockHidden).toBe(0); // b is still on screen
    b.minimize();
    timers.advance(0);
    expect(state.dockHidden).toBe(1);
    tray.restoreAll();
    expect(state.dockShown).toBe(1);
  });
});

describe("tray glyph", () => {
  it("is drawn in code: BGRA, square, with transparent corners and an opaque ring", () => {
    const linux = trayBitmap("linux");
    expect(linux.width).toBe(22);
    expect(linux.pixels.length).toBe(22 * 22 * 4);
    expect(linux.pixels[3]).toBe(0);
    expect(Math.max(...linux.pixels.filter((_, i) => i % 4 === 3))).toBe(255);
    const mac = trayBitmap("darwin", 2);
    expect(mac.template).toBe(true);
    expect(mac.width).toBe(36);
  });
});
