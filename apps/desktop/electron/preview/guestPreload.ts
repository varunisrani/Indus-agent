// The preview guest's preload logic (P20-28). The bundled preload (`guestPreloadEntry.ts`, built to
// `dist-electron/preview-guest.cjs`) runs in every preview `<webview>` guest, isolated from the
// page. It exposes nothing to the page. Its one job: when the person really clicks (a trusted,
// primary-button click) a link that asks for a new window (`target=_blank`), hand the link's
// absolute URL to the host page with `sendToHost`. The host asks main to open it; main opens only
// http/https and only after a real input in that guest (D-16.20, R08.8). Page script can dispatch a
// click on an anchor, but never a trusted one, so a page cannot drive the OS browser.

/** The `sendToHost` channel the preview pane listens on (`ipc-message` of the webview). */
export const GUEST_LINK_CHANNEL = "indus-preview:open-link";

/** What the listener reads from a click (a DOM `MouseEvent` in the guest). */
export interface GuestClick {
  readonly isTrusted?: boolean;
  readonly button?: number;
  readonly target?: unknown;
}

interface AnchorLike {
  readonly href?: unknown;
}

interface ClosestCapable {
  closest(selector: string): AnchorLike | null;
}

export interface GuestDocument {
  addEventListener(type: "click", listener: (event: GuestClick) => void, capture: boolean): void;
}

/** The absolute URL a trusted primary click on a `_blank` link asks for, else null. */
export function linkToHandOff(event: GuestClick): string | null {
  if (event.isTrusted !== true) return null;
  if ((event.button ?? 0) !== 0) return null;
  const target = event.target as Partial<ClosestCapable> | null | undefined;
  if (target === null || target === undefined || typeof target.closest !== "function") return null;
  const anchor = target.closest('a[target="_blank" i]');
  // `href` on an anchor element is already resolved against the page (relative links included).
  if (anchor === null || typeof anchor.href !== "string" || anchor.href === "") return null;
  return anchor.href;
}

/** Listen (capture phase, before the page) and hand qualifying links to the host. */
export function installLinkHandOff(
  doc: GuestDocument,
  sendToHost: (channel: string, url: string) => void,
): void {
  doc.addEventListener(
    "click",
    (event) => {
      const url = linkToHandOff(event);
      if (url !== null) sendToHost(GUEST_LINK_CHANNEL, url);
    },
    true,
  );
}
