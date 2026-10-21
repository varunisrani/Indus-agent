// Who may talk to main (D-16.18). A message is accepted only when it comes from the main frame of
// a web contents the window manager created, showing one of the app's own pages. Everything else
// — an iframe (even one of ours), a `<webview>` guest, a devtools page, a window nobody
// registered, a frame that navigated to another file — is refused.

import { type AppOrigin, isAppUrl } from "../appOrigin.ts";

export type SenderRejection =
  | "no-frame"
  | "destroyed"
  | "guest"
  | "not-main-frame"
  | "foreign-origin"
  | "unknown-window";

export interface FrameLike {
  readonly url: string;
  readonly processId?: number;
  readonly routingId?: number;
  readonly frameTreeNodeId?: number;
}

export interface SenderLike {
  readonly id: number;
  readonly mainFrame?: FrameLike | null;
  getType?(): string;
  isDestroyed?(): boolean;
}

export interface IpcEventLike {
  readonly sender: SenderLike;
  readonly senderFrame?: FrameLike | null;
}

export interface SenderPolicy {
  appOrigin: AppOrigin;
  /** True for web contents of windows the window manager created. */
  isKnownWebContents: (id: number) => boolean;
}

/** Same frame: the same object, or the same ids wherever both sides report them. */
export function sameFrame(a: FrameLike, b: FrameLike): boolean {
  if (a === b) return true;
  const keys = ["frameTreeNodeId", "processId", "routingId"] as const;
  let compared = 0;
  for (const key of keys) {
    const left = a[key];
    const right = b[key];
    if (typeof left !== "number" || typeof right !== "number") continue;
    if (left !== right) return false;
    compared += 1;
  }
  return compared >= 2;
}

/** Null when the sender may use the bridge, else why not. */
export function checkSender(event: IpcEventLike, policy: SenderPolicy): SenderRejection | null {
  const sender = event?.sender;
  const frame = event?.senderFrame;
  if (sender === undefined || sender === null || frame === undefined || frame === null)
    return "no-frame";
  if (sender.isDestroyed?.() === true) return "destroyed";
  if (sender.getType?.() === "webview") return "guest";
  const main = sender.mainFrame;
  if (main === undefined || main === null || !sameFrame(frame, main)) return "not-main-frame";
  if (!isAppUrl(frame.url, policy.appOrigin)) return "foreign-origin";
  if (!policy.isKnownWebContents(sender.id)) return "unknown-window";
  return null;
}
