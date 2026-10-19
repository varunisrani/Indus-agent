import { describe, expect, it } from "vitest";
import { FakeClock } from "../composer/testSupport.ts";
import {
  GAVE_UP_STATUS,
  gaveUpLine,
  planRestart,
  RESTART_LINE,
  RESTART_WINDOW_MS,
  Recovery,
} from "./recovery.ts";

function setup() {
  const clock = new FakeClock();
  const said: string[] = [];
  const statuses: string[] = [];
  let restarts = 0;
  const recovery = new Recovery({
    clock,
    restart: () => {
      restarts += 1;
    },
    say: (text) => said.push(text),
    status: (text) => statuses.push(text),
  });
  return { clock, said, statuses, recovery, restarts: () => restarts };
}

describe("restart budget", () => {
  it("grants three restarts inside one sliding window", () => {
    let attempts: number[] = [];
    const granted: boolean[] = [];
    for (const at of [0, 10, 20, 30]) {
      const plan = planRestart(attempts, at);
      attempts = plan.attempts;
      granted.push(plan.granted);
    }
    expect(granted).toEqual([true, true, true, false]);
    expect(attempts).toEqual([0, 10, 20]);
  });

  it("forgets restarts older than the window", () => {
    const plan = planRestart([0, 10, 20], RESTART_WINDOW_MS + 15);
    expect(plan.granted).toBe(true);
    expect(plan.attempts).toEqual([20, RESTART_WINDOW_MS + 15]);
  });

  it("names the exit code or signal and the last stderr line", () => {
    expect(gaveUpLine({ code: 3, signal: null, stderrLast: "boom" })).toMatch(
      /exit code 3[\s\S]*last message: boom/,
    );
    expect(gaveUpLine({ code: null, signal: "SIGKILL", stderrLast: null })).toContain("SIGKILL");
    expect(gaveUpLine(null)).toContain("closed the connection");
  });
});

describe("Recovery", () => {
  it("restarts at once inside the budget and keeps the session to reopen", () => {
    const rig = setup();
    expect(rig.recovery.lost("s-7", null)).toBe("restarted");
    expect(rig.restarts()).toBe(1);
    expect(rig.said).toEqual([RESTART_LINE]);
    expect(rig.recovery.target).toBe("s-7");
  });

  it("keeps the target across a death before ready, and drops it once resumed", () => {
    const rig = setup();
    rig.recovery.lost("s-7", null);
    // the new child died before a session was open again: nothing live, target kept
    rig.recovery.lost(null, null);
    expect(rig.recovery.ready()).toBe("s-7");
    rig.recovery.settled("other");
    expect(rig.recovery.target).toBe("s-7");
    rig.recovery.settled("s-7");
    expect(rig.recovery.target).toBeNull();
  });

  it("4th loss in 60 s says the error once, retries on a backoff, then reopens the same id", () => {
    const rig = setup();
    const exit = { code: 1, signal: null, stderrLast: "Traceback: it broke" };
    for (let i = 0; i < 3; i += 1) {
      rig.clock.advance(1_000);
      expect(rig.recovery.lost(i === 0 ? "s-1" : null, exit)).toBe("restarted");
    }
    rig.clock.advance(1_000);
    expect(rig.recovery.lost(null, exit)).toBe("waiting");
    const errors = rig.said.filter((line) => line.startsWith("error:"));
    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain("exit code 1");
    expect(errors[0]).toContain("Traceback: it broke");
    expect(rig.statuses.at(-1)).toBe(GAVE_UP_STATUS);
    expect(rig.restarts()).toBe(3);
    // background retry after the first backoff step (1 s)
    rig.clock.advance(999);
    expect(rig.restarts()).toBe(3);
    rig.clock.advance(1);
    expect(rig.restarts()).toBe(4);
    // that child dies too: no second error, the next wait doubles
    expect(rig.recovery.lost(null, exit)).toBe("waiting");
    expect(rig.said.filter((line) => line.startsWith("error:"))).toHaveLength(1);
    rig.clock.advance(1_999);
    expect(rig.restarts()).toBe(4);
    rig.clock.advance(1);
    expect(rig.restarts()).toBe(5);
    // the 5th child stays up: the same chat is reopened
    expect(rig.recovery.ready()).toBe("s-1");
    expect(rig.recovery.outage).toBe(false);
  });

  it("a second loss while a retry is scheduled does not stack timers", () => {
    const rig = setup();
    for (let i = 0; i < 4; i += 1) rig.recovery.lost("s-1", null);
    expect(rig.recovery.waiting).toBe(true);
    expect(rig.recovery.lost("s-1", null)).toBe("waiting");
    expect(rig.clock.pending).toBe(1);
  });

  it("stops restarting once stopped", () => {
    const rig = setup();
    for (let i = 0; i < 4; i += 1) rig.recovery.lost("s-1", null);
    rig.recovery.stop();
    rig.clock.advance(60_000);
    expect(rig.restarts()).toBe(3);
    expect(rig.recovery.lost("s-1", null)).toBe("stopped");
  });
});
