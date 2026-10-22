// Renderer error forwarding (P20-39): uncaught errors and unhandled promise rejections in this
// window go to main's desktop.log through `logs.rendererError` (main redacts and rate-limits too).
// The same error repeated within 5 s is sent once; at most 30 reports a minute leave the window.
//
// W48-c: the browser's "ResizeObserver loop …" notice is not an error (size changes that did not
// fit in one frame are delivered in the next) and is not forwarded. Locations name the file only:
// a `file:` URL's folders are where this copy of the app runs, which says nothing about the error,
// and an event without a position (line 0) gets no location line at all.

import type { IndusDesktopBridge } from "../global.d.ts";

export const DUPLICATE_WINDOW_MS = 5_000;
export const REPORTS_PER_MINUTE = 30;

type Report = Parameters<IndusDesktopBridge["logs"]["rendererError"]>[0];

export interface ForwardingTarget {
  addEventListener(type: string, listener: (event: Event) => void): void;
  removeEventListener(type: string, listener: (event: Event) => void): void;
}

/** Chromium's notice for size changes left over to the next frame (an error event, no error). */
const RESIZE_OBSERVER_NOTICE =
  /^ResizeObserver loop (completed with undelivered notifications|limit exceeded)/;

/** `file:` URLs shortened to their file name (`…/dist/assets/index-Ab12.js:3:9` → `index-Ab12.js:3:9`). */
export function shortenFileUrls(text: string): string {
  return text.replace(/file:\/\/[^\s()]*\/([^\s()/]+)/g, "$1");
}

function describe(reason: unknown): { message: string; stack?: string } {
  if (reason instanceof Error) {
    return {
      message: `${reason.name}: ${reason.message}`,
      ...(reason.stack ? { stack: reason.stack } : {}),
    };
  }
  if (typeof reason === "string") return { message: reason };
  try {
    return { message: JSON.stringify(reason) ?? String(reason) };
  } catch {
    return { message: String(reason) };
  }
}

/** Start forwarding; returns a function that stops it. */
export function installErrorForwarding(
  target: ForwardingTarget,
  send: (report: Report) => void,
  options: { source: string; now?: () => number },
): () => void {
  const now = options.now ?? Date.now;
  const lastSent = new Map<string, number>();
  let minuteStart = now();
  let sentThisMinute = 0;

  const forward = (kind: string, reason: unknown, where?: string) => {
    const at = now();
    if (at - minuteStart >= 60_000) {
      minuteStart = at;
      sentThisMinute = 0;
    }
    const { message, stack } = describe(reason);
    const text = `${kind}: ${message}`.slice(0, 8_000);
    const previous = lastSent.get(text);
    if (previous !== undefined && at - previous < DUPLICATE_WINDOW_MS) return;
    if (sentThisMinute >= REPORTS_PER_MINUTE) return;
    lastSent.set(text, at);
    if (lastSent.size > 100) lastSent.delete(lastSent.keys().next().value as string);
    sentThisMinute += 1;
    const fullStack = shortenFileUrls(
      [stack, where].filter((part) => part !== undefined && part !== "").join("\n"),
    );
    try {
      send({
        message: text,
        ...(fullStack === "" ? {} : { stack: fullStack.slice(0, 60_000) }),
        source: options.source,
      });
    } catch {
      // the log is best effort
    }
  };

  const onError = (event: Event) => {
    const error = event as ErrorEvent;
    if (
      (error.error === null || error.error === undefined) &&
      RESIZE_OBSERVER_NOTICE.test(error.message ?? "")
    ) {
      return;
    }
    const where =
      typeof error.filename === "string" && error.filename !== "" && (error.lineno ?? 0) > 0
        ? `at ${error.filename}:${error.lineno}:${error.colno ?? 0}`
        : undefined;
    forward("error", error.error ?? error.message ?? "unknown error", where);
  };
  const onRejection = (event: Event) => {
    forward("unhandled rejection", (event as PromiseRejectionEvent).reason);
  };
  target.addEventListener("error", onError);
  target.addEventListener("unhandledrejection", onRejection);
  return () => {
    target.removeEventListener("error", onError);
    target.removeEventListener("unhandledrejection", onRejection);
  };
}
