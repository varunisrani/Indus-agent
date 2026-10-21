// The desktop pet overlay (P24-19): one transparent window over the primary display's work area,
// created by the window manager as kind `pet`. Main owns where it is and whether it shows; the pet
// window's renderer draws the figure, wanders inside the window (`src/pet/roam.ts`) and says when
// the pointer is over the figure — main then lets that one area take clicks and keeps the rest of
// the window click-through (`setIgnoreMouseEvents(true, { forward: true })` so pointer moves still
// reach the page).
//
// - App windows (main / secondary / instance) show it with the pet's pictures (`pet:show`, fetched
//   from the daemon's `pet.info`), hide it, and give it a short status line (`pet:say`); only the
//   pet window may report the pointer or read its state. Every channel is a registry channel, so the
//   sender checks of P20-03 apply on top of these kind checks (D-16.18).
// - The overlay never takes focus (factory: `focusable: false`) and is hidden while one of our
//   windows is full screen and focused; on macOS it is not shown on full-screen spaces at all.
// - The speech bubble clears itself after `PET_BUBBLE_MS`.

import type { MainContext } from "../context.ts";
import { IpcError } from "../ipc/envelope.ts";
import type { PayloadOf } from "../ipc/schemas.ts";
import type { BrowserWindowLike } from "./manager.ts";
import type { DisplayLike, Rect } from "./windowState.ts";

export const PET_BUBBLE_MS = 6_000;
export const PET_FULLSCREEN_POLL_MS = 1_000;

export type PetView = PayloadOf<"indus:pet:state">;

export interface PetWindowLike extends BrowserWindowLike {
  setBounds(bounds: Rect): void;
  hide(): void;
  showInactive?(): void;
  setIgnoreMouseEvents(ignore: boolean, options?: { forward?: boolean }): void;
  setVisibleOnAllWorkspaces?(visible: boolean, options?: Record<string, boolean>): void;
  setAlwaysOnTop?(flag: boolean, level?: string): void;
}

export interface PetScreen {
  getPrimaryDisplay(): DisplayLike;
  on?(event: "display-metrics-changed" | "display-added" | "display-removed", fn: () => void): void;
}

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
  setInterval(fn: () => void, ms: number): unknown;
  clearInterval(handle: unknown): void;
}

export interface PetDeps {
  create(bounds: Rect): PetWindowLike;
  screen: PetScreen;
  /** The window kind of a web contents (`windows.kindOf`). */
  kindOf(contentsId: number): string | null;
  /** True while one of our windows is full screen and focused. */
  fullscreenFocused(): boolean;
  emit(win: PetWindowLike, view: PetView): void;
  timers: Timers;
  platform: string;
  log(line: string): void;
}

export interface PetPictures {
  name: string;
  scale: number;
  revision: string;
  moods: Record<string, string>;
}

const APP_KINDS: ReadonlySet<string> = new Set(["main", "secondary", "instance"]);

export class PetController {
  private readonly deps: PetDeps;
  private win: PetWindowLike | null = null;
  private pictures: PetPictures | null = null;
  private bubble: string | null = null;
  private bubbleTimer: unknown = null;
  private poll: unknown = null;
  private hiddenForFullscreen = false;
  private clickable = false;

  constructor(deps: PetDeps) {
    this.deps = deps;
    deps.screen.on?.("display-metrics-changed", () => this.place());
    deps.screen.on?.("display-removed", () => this.place());
  }

  /** The pet window's web contents id, or null. */
  contentsId(): number | null {
    return this.win !== null && !this.win.isDestroyed() ? this.win.webContents.id : null;
  }

  view(): PetView {
    const pictures = this.pictures;
    return {
      visible: pictures !== null && !this.hiddenForFullscreen,
      name: pictures?.name ?? null,
      scale: pictures?.scale ?? 1,
      revision: pictures?.revision ?? null,
      moods: pictures?.moods ?? {},
      bubble: this.bubble,
    };
  }

  /** Only app windows may show, hide or speak through the pet. */
  requireApp(senderId: number): void {
    const kind = this.deps.kindOf(senderId);
    if (kind === null || !APP_KINDS.has(kind)) {
      throw new IpcError("sender-rejected", "only an app window may drive the pet");
    }
  }

  /** Only the pet window reads its state and reports the pointer. */
  requirePet(senderId: number): void {
    if (this.deps.kindOf(senderId) !== "pet" || senderId !== this.contentsId()) {
      throw new IpcError("sender-rejected", "only the pet window may do this");
    }
  }

  show(pictures: PetPictures): { visible: boolean } {
    this.pictures = pictures;
    if (this.win === null || this.win.isDestroyed()) this.open();
    this.refresh();
    this.push();
    return { visible: this.view().visible };
  }

  hide(): { visible: boolean } {
    this.pictures = null;
    this.clearBubble();
    this.stopPolling();
    const win = this.win;
    this.win = null;
    if (win !== null && !win.isDestroyed()) win.close();
    return { visible: false };
  }

  say(text: string): void {
    const line = text.trim();
    this.clearBubble();
    this.bubble = line === "" ? null : line;
    if (this.bubble !== null) {
      this.bubbleTimer = this.deps.timers.setTimeout(() => {
        this.bubbleTimer = null;
        this.bubble = null;
        this.push();
      }, PET_BUBBLE_MS);
    }
    this.push();
  }

  /** The pointer entered or left the figure: only the figure takes clicks. */
  pointer(over: boolean): void {
    const win = this.win;
    if (win === null || win.isDestroyed() || over === this.clickable) return;
    this.clickable = over;
    win.setIgnoreMouseEvents(!over, over ? {} : { forward: true });
  }

  /** Hide while a full-screen window of ours is focused; come back after. */
  refresh(): void {
    const win = this.win;
    if (win === null || win.isDestroyed()) return;
    const hide = this.deps.fullscreenFocused();
    if (hide === this.hiddenForFullscreen) return;
    this.hiddenForFullscreen = hide;
    if (hide) win.hide();
    else if (win.showInactive !== undefined) win.showInactive();
    else win.show();
    this.push();
  }

  private open(): void {
    const area = this.deps.screen.getPrimaryDisplay().workArea;
    const win = this.deps.create({ ...area });
    this.win = win;
    this.clickable = false;
    this.hiddenForFullscreen = false;
    win.setIgnoreMouseEvents(true, { forward: true });
    try {
      win.setAlwaysOnTop?.(true, this.deps.platform === "darwin" ? "floating" : "screen-saver");
      // Not on other apps' full-screen spaces (macOS): the OS hides it there.
      win.setVisibleOnAllWorkspaces?.(true, { visibleOnFullScreen: false });
    } catch {
      // window managers without these keep the plain always-on-top window
    }
    win.on("closed", () => {
      if (this.win === win) {
        this.win = null;
        this.stopPolling();
      }
    });
    this.startPolling();
    this.deps.log("[pet] overlay opened");
  }

  private place(): void {
    const win = this.win;
    if (win === null || win.isDestroyed()) return;
    win.setBounds({ ...this.deps.screen.getPrimaryDisplay().workArea });
    this.push();
  }

  private push(): void {
    const win = this.win;
    if (win !== null && !win.isDestroyed()) this.deps.emit(win, this.view());
  }

  private clearBubble(): void {
    if (this.bubbleTimer !== null) {
      this.deps.timers.clearTimeout(this.bubbleTimer);
      this.bubbleTimer = null;
    }
    this.bubble = null;
  }

  private startPolling(): void {
    if (this.poll !== null) return;
    this.poll = this.deps.timers.setInterval(() => this.refresh(), PET_FULLSCREEN_POLL_MS);
  }

  private stopPolling(): void {
    if (this.poll === null) return;
    this.deps.timers.clearInterval(this.poll);
    this.poll = null;
  }
}

/** Close an overlay with the last app window, so it never keeps the app running on its own. */
export function closeWithLastAppWindow(
  windows: Pick<MainContext["windows"], "onWindowCreated" | "windows" | "kindOf">,
  close: () => void,
): void {
  const appKinds = new Set(["main", "secondary", "instance"]);
  windows.onWindowCreated((win, kind) => {
    if (!appKinds.has(kind)) return;
    win.on("closed", () => {
      const left = windows
        .windows()
        .some((w) => !w.isDestroyed() && appKinds.has(windows.kindOf(w.webContents.id) ?? ""));
      if (!left) close();
    });
  });
}

/** Main's pet install step: the controller and its six channels. */
export function installPetWindow(
  context: MainContext,
  electron: { screen: PetScreen },
): PetController {
  const { registry, windows } = context;
  const controller = new PetController({
    create: (bounds) => windows.openPet(bounds) as PetWindowLike,
    screen: electron.screen,
    kindOf: (id) => windows.kindOf(id),
    fullscreenFocused: () =>
      windows.windows().some((w) => w.isFullScreen() && w.isFocused() && !w.isDestroyed()),
    emit: (win, view) => {
      registry.emit(win.webContents, "indus:pet:state", view);
    },
    timers: {
      setTimeout: (fn, ms) => setTimeout(fn, ms),
      clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
      setInterval: (fn, ms) => setInterval(fn, ms),
      clearInterval: (handle) => clearInterval(handle as ReturnType<typeof setInterval>),
    },
    platform: context.platform,
    log: (line) => context.log.write(line),
  });
  closeWithLastAppWindow(windows, () => controller.hide());
  registry.handle("indus:pet:show", (pictures, { senderId }) => {
    controller.requireApp(senderId);
    return controller.show(pictures);
  });
  registry.handle("indus:pet:hide", (_payload, { senderId }) => {
    controller.requireApp(senderId);
    return controller.hide();
  });
  registry.on("indus:pet:say", ({ text }, { senderId }) => {
    controller.requireApp(senderId);
    controller.say(text);
  });
  registry.handle("indus:pet:get", (_payload, { senderId }) => {
    controller.requirePet(senderId);
    return controller.view();
  });
  registry.on("indus:pet:pointer", ({ over }, { senderId }) => {
    controller.requirePet(senderId);
    controller.pointer(over);
  });
  return controller;
}
