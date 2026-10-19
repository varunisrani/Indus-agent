// Time source for every timer in @indus/client, so tests and other hosts can drive it.

export type TimerHandle = unknown;

export interface Clock {
  now(): number;
  setTimeout(run: () => void, ms: number): TimerHandle;
  clearTimeout(handle: TimerHandle): void;
}

/** The host's timers, looked up at call time so test fakes installed later still apply. */
export const systemClock: Clock = {
  now: () => Date.now(),
  setTimeout: (run, ms) => {
    const handle = globalThis.setTimeout(run, ms) as unknown;
    // In Node a pending client timer must not keep the process alive on its own.
    (handle as { unref?: () => void }).unref?.();
    return handle;
  },
  clearTimeout: (handle) => globalThis.clearTimeout(handle as Parameters<typeof clearTimeout>[0]),
};
