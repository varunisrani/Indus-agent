// Main's side of the preview pane (P20-28): which `<webview>` guests belong to which window, the
// key hook that always gets a person out of a fullscreened guest, page capture, and the preview
// file-watch channels.
//
// Guests are the preview pane's webviews: web contents of type `webview` on the preview partition,
// hosted by one of our windows. A guest never talks to main (the sender check refuses guests); its
// preload hands a trusted link click to the host page, and the host asks `external.open` naming the
// guest — main then checks here that the guest is really hosted by the asking window, and the
// opener checks that the guest itself saw a real input (D-16.20, R08.8).

import type { PathGuard } from "../fs/pathGuard.ts";
import { IpcError } from "../ipc/envelope.ts";
import type { EventTarget, IpcRegistry } from "../ipc/registry.ts";
import { PREVIEW_PARTITION } from "../security/navigation.ts";
import { type WatchHub, WatchLimitError, type WatchOwner } from "./fileWatch.ts";

export type GuestKeyAction = "exit-fullscreen" | "close-preview";

/** The parts of Electron's `Input` the hook reads. */
export interface GuestInput {
  type: string;
  key: string;
  control?: boolean;
  meta?: boolean;
  shift?: boolean;
  alt?: boolean;
  isAutoRepeat?: boolean;
}

/**
 * What a key pressed inside a guest does before the page sees it: Escape leaves fullscreen (only
 * while the host window is fullscreen, so pages keep their own Escape otherwise); Ctrl/Cmd+Shift+W
 * closes the preview. Anything else reaches the page.
 */
export function guestKeyAction(input: GuestInput, hostFullscreen: boolean): GuestKeyAction | null {
  if (input.type !== "keyDown" || input.isAutoRepeat === true) return null;
  if (input.key === "Escape") return hostFullscreen ? "exit-fullscreen" : null;
  const modifier = input.control === true || input.meta === true;
  if (modifier && input.shift === true && input.alt !== true && input.key.toLowerCase() === "w") {
    return "close-preview";
  }
  return null;
}

/** A guest web contents as main sees it. */
export interface GuestContents {
  readonly id: number;
  getType(): string;
  isDestroyed(): boolean;
  /** The window page hosting this webview. */
  readonly hostWebContents?: { readonly id: number } | null;
  /** The guest's session partition is checked through this (Electron: `session.storagePath` etc.). */
  on(event: string, listener: (...args: never[]) => void): unknown;
  capturePage(): Promise<CapturedImage>;
}

export interface CapturedImage {
  isEmpty(): boolean;
  toDataURL(): string;
}

export interface HostWindow {
  isFullScreen(): boolean;
  setFullScreen(flag: boolean): void;
  isDestroyed(): boolean;
}

export interface PreviewGuestsOptions {
  /** The window hosting `hostContentsId`, or null when it is not one of ours. */
  hostWindow: (hostContentsId: number) => HostWindow | null;
  /** The host page to tell about a close request. */
  hostContents: (hostContentsId: number) => EventTarget | null;
  registry: Pick<IpcRegistry, "emit">;
  log: (line: string) => void;
}

/** Tracks the preview guests and applies the key hook to each. */
export class PreviewGuests {
  private readonly options: PreviewGuestsOptions;
  private readonly guests = new Map<number, { contents: GuestContents; hostId: number }>();

  constructor(options: PreviewGuestsOptions) {
    this.options = options;
  }

  /**
   * Feed every new web contents (`app.on("web-contents-created")`) with the partition it was
   * attached on (from `will-attach-webview`). Only preview guests are tracked.
   */
  adopt(contents: GuestContents, partition: string): boolean {
    if (contents.getType() !== "webview" || partition !== PREVIEW_PARTITION) return false;
    const hostId = contents.hostWebContents?.id;
    if (hostId === undefined) return false;
    this.guests.set(contents.id, { contents, hostId });
    contents.on("destroyed", () => this.guests.delete(contents.id));
    contents.on("before-input-event", ((event: { preventDefault(): void }, input: GuestInput) => {
      this.onKey(contents.id, event, input);
    }) as (...args: never[]) => void);
    return true;
  }

  /** True when `guestId` is a live preview guest hosted by the window page `hostId`. */
  isGuestOf(guestId: number, hostId: number): boolean {
    const entry = this.guests.get(guestId);
    if (entry === undefined || entry.contents.isDestroyed()) return false;
    return entry.hostId === hostId;
  }

  count(): number {
    return this.guests.size;
  }

  private onKey(guestId: number, event: { preventDefault(): void }, input: GuestInput): void {
    const entry = this.guests.get(guestId);
    if (entry === undefined) return;
    const host = this.options.hostWindow(entry.hostId);
    const action = guestKeyAction(input, host?.isFullScreen() === true);
    if (action === null) return;
    event.preventDefault();
    if (action === "exit-fullscreen") {
      if (host !== null && !host.isDestroyed()) host.setFullScreen(false);
      this.options.log("[preview] left fullscreen from the guest key hook");
      return;
    }
    const target = this.options.hostContents(entry.hostId);
    if (target !== null) this.options.registry.emit(target, "indus:preview:close-requested", {});
  }

  /** A PNG data URL of the guest's visible page; refused for another window's guest. */
  async capture(guestId: number, hostId: number): Promise<string> {
    const entry = this.guests.get(guestId);
    if (entry === undefined || entry.contents.isDestroyed() || entry.hostId !== hostId) {
      throw new IpcError("not-found", "that preview is not open in this window");
    }
    const image = await entry.contents.capturePage();
    if (image.isEmpty()) throw new IpcError("capture-failed", "the preview produced no image");
    return image.toDataURL();
  }
}

export interface PreviewIpcDeps {
  registry: IpcRegistry;
  guests: PreviewGuests;
  guard: PathGuard;
  watches: WatchHub;
  contents: (id: number) => (WatchOwner & EventTarget) | null;
}

/** `indus:preview:capture|watch-file|unwatch-file`. */
export function installPreviewIpc(deps: PreviewIpcDeps): void {
  const { registry, guests, guard, watches } = deps;

  registry.handle("indus:preview:capture", async ({ guestContentsId }, { senderId }) => ({
    dataUrl: await guests.capture(guestContentsId, senderId),
  }));

  registry.handle("indus:preview:watch-file", async ({ path: raw }, { senderId }) => {
    const resolved = await guard.resolve(senderId, raw, { refuseSensitive: true });
    const owner = deps.contents(senderId);
    if (owner === null) throw new IpcError("not-found", "the window is gone");
    let watchId = "";
    try {
      watchId = watches.watchFile(owner, resolved.path, () => {
        registry.emit(owner, "indus:preview:file-changed", { watchId, path: resolved.path });
      });
    } catch (error) {
      if (error instanceof WatchLimitError) throw new IpcError("too-many-watches", error.message);
      throw new IpcError("watch-failed", "the file could not be watched");
    }
    return { watchId };
  });

  registry.handle("indus:preview:unwatch-file", ({ watchId }, { senderId }) => {
    watches.unwatch(senderId, watchId);
    return { ok: true as const };
  });
}
