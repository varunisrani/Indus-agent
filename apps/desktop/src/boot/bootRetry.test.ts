import { describe, expect, it } from "vitest";
import { FakeTimers } from "../../testing/fakeTimers.ts";
import { seeded } from "../../testing/seeded.ts";
import {
  BOOT_RETRY_BASE_MS,
  BOOT_RETRY_CAP_MS,
  BOOT_RETRY_MAX_ATTEMPTS,
  BootRetry,
  bootRetryDelay,
} from "./bootRetry.ts";

function clockOf(timers: FakeTimers) {
  return {
    now: () => timers.now,
    setTimeout: (run: () => void, ms: number) => timers.setTimeout(run, ms),
    clearTimeout: (handle: unknown) => timers.clearTimeout(handle),
  };
}

describe("boot retry", () => {
  it("keeps every delay inside the full-jitter bounds (seeded)", () => {
    for (let seed = 1; seed <= 50; seed += 1) {
      const random = seeded(seed);
      for (let attempt = 1; attempt <= BOOT_RETRY_MAX_ATTEMPTS; attempt += 1) {
        const step = Math.min(BOOT_RETRY_CAP_MS, BOOT_RETRY_BASE_MS * 2 ** (attempt - 1));
        const delay = bootRetryDelay(attempt, random);
        expect(delay).toBeGreaterThanOrEqual(0);
        expect(delay).toBeLessThan(step);
      }
    }
  });

  it("makes at most five attempts, each after its delay; the sixth is never made", () => {
    const timers = new FakeTimers();
    const retry = new BootRetry({ clock: clockOf(timers), random: seeded(7) });
    let runs = 0;
    for (let i = 0; i < BOOT_RETRY_MAX_ATTEMPTS; i += 1) {
      const delay = retry.schedule(() => {
        runs += 1;
      });
      expect(delay).not.toBeNull();
      expect(retry.schedule(() => undefined)).toBeNull();
      timers.advance(Math.max(0, (delay ?? 0) - 1));
      expect(runs).toBe(i);
      timers.advance(1);
      expect(runs).toBe(i + 1);
    }
    expect(retry.exhausted).toBe(true);
    expect(retry.schedule(() => undefined)).toBeNull();
    timers.advance(10 * BOOT_RETRY_CAP_MS);
    expect(runs).toBe(BOOT_RETRY_MAX_ATTEMPTS);
    retry.reset();
    expect(retry.exhausted).toBe(false);
  });
});
