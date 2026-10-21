// The wake-word indicator (P24-20): a small always-on-top light at the top centre of the primary
// display, created by the window manager as kind `wake`. It is up whenever the microphone is in
// use for the wake word — `listening`, `heard` (the phrase was just detected) and `capturing`
// (voice mode records) — and hidden otherwise, so listening is never invisible to the person.
//
// App windows set the state (`wake:indicator`); the indicator window reads it once
// (`wake:indicator-get`) and then follows `wake:indicator-state`. The window is made on the first
// visible state, never takes focus, and is hidden (not closed) on `hidden`.

import type { MainContext } from "../context.ts";
import { IpcError } from "../ipc/envelope.ts";
import type { PayloadOf } from "../ipc/schemas.ts";
import type { BrowserWindowLike } from "./manager.ts";
import { closeWithLastAppWindow } from "./petWindow.ts";
import type { DisplayLike, Rect } from "./windowState.ts";

export const WAKE_INDICATOR_SIZE = { width: 184, height: 44 } as const;
/** Gap between the top of the work area and the light. */
export const WAKE_INDICATOR_TOP = 8;

export type WakeIndicatorState = PayloadOf<"indus:wake:indicator-state">["state"];

export interface IndicatorWindowLike extends BrowserWindowLike {
  setBounds(bounds: Rect): void;
  hide(): void;
  showInactive?(): void;
}

export interface IndicatorDeps {
  create(bounds: Rect): IndicatorWindowLike;
  screen: { getPrimaryDisplay(): DisplayLike };
  kindOf(contentsId: number): string | null;
  emit(win: IndicatorWindowLike, state: WakeIndicatorState): void;
}

const APP_KINDS: ReadonlySet<string> = new Set(["main", "secondary", "instance", "hud"]);

/** Where the light sits on a display: centred, just under the top of the work area. */
export function indicatorBounds(display: DisplayLike): Rect {
  const area = display.workArea;
  const width = Math.min(WAKE_INDICATOR_SIZE.width, area.width);
  const height = Math.min(WAKE_INDICATOR_SIZE.height, area.height);
  return {
    width,
    height,
    x: Math.round(area.x + (area.width - width) / 2),
    y: area.y + Math.min(WAKE_INDICATOR_TOP, Math.max(0, area.height - height)),
  };
}

export class WakeIndicatorController {
  private readonly deps: IndicatorDeps;
  private win: IndicatorWindowLike | null = null;
  private current: WakeIndicatorState = "hidden";

  constructor(deps: IndicatorDeps) {
    this.deps = deps;
  }

  state(): WakeIndicatorState {
    return this.current;
  }

  requireApp(senderId: number): void {
    const kind = this.deps.kindOf(senderId);
    if (kind === null || !APP_KINDS.has(kind)) {
      throw new IpcError("sender-rejected", "only an app window may set the wake indicator");
    }
  }

  requireIndicator(senderId: number): void {
    const win = this.win;
    if (this.deps.kindOf(senderId) !== "wake" || win === null || win.webContents.id !== senderId) {
      throw new IpcError("sender-rejected", "only the wake indicator reads its state");
    }
  }

  set(state: WakeIndicatorState): void {
    if (state === this.current) return;
    this.current = state;
    let win = this.win !== null && !this.win.isDestroyed() ? this.win : null;
    if (state === "hidden") {
      if (win !== null) {
        this.deps.emit(win, state);
        win.hide();
      }
      return;
    }
    if (win === null) {
      win = this.deps.create(indicatorBounds(this.deps.screen.getPrimaryDisplay()));
      this.win = win;
      const made = win;
      made.on("closed", () => {
        if (this.win === made) this.win = null;
      });
    } else {
      win.setBounds(indicatorBounds(this.deps.screen.getPrimaryDisplay()));
      if (win.showInactive !== undefined) win.showInactive();
      else win.show();
    }
    this.deps.emit(win, state);
  }

  close(): void {
    const win = this.win;
    this.win = null;
    this.current = "hidden";
    if (win !== null && !win.isDestroyed()) win.close();
  }
}

/** Main's wake-indicator install step. */
export function installWakeIndicator(
  context: MainContext,
  electron: { screen: { getPrimaryDisplay(): DisplayLike } },
): WakeIndicatorController {
  const { registry, windows } = context;
  const controller = new WakeIndicatorController({
    create: (bounds) => windows.openWakeIndicator(bounds) as IndicatorWindowLike,
    screen: electron.screen,
    kindOf: (id) => windows.kindOf(id),
    emit: (win, state) => {
      registry.emit(win.webContents, "indus:wake:indicator-state", { state });
    },
  });
  closeWithLastAppWindow(windows, () => controller.close());
  registry.on("indus:wake:indicator", ({ state }, { senderId }) => {
    controller.requireApp(senderId);
    controller.set(state);
  });
  registry.handle("indus:wake:indicator-get", (_payload, { senderId }) => {
    controller.requireIndicator(senderId);
    return { state: controller.state() };
  });
  return controller;
}
