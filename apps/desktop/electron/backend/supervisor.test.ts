import { describe, expect, it } from "vitest";
import { FakeTimers } from "../../testing/fakeTimers.ts";
import {
  type BackendNotice,
  DaemonSupervisor,
  READY_POLL_MS,
  READY_TIMEOUT_MS,
  RESTART_WINDOW_MS,
  WATCH_INTERVAL_MS,
  withTimeout,
} from "./supervisor.ts";

const flush = async () => {
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
};

function rig(options: { readyAfterRespawn?: boolean } = {}) {
  const timers = new FakeTimers();
  const notices: BackendNotice[] = [];
  const state = { alive: true, respawns: 0 };
  const supervisor = new DaemonSupervisor({
    alive: async () => state.alive,
    respawn: async () => {
      state.respawns += 1;
      if (options.readyAfterRespawn !== false) state.alive = true;
    },
    describeExit: async () => ({ exitCode: 70, recentOutput: ["boom", "traceback"] }),
    notify: (notice) => notices.push(notice),
    timers,
    now: timers.clock,
    log: () => {},
  });
  const advance = async (ms: number) => {
    timers.advance(ms);
    await flush();
  };
  const crash = async () => {
    state.alive = false;
    await advance(WATCH_INTERVAL_MS);
  };
  return { timers, notices, state, supervisor, advance, crash };
}

describe("daemon supervisor", () => {
  it("a crash after ready: respawn and a 'stopped working in the background' notice", async () => {
    const r = rig();
    r.supervisor.watch(true);
    await r.advance(WATCH_INTERVAL_MS * 3);
    expect(r.state.respawns).toBe(0);
    await r.crash();
    expect(r.state.respawns).toBe(1);
    expect(r.notices).toEqual([
      { kind: "restarted", message: "Indus stopped working in the background and was restarted." },
    ]);
    expect(r.supervisor.active()).toBe(true);
  });

  it("4th crash in 120 s → no respawn; 'keeps crashing' with the exit code and output (fake clock)", async () => {
    const r = rig();
    r.supervisor.watch(true);
    for (let i = 0; i < 3; i += 1) await r.crash();
    expect(r.state.respawns).toBe(3);
    await r.crash();
    expect(r.state.respawns).toBe(3);
    expect(r.notices.at(-1)).toEqual({
      kind: "crashing",
      message: "Indus keeps crashing in the background, so it was not restarted again.",
      exitCode: 70,
      recentOutput: ["boom", "traceback"],
    });
    expect(r.supervisor.active()).toBe(false);
    expect(r.supervisor.stoppedRespawning()).toBe(true);
    await r.advance(WATCH_INTERVAL_MS * 10);
    expect(r.state.respawns).toBe(3);
  });

  it("crashes spread wider than two minutes keep being restarted", async () => {
    const r = rig();
    r.supervisor.watch(true);
    for (let i = 0; i < 6; i += 1) {
      await r.crash();
      await r.advance(RESTART_WINDOW_MS / 2);
    }
    expect(r.state.respawns).toBe(6);
    expect(r.notices.every((n) => n.kind === "restarted")).toBe(true);
  });

  it("a daemon this app only attached to is never restarted", async () => {
    const r = rig();
    r.supervisor.watch(false);
    await r.crash();
    await r.advance(WATCH_INTERVAL_MS * 5);
    expect(r.state.respawns).toBe(0);
    expect(r.notices).toEqual([]);
  });

  it("readiness wait: polls every 500 ms and gives up at 180 s", async () => {
    const r = rig({ readyAfterRespawn: false });
    r.supervisor.watch(true);
    await r.crash();
    expect(r.state.respawns).toBe(1);
    // still waiting just before the deadline
    for (let t = 0; t < READY_TIMEOUT_MS - READY_POLL_MS * 2; t += READY_POLL_MS)
      await r.advance(READY_POLL_MS);
    expect(r.notices).toEqual([]);
    for (let t = 0; t < READY_POLL_MS * 4; t += READY_POLL_MS) await r.advance(READY_POLL_MS);
    expect(r.notices.at(-1)?.kind).toBe("crashing");
  });

  it("a ready answer during the wait ends it", async () => {
    const r = rig({ readyAfterRespawn: false });
    r.supervisor.watch(true);
    await r.crash();
    await r.advance(READY_POLL_MS * 4);
    r.state.alive = true;
    await r.advance(READY_POLL_MS);
    expect(r.notices.at(-1)?.kind).toBe("restarted");
  });

  it("withTimeout bounds a probe", async () => {
    const timers = new FakeTimers();
    const never = new Promise<boolean>(() => {});
    const bounded = withTimeout(never, 5_000, false, timers);
    timers.advance(5_000);
    expect(await bounded).toBe(false);
    expect(await withTimeout(Promise.resolve(true), 5_000, false, timers)).toBe(true);
  });
});
