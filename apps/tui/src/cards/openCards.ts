// Open the local cards (confirm, pager) on the overlay store.

import type { StoreApi } from "zustand";
import { type OverlayState, overlayStore } from "../state/overlayStore.ts";
import { pagerLines } from "./Pager.tsx";
import type { ConfirmPayload, PagerPayload } from "./payloads.ts";

let serial = 0;
const nextId = (prefix: string): string => {
  serial += 1;
  return `${prefix}:${serial}`;
};

export type ConfirmOptions = Omit<ConfirmPayload, "confirm" | "cancel">;

/** Ask yes/no; resolves true when confirmed, false when cancelled or replaced. */
export function askConfirm(
  options: ConfirmOptions,
  overlay: StoreApi<OverlayState> = overlayStore,
): Promise<boolean> {
  return new Promise((resolve) => {
    const id = nextId("confirm");
    let done = false;
    const finish = (yes: boolean): void => {
      if (done) return;
      done = true;
      unsubscribe();
      overlay.getState().close(id);
      resolve(yes);
    };
    const payload: ConfirmPayload = {
      ...options,
      confirm: () => finish(true),
      cancel: () => finish(false),
    };
    // Another card taking the overlay counts as "no".
    const unsubscribe = overlay.subscribe((state) => {
      if (state.card?.id !== id) finish(false);
    });
    overlay.getState().open({ id, kind: "confirm", payload, ownsInput: true });
    if (overlay.getState().card?.id !== id) finish(false);
  });
}

/** Show long text in the pager. */
export function openPager(
  text: string,
  title: string | undefined,
  overlay: StoreApi<OverlayState> = overlayStore,
): void {
  const payload: PagerPayload =
    title === undefined ? { lines: pagerLines(text) } : { title, lines: pagerLines(text) };
  overlay.getState().open({ id: nextId("pager"), kind: "pager", payload, ownsInput: true });
}
