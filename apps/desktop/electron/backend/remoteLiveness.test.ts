import { describe, expect, it } from "vitest";
import { FakeTimers } from "../../testing/fakeTimers.ts";
import {
  LivenessTracker,
  REMOTE_FAILURE_WINDOW_MS,
  REMOTE_PROBE_TIMEOUT_MS,
  RESUME_REVALIDATION_HOLD_MS,
  ResumeGate,
} from "./remoteLiveness.ts";

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("remote liveness", () => {
  it("constants", () => {
    expect(REMOTE_PROBE_TIMEOUT_MS).toBe(10_000);
    expect(REMOTE_FAILURE_WINDOW_MS).toBe(60_000);
    expect(RESUME_REVALIDATION_HOLD_MS).toBe(15_000);
  });

  it("the 3rd failure within 60 s marks the route dead (fake clock)", () => {
    const timers = new FakeTimers();
    const tracker = new LivenessTracker(timers.clock);
    expect(tracker.recordFailure("r")).toEqual({ failures: 1, dead: false });
    timers.advance(20_000);
    expect(tracker.recordFailure("r")).toEqual({ failures: 2, dead: false });
    timers.advance(20_000);
    expect(tracker.recordFailure("r")).toEqual({ failures: 3, dead: true });
    // counting starts over after a dead verdict
    expect(tracker.recordFailure("r")).toEqual({ failures: 1, dead: false });
  });

  it("failures spread wider than 60 s, or a success between them, do not kill the route", () => {
    const timers = new FakeTimers();
    const tracker = new LivenessTracker(timers.clock);
    tracker.recordFailure("r");
    timers.advance(REMOTE_FAILURE_WINDOW_MS + 1);
    expect(tracker.recordFailure("r").failures).toBe(1);
    tracker.recordFailure("r");
    tracker.recordSuccess("r");
    expect(tracker.recordFailure("r")).toEqual({ failures: 1, dead: false });
    // routes are counted apart
    expect(tracker.recordFailure("other").failures).toBe(1);
  });

  it("a resume holds revalidation 15 s and coalesces each window's requests", async () => {
    const timers = new FakeTimers();
    const gate = new ResumeGate(timers, timers.clock);
    const runs: string[] = [];
    gate.noteResume();
    const a1 = gate.request("win-1", async () => {
      runs.push("win-1 first");
    });
    const a2 = gate.request("win-1", async () => {
      runs.push("win-1 second");
    });
    const b = gate.request("win-2", async () => {
      runs.push("win-2");
    });
    timers.advance(RESUME_REVALIDATION_HOLD_MS - 1);
    await tick();
    expect(runs).toEqual([]);
    timers.advance(1);
    await Promise.all([a1, a2, b]);
    // one run per window, the latest request's work
    expect(runs.sort()).toEqual(["win-1 second", "win-2"]);
  });

  it("without a hold, a request joins the run already in flight for its window", async () => {
    const timers = new FakeTimers();
    const gate = new ResumeGate(timers, timers.clock);
    let calls = 0;
    let release: () => void = () => {};
    const run = () =>
      new Promise<void>((resolve) => {
        calls += 1;
        release = resolve;
      });
    const first = gate.request("w", run);
    const second = gate.request("w", run);
    await tick();
    expect(calls).toBe(1);
    release();
    await Promise.all([first, second]);
    await gate.request("w", async () => {
      calls += 1;
    });
    expect(calls).toBe(2);
  });
});
