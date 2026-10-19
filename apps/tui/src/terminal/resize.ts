// Resize storms (R05.16): reflow at once on the first event, then at most once per 32 ms, and
// always once more after the last event so the final size is exact.

import { type Clock, systemClock } from "@indus/client";

export const RESIZE_INTERVAL_MS = 32;

export interface ResizeCoalescer {
  /** Call on every resize event. */
  schedule(): void;
  /** Drop a pending trailing reflow (listener removed, unmount). */
  cancel(): void;
  /** Reflows run so far. */
  readonly runs: number;
}

export function createResizeCoalescer(
  reflow: () => void,
  intervalMs: number = RESIZE_INTERVAL_MS,
  clock: Clock = systemClock,
): ResizeCoalescer {
  let timer: ReturnType<Clock["setTimeout"]> | null = null;
  let owed = false;
  let runs = 0;
  const run = (): void => {
    runs += 1;
    reflow();
  };
  // A window opens at each reflow; an event inside it is owed one reflow when the window closes.
  const openWindow = (): void => {
    timer = clock.setTimeout(() => {
      timer = null;
      if (!owed) return;
      owed = false;
      run();
      openWindow();
    }, intervalMs);
  };
  return {
    schedule() {
      if (timer === null) {
        run();
        openWindow();
      } else {
        owed = true;
      }
    },
    cancel() {
      if (timer !== null) clock.clearTimeout(timer);
      timer = null;
      owed = false;
    },
    get runs() {
      return runs;
    },
  };
}
