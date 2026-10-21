// The one audited way to open a link in the OS browser (D-16.20).
//
// Rules: http and https only (never `file:`, `javascript:`, our own `indus:` scheme or anything
// else), no user-info in the URL, at most 8 KiB, and only right after a real user input on the
// web contents that asks — observed by main itself through `input-event`, which page script
// cannot fake (a synthetic `click()` produces no input event). One input allows one open within
// `USER_ACTIVATION_WINDOW_MS`. A request from the preview pane names the guest web contents the
// person clicked in (P20-28): that guest's own input is what counts.

import type { IpcRegistry } from "../ipc/registry.ts";

export const USER_ACTIVATION_WINDOW_MS = 2_000;
export const EXTERNAL_URL_MAX_LENGTH = 8 * 1024;

export type ExternalOpenRefusal =
  | "invalid"
  | "scheme"
  | "credentials"
  | "too-long"
  | "no-user-activation"
  | "failed";

export type ExternalOpenResult = { ok: true } | { ok: false; reason: ExternalOpenRefusal };

/** Input event types that count as a person acting (a press, not a hover or a scroll). */
const ACTIVATING_INPUTS = new Set([
  "mouseUp",
  "mouseDown",
  "keyDown",
  "rawKeyDown",
  "char",
  "touchEnd",
  "gestureTap",
]);

/** Records the last real input per web contents; the opener consumes it. */
export class UserActivationTracker {
  private readonly last = new Map<number, number>();
  private readonly now: () => number;

  constructor(now: () => number = Date.now) {
    this.now = now;
  }

  /** Feed from `webContents.on("input-event", (_e, input) => ...)`. */
  noteInput(contentsId: number, inputType: string): void {
    if (ACTIVATING_INPUTS.has(inputType)) this.last.set(contentsId, this.now());
  }

  /** True (and the activation is used up) when `contentsId` saw an input within the window. */
  consume(contentsId: number): boolean {
    const at = this.last.get(contentsId);
    if (at === undefined) return false;
    this.last.delete(contentsId);
    return this.now() - at <= USER_ACTIVATION_WINDOW_MS;
  }

  forget(contentsId: number): void {
    this.last.delete(contentsId);
  }
}

/** Why `url` may not be opened externally, or null when it may. */
export function externalUrlProblem(url: unknown): ExternalOpenRefusal | null {
  if (typeof url !== "string" || url.trim() === "") return "invalid";
  if (url.length > EXTERNAL_URL_MAX_LENGTH) return "too-long";
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return "invalid";
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return "scheme";
  if (parsed.username !== "" || parsed.password !== "") return "credentials";
  if (parsed.hostname === "") return "invalid";
  return null;
}

export interface ExternalOpenerOptions {
  /** Electron `shell.openExternal`. */
  openExternal: (url: string) => Promise<void>;
  activation: UserActivationTracker;
  log: (line: string) => void;
}

export interface ExternalOpenRequest {
  url: string;
  /** The web contents the person acted in (the requesting window, or a preview guest). */
  contentsId: number;
}

export class ExternalOpener {
  private readonly options: ExternalOpenerOptions;

  constructor(options: ExternalOpenerOptions) {
    this.options = options;
  }

  async open(request: ExternalOpenRequest): Promise<ExternalOpenResult> {
    const problem = externalUrlProblem(request.url);
    if (problem !== null) {
      this.options.log(`[external] refused (${problem})`);
      return { ok: false, reason: problem };
    }
    if (!this.options.activation.consume(request.contentsId)) {
      this.options.log(`[external] refused (no user activation on contents ${request.contentsId})`);
      return { ok: false, reason: "no-user-activation" };
    }
    const url = new URL(request.url).href;
    try {
      await this.options.openExternal(url);
      this.options.log(`[external] opened ${new URL(url).origin}`);
      return { ok: true };
    } catch {
      this.options.log("[external] the OS could not open the link");
      return { ok: false, reason: "failed" };
    }
  }
}

export interface ExternalOpenIpcOptions {
  registry: IpcRegistry;
  opener: ExternalOpener;
  /** True when `guestId` is a preview guest hosted by the window `hostId` (P20-28). */
  isGuestOf: (guestId: number, hostId: number) => boolean;
}

/** `indus:external:open`: the person's own window, or a preview guest it hosts. */
export function installExternalOpenIpc(options: ExternalOpenIpcOptions): void {
  options.registry.handle("indus:external:open", async ({ url, guestContentsId }, { senderId }) => {
    let contentsId = senderId;
    if (guestContentsId !== undefined) {
      if (!options.isGuestOf(guestContentsId, senderId))
        return { ok: false, reason: "no-user-activation" } as const;
      contentsId = guestContentsId;
    }
    return options.opener.open({ url, contentsId });
  });
}
