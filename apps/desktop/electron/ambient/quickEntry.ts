// Quick entry (P20-35): a global chord toggles a small composer that floats over every app. It has
// no backend connection of its own. Its text goes to main, main hands it to the app window the
// person used last, that window submits it into its last-active session through the normal send
// path and answers. Main owns the waiting: the composer keeps its text until the answer, and an
// answer that does not come within 15 s is "not confirmed yet" (the prompt may still land), never
// a failure to retry.

import type { BrowserWindowLike } from "../windows/manager.ts";
import type { Rect } from "../windows/windowState.ts";
import { type FloatingWindow, floatAbove, type ScreenLike, type Timers } from "./hud.ts";

export const QUICK_SIZE = { width: 620, height: 150 } as const;
/** How far down the display the composer sits (fraction of the work area's height). */
export const QUICK_TOP_FRACTION = 0.25;
export const QUICK_RELAY_TIMEOUT_MS = 15_000;
/** Answers we waited for but gave up on, kept so a late one is recognised (and logged). */
const LATE_MEMORY = 32;

export type QuickResult =
  | { ok: true; sessionId: string | null }
  | {
      ok: false;
      code: "empty" | "no-target" | "unconfirmed" | "failed" | "busy";
      message: string;
    };

export interface QuickTarget {
  /** The receiving app window's web contents id. */
  contentsId: number;
  /** Its last-active session (null: a new chat). */
  sessionId: string | null;
  window: BrowserWindowLike;
}

export interface QuickEntryDeps {
  /** `windows.openQuick`. */
  create(bounds: Rect): FloatingWindow;
  screen: ScreenLike;
  /** The app window used last, or null when there is none. */
  target(): QuickTarget | null;
  /** Send `indus:quick:deliver` to the target; false when it could not be sent. */
  deliver(
    target: QuickTarget,
    payload: { requestId: string; text: string; sessionId: string | null },
  ): boolean;
  /** Tell a re-summoned composer to clear and refocus (`indus:quick:shown`). */
  shown(win: FloatingWindow): void;
  /** Bring the receiving window forward after a delivery (a user action). */
  raise(win: BrowserWindowLike): void;
  timers: Timers;
  newId(): string;
  log(line: string): void;
  platform?: string;
}

/** Where the composer opens on a work area: centred, a quarter down, inside the area. */
export function quickBounds(area: Rect): Rect {
  const width = Math.min(QUICK_SIZE.width, area.width);
  const height = Math.min(QUICK_SIZE.height, area.height);
  const y = Math.round(area.y + area.height * QUICK_TOP_FRACTION);
  return {
    width,
    height,
    x: Math.round(area.x + (area.width - width) / 2),
    y: Math.min(Math.max(y, area.y), area.y + area.height - height),
  };
}

interface Pending {
  resolve(result: QuickResult): void;
  timer: unknown;
  contentsId: number;
  window: BrowserWindowLike;
}

export class QuickEntryController {
  private readonly deps: QuickEntryDeps;
  private win: FloatingWindow | null = null;
  private readonly pending = new Map<string, Pending>();
  private readonly late: string[] = [];

  constructor(deps: QuickEntryDeps) {
    this.deps = deps;
  }

  isQuick(contentsId: number): boolean {
    return this.live()?.webContents.id === contentsId;
  }

  isVisible(): boolean {
    return this.live()?.isVisible() === true;
  }

  /** The chord: put it away when it is up, else show it on the pointer's display. */
  toggle(): void {
    if (this.isVisible()) this.hide();
    else this.show();
  }

  show(): void {
    const bounds = this.boundsHere();
    const existing = this.live();
    if (existing === null) {
      const win = this.deps.create(bounds);
      this.win = win;
      floatAbove(win, this.deps.platform ?? process.platform);
      // The manager shows it on its first paint; the person summoned it, so it takes the keys.
      win.on("show", () => {
        if (!win.isDestroyed()) win.focus();
      });
      // Losing focus is the quietest way out (it never holds the keyboard captive).
      win.on("blur", () => {
        if (!win.isDestroyed() && win.isVisible()) win.hide();
      });
      win.on("closed", () => {
        if (this.win === win) this.win = null;
      });
      return;
    }
    try {
      existing.setBounds(bounds);
    } catch {
      this.deps.log("[quick] could not move the composer to this display");
    }
    existing.show();
    existing.focus();
    this.deps.shown(existing);
  }

  hide(): void {
    const win = this.live();
    if (win?.isVisible()) win.hide();
  }

  /** Turning the feature off closes the window. */
  close(): void {
    const win = this.live();
    this.win = null;
    if (win !== null) win.close();
  }

  /** `indus:quick:submit` from the composer. */
  submit(senderId: number, rawText: string): Promise<QuickResult> {
    if (!this.isQuick(senderId)) {
      return Promise.resolve({
        ok: false,
        code: "failed",
        message: "Only the quick entry window can send from here.",
      });
    }
    const text = rawText.trim();
    if (text === "") {
      return Promise.resolve({ ok: false, code: "empty", message: "Type a message first." });
    }
    if (this.pending.size > 0) {
      return Promise.resolve({
        ok: false,
        code: "busy",
        message: "The previous message is still on its way.",
      });
    }
    const target = this.deps.target();
    if (target === null || target.window.isDestroyed()) {
      return Promise.resolve({
        ok: false,
        code: "no-target",
        message: "Open an Indus window first; your text is kept.",
      });
    }
    const requestId = this.deps.newId();
    return new Promise<QuickResult>((resolve) => {
      const timer = this.deps.timers.setTimeout(() => {
        if (!this.pending.delete(requestId)) return;
        this.remember(requestId);
        resolve({
          ok: false,
          code: "unconfirmed",
          message:
            "Indus has not confirmed the message yet. It may still arrive; check the chat before sending it again.",
        });
      }, QUICK_RELAY_TIMEOUT_MS);
      this.pending.set(requestId, {
        resolve,
        timer,
        contentsId: target.contentsId,
        window: target.window,
      });
      const sent = this.deps.deliver(target, { requestId, text, sessionId: target.sessionId });
      if (!sent) {
        this.pending.delete(requestId);
        this.deps.timers.clearTimeout(timer);
        resolve({
          ok: false,
          code: "no-target",
          message: "Open an Indus window first; your text is kept.",
        });
      }
    });
  }

  /** `indus:quick:ack` from the receiving window. */
  ack(
    senderId: number,
    answer: {
      requestId: string;
      ok: boolean;
      sessionId?: string | null | undefined;
      error?: string | undefined;
    },
  ): void {
    const pending = this.pending.get(answer.requestId);
    if (pending === undefined) {
      if (this.late.includes(answer.requestId)) {
        this.deps.log(
          `[quick] a late answer arrived (${answer.ok ? "delivered" : "not delivered"})`,
        );
      }
      return;
    }
    if (pending.contentsId !== senderId) return; // only the window we asked may answer
    this.pending.delete(answer.requestId);
    this.deps.timers.clearTimeout(pending.timer);
    if (answer.ok) {
      pending.resolve({ ok: true, sessionId: answer.sessionId ?? null });
      this.hide();
      if (!pending.window.isDestroyed()) this.deps.raise(pending.window);
    } else {
      pending.resolve({
        ok: false,
        code: "failed",
        message: (answer.error ?? "").trim() || "Indus could not send that message.",
      });
    }
  }

  private remember(requestId: string): void {
    this.late.push(requestId);
    if (this.late.length > LATE_MEMORY) this.late.shift();
  }

  private boundsHere(): Rect {
    const pointer = this.deps.screen.getCursorScreenPoint();
    return quickBounds(this.deps.screen.getDisplayNearestPoint(pointer).workArea);
  }

  private live(): FloatingWindow | null {
    return this.win !== null && !this.win.isDestroyed() ? this.win : null;
  }
}
