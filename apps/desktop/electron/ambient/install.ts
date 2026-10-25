// Main's ambient side (P20-35, P20-36): the HUD, quick entry and the global shortcuts, the tray,
// keep awake, notifications and link previews, wired into the IPC registry. `main.ts` calls
// `installAmbient(context, electron, { quitGuard })` once, after `installBackend` (so its quit hook
// sees whether the quit guard stopped a quit). With `options.prefs` (main passes the P20-32
// desktop preferences) the tray, keep-awake, notification and shortcut settings live in that one
// store (`prefsBridge.ts`).

import { randomUUID } from "node:crypto";
import path from "node:path";
import { BRAND_NAME, BRAND_SHORT } from "@indus/ui";
import type { MainContext } from "../context.ts";
import type { EventChannel } from "../ipc/channels.ts";
import type { EventTarget } from "../ipc/registry.ts";
import type { PayloadOf } from "../ipc/schemas.ts";
import type { QuitGuard } from "../quitGuard.ts";
import type { WindowKind } from "../windows/factory.ts";
import type { BrowserWindowLike } from "../windows/manager.ts";
import { ActiveSessions } from "./activeSession.ts";
import { GdbusClient } from "./dbus.ts";
import { type FloatingWindow, HudController, type ScreenLike } from "./hud.ts";
import { KeepAwake, type PowerSaveBlockerLike } from "./keepAwake.ts";
import { type LinkPreviewDeps, LinkPreviewer, type LinkWindowLike } from "./linkPreview.ts";
import {
  type ElectronNotificationLike,
  type ElectronNotificationOptions,
  NotificationCenter,
} from "./notifications.ts";
import {
  ambientFromPrefs,
  type PrefsPort,
  prefsPatchFromAmbient,
  prefsPatchFromShortcuts,
  sameShortcuts,
  shortcutsFromPrefs,
} from "./prefsBridge.ts";
import { QuickEntryController } from "./quickEntry.ts";
import { AmbientSettingsStore } from "./settings.ts";
import {
  type AcceleratorRefusal,
  type GlobalShortcutLike,
  parseAccelerator,
  readShortcutSettings,
  type ShortcutAction,
  ShortcutBoard,
  type ShortcutSettings,
  writeShortcutSettings,
} from "./shortcuts.ts";
import { type MenuItem, TrayController, type TrayLike, type TrayWindow } from "./tray.ts";

/** The Electron pieces this step uses (passed by main.ts so the module stays testable). */
export interface AmbientElectron {
  app: {
    on(event: "before-quit", listener: (event: { defaultPrevented?: boolean }) => void): unknown;
    on(event: "will-quit", listener: () => void): unknown;
    quit(): void;
    dock?: { hide(): void; show(): void } | undefined;
  };
  globalShortcut: GlobalShortcutLike;
  screen: ScreenLike;
  powerSaveBlocker: PowerSaveBlockerLike;
  powerMonitor: {
    on(event: "suspend" | "resume", listener: () => void): unknown;
    getSystemIdleTime(): number;
  };
  /** A tray with our glyph (or the packaged icon); throws when the platform has none. */
  createTray(): TrayLike;
  buildMenu(items: MenuItem[]): unknown;
  notification: {
    isSupported(): boolean;
    create(options: ElectronNotificationOptions): ElectronNotificationLike;
  };
  /** `new BrowserWindow(options)` for the hidden link-preview window. */
  createLinkWindow(options: Record<string, unknown>): LinkWindowLike;
  /** Fetch for link previews (main's network stack, the link partition). */
  fetch: LinkPreviewDeps["fetch"];
}

export interface AmbientHandle {
  hud: HudController;
  quick: QuickEntryController;
  shortcuts: ShortcutBoard;
  tray: TrayController;
  keepAwake: KeepAwake;
  notifications: NotificationCenter;
  links: LinkPreviewer;
  activeSessions: ActiveSessions;
}

const APP_WINDOW_KINDS: ReadonlySet<WindowKind> = new Set(["main", "secondary", "instance"]);

export function installAmbient(
  context: MainContext,
  electron: AmbientElectron,
  backend: { quitGuard: QuitGuard },
  options: { prefs?: PrefsPort } = {},
): AmbientHandle {
  const { registry, windows, paths, log } = context;
  const line = (text: string) => log.write(text);
  const timers = {
    setTimeout: (fn: () => void, ms: number) => setTimeout(fn, ms),
    clearTimeout: (handle: unknown) => clearTimeout(handle as ReturnType<typeof setTimeout>),
  };
  const broadcast = <N extends EventChannel>(name: N, payload: PayloadOf<N>) => {
    for (const win of windows.windows())
      registry.emit(win.webContents as EventTarget, name, payload);
  };
  const emitTo = <N extends EventChannel>(
    win: BrowserWindowLike,
    name: N,
    payload: PayloadOf<N>,
  ): boolean => registry.emit(win.webContents as EventTarget, name, payload);

  // -- last-active session ----------------------------------------------------------------------

  const activeSessions = new ActiveSessions();
  const isAppWindow = (contentsId: number) => {
    const kind = windows.kindOf(contentsId);
    return kind !== null && APP_WINDOW_KINDS.has(kind) && windows.isKnownWebContents(contentsId);
  };
  const lastActive = () => {
    const report = activeSessions.latest(isAppWindow);
    if (report === null) return null;
    const win = windows.windowOf(report.contentsId);
    return win === null ? null : { ...report, window: win };
  };

  // -- settings ---------------------------------------------------------------------------------

  const settings = new AmbientSettingsStore(path.join(paths.userData, "ambient.json"), line);
  const shortcutsFile = path.join(paths.userData, "shortcuts.json");
  let shortcutSettings: ShortcutSettings = readShortcutSettings(shortcutsFile);
  const prefs = options.prefs ?? null;
  const refusedPref = (action: ShortcutAction) =>
    line(`[shortcuts] the saved ${action} chord is not valid; keeping the previous one`);
  if (prefs !== null)
    shortcutSettings = shortcutsFromPrefs(prefs.get(), shortcutSettings, refusedPref);
  /** The ambient settings in force (from the preferences when main shares them). */
  const current = () =>
    prefs === null
      ? settings.get()
      : ambientFromPrefs(prefs.get(), settings.get().keepAwakeOffered);

  // -- HUD, quick entry, shortcuts ---------------------------------------------------------------

  let hud: HudController;
  let quick: QuickEntryController;
  const board = new ShortcutBoard({
    globalShortcut: electron.globalShortcut,
    handlers: {
      "hud-toggle": () =>
        hud.toggle(() => {
          const active = lastActive();
          return {
            sessionId: active?.sessionId ?? null,
            profile:
              active === null ? null : (windows.describe(active.contentsId)?.profile ?? null),
          };
        }),
      "hud-snap": () => hud.snapToPointer(),
      "quick-entry": () => quick.toggle(),
    },
    log: line,
  });
  const applyShortcuts = () =>
    board.apply(shortcutSettings, (action) => action !== "hud-snap" || hud.state().open);

  hud = new HudController({
    create: (options, bounds) => windows.openHud(options, bounds) as FloatingWindow,
    mainWindow: () => windows.mainWindow() as (BrowserWindowLike & { hide(): void }) | null,
    screen: electron.screen,
    statePath: path.join(paths.userData, "hud-state.json"),
    timers,
    now: Date.now,
    broadcast: (state) => broadcast("indus:hud:changed", state),
    onOpenChanged: () => applyShortcuts(),
    log: line,
    platform: context.platform,
  });

  quick = new QuickEntryController({
    create: (bounds) => windows.openQuick(bounds) as FloatingWindow,
    screen: electron.screen,
    target: () => {
      const active = lastActive();
      if (active !== null)
        return {
          contentsId: active.contentsId,
          sessionId: active.sessionId,
          window: active.window,
        };
      const main = windows.mainWindow();
      return main === null
        ? null
        : { contentsId: main.webContents.id, sessionId: null, window: main };
    },
    deliver: (target, payload) => emitTo(target.window, "indus:quick:deliver", payload),
    shown: (win) => emitTo(win, "indus:quick:shown", {}),
    raise: (win) => windows.raise(win, "open-request"),
    timers,
    newId: () => randomUUID(),
    log: line,
    platform: context.platform,
  });

  const shortcutsView = (rejected?: { action: ShortcutAction; reason: AcceleratorRefusal }) => ({
    shortcuts: board.current(),
    ...(rejected === undefined ? {} : { rejected }),
  });

  registry.handle("indus:hud:open", ({ sessionId, profile }, { senderId }) => {
    const from = windows.describe(senderId);
    return hud.open(sessionId ?? null, profile ?? from?.profile ?? null);
  });
  registry.handle("indus:hud:close", () => hud.close());
  registry.handle("indus:hud:reset-layout", () => ({ reset: hud.resetLayout() }));
  registry.on("indus:hud:drag", ({ phase, mode }, { senderId }) =>
    hud.dragFrom(senderId, phase, mode),
  );

  registry.handle("indus:quick:submit", ({ text }, { senderId }) => quick.submit(senderId, text));
  registry.on("indus:quick:dismiss", (_payload, { senderId }) => {
    if (quick.isQuick(senderId)) quick.hide();
  });
  registry.on("indus:quick:ack", (answer, { senderId }) => quick.ack(senderId, answer));

  registry.handle("indus:shortcuts:get", () => shortcutsView());
  registry.handle("indus:shortcuts:set", ({ action, accelerator, enabled }) => {
    const next: ShortcutSettings = {
      ...shortcutSettings,
      [action]: { ...shortcutSettings[action] },
    };
    if (accelerator !== undefined) {
      const parsed = parseAccelerator(accelerator);
      if (!parsed.ok) return shortcutsView({ action, reason: parsed.reason });
      next[action].accelerator = parsed.accelerator;
    }
    if (enabled !== undefined) next[action].enabled = enabled;
    shortcutSettings = next;
    try {
      if (prefs !== null) prefs.set(prefsPatchFromShortcuts(shortcutSettings));
      else writeShortcutSettings(shortcutsFile, shortcutSettings);
    } catch {
      line("[shortcuts] could not save the shortcuts; they apply until the app quits");
    }
    applyShortcuts();
    if (action === "quick-entry" && !shortcutSettings["quick-entry"].enabled) quick.close();
    return shortcutsView();
  });

  registry.on("indus:session:active", ({ sessionId }, { senderId }) => {
    if (isAppWindow(senderId)) activeSessions.report(senderId, sessionId);
  });

  // -- tray --------------------------------------------------------------------------------------

  const dbus = context.platform === "linux" ? new GdbusClient() : null;
  const ambientView = () => {
    const now = current();
    return {
      tray: tray.status(),
      keepAwake: now.keepAwake,
      notifications: now.notifications,
      sounds: now.sounds,
    };
  };
  const tray: TrayController = new TrayController({
    platform: context.platform,
    createTray: electron.createTray,
    buildMenu: electron.buildMenu,
    hostCheck: async () => (dbus === null ? { ok: true as const } : dbus.statusNotifierHost()),
    quit: () => electron.app.quit(),
    ensureMainWindow: () => {
      const main = windows.mainWindow();
      if (main === null) windows.openMain();
      else windows.raise(main, "app-activate");
    },
    dock: electron.app.dock,
    timers,
    log: line,
    onChange: () => broadcast("indus:ambient:changed", ambientView()),
    labels: { show: `Show ${BRAND_SHORT}`, quit: `Quit ${BRAND_SHORT}`, tooltip: BRAND_NAME },
  });

  windows.onWindowCreated((win, kind) => {
    if (!APP_WINDOW_KINDS.has(kind)) return;
    const id = win.webContents.id;
    tray.register(win as TrayWindow, { closeToTray: kind === "main" });
    win.on("focus", () => activeSessions.touch(id));
    win.on("closed", () => activeSessions.forget(id));
  });

  // -- keep awake --------------------------------------------------------------------------------

  const keepAwake = new KeepAwake({
    blocker: electron.powerSaveBlocker,
    mode: () => current().keepAwake,
    offered: () => settings.get().keepAwakeOffered,
    markOffered: () => {
      settings.update({ keepAwakeOffered: true });
    },
    offer: () => {
      const main = windows.mainWindow();
      return main !== null && emitTo(main, "indus:keep-awake:offer", {});
    },
    log: line,
  });
  backend.quitGuard.subscribe(() => keepAwake.setWorking(backend.quitGuard.work().length > 0));
  keepAwake.reconcile();
  electron.powerMonitor.on("suspend", () => {
    let idle = 0;
    try {
      idle = electron.powerMonitor.getSystemIdleTime();
    } catch {
      idle = 0;
    }
    keepAwake.noteSuspend(idle);
  });
  electron.powerMonitor.on("resume", () => keepAwake.noteResume());

  // -- notifications and link previews -----------------------------------------------------------

  const notifications = new NotificationCenter({
    platform: context.platform,
    appName: BRAND_NAME,
    isSupported: () => electron.notification.isSupported(),
    create: (options) => electron.notification.create(options),
    dbus,
    enabled: () => current().notifications,
    activate: (senderId, tag, actionId) => {
      let win = windows.windowOf(senderId) ?? windows.mainWindow();
      if (win === null) win = windows.openMain();
      windows.raise(win, "notification-click");
      emitTo(win, "indus:notification:action", { tag, actionId });
    },
    log: line,
  });
  registry.handle("indus:notify", (request, { senderId }) => notifications.show(senderId, request));

  const links = new LinkPreviewer({
    fetch: electron.fetch,
    createWindow: electron.createLinkWindow,
    timers,
    now: Date.now,
    log: line,
  });
  registry.handle("indus:links:preview", ({ url }) => links.preview(url));

  // -- ambient settings --------------------------------------------------------------------------

  registry.handle("indus:ambient:get", () => ambientView());
  registry.handle("indus:ambient:set", async (patch) => {
    if (prefs !== null) {
      // the preferences listener below applies it and tells every window
      prefs.set(prefsPatchFromAmbient(patch));
      return ambientView();
    }
    const update: Parameters<AmbientSettingsStore["update"]>[0] = {};
    if (patch.minimizeToTray !== undefined) update.minimizeToTray = patch.minimizeToTray;
    if (patch.keepAwake !== undefined) update.keepAwake = patch.keepAwake;
    if (patch.notifications !== undefined) update.notifications = patch.notifications;
    if (patch.sounds !== undefined) update.sounds = patch.sounds;
    settings.update(update);
    if (patch.keepAwake !== undefined) keepAwake.reconcile();
    if (patch.minimizeToTray !== undefined) await tray.setEnabled(patch.minimizeToTray);
    const view = ambientView();
    broadcast("indus:ambient:changed", view);
    return view;
  });

  // -- start and stop ----------------------------------------------------------------------------

  if (prefs !== null) {
    let applied = current();
    prefs.onChange((value) => {
      const next = ambientFromPrefs(value, settings.get().keepAwakeOffered);
      if (next.keepAwake !== applied.keepAwake) keepAwake.reconcile();
      if (next.minimizeToTray !== applied.minimizeToTray) void tray.setEnabled(next.minimizeToTray);
      applied = next;
      const shortcuts = shortcutsFromPrefs(value, shortcutSettings, refusedPref);
      if (!sameShortcuts(shortcuts, shortcutSettings)) {
        shortcutSettings = shortcuts;
        applyShortcuts();
        if (!shortcutSettings["quick-entry"].enabled) quick.close();
      }
      broadcast("indus:ambient:changed", ambientView());
    });
  }

  applyShortcuts();
  void tray.setEnabled(current().minimizeToTray);

  electron.app.on("before-quit", (event) => {
    // Only a quit the quit guard let through stops tray hiding (a cancelled one keeps it).
    if (event.defaultPrevented !== true) tray.beginQuit();
  });
  electron.app.on("will-quit", () => {
    board.dispose();
    keepAwake.dispose();
    notifications.dispose();
    tray.dispose();
  });

  return {
    hud,
    quick,
    shortcuts: board,
    tray,
    keepAwake,
    notifications,
    links,
    activeSessions,
  };
}
