import { type ChildProcess, spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { FakeTimers } from "../../testing/fakeTimers.ts";
import {
  EMPTY_MARKER_GRACE_MS,
  GATE_POLL_MS,
  IN_FLIGHT_WAIT_MAX_MS,
  inspectMarker,
  judgeMarker,
  linuxStartSeconds,
  markerPath,
  nodeProcessFacts,
  type ProcessFacts,
  parseLstartUtc,
  UNVERIFIED_MARKER_CEILING_MS,
  UpdateGate,
} from "./marker.ts";

const NOW_S = 1_800_000_000;
const NOW_MS = NOW_S * 1000;

function facts(
  table: Record<number, { start: number | null; zombie?: boolean }>,
  self = 1,
): ProcessFacts {
  return {
    selfPid: self,
    alive: (pid) => pid in table,
    zombie: (pid) => table[pid]?.zombie === true,
    startTime: async (pid) => table[pid]?.start ?? null,
  };
}

const body = (pid: number, start: number | null, created = NOW_S - 10) =>
  JSON.stringify({
    format: 1,
    owner: "indus-update",
    pid,
    process_start: start,
    created_at: created,
  });

describe("marker judgement", () => {
  it("absent, live, ours, stale", async () => {
    const f = facts({ 50: { start: NOW_S - 100 }, 1: { start: NOW_S - 999 } });
    expect(await judgeMarker(null, 0, NOW_MS, f)).toEqual({ state: "absent" });
    expect(await judgeMarker(body(50, NOW_S - 99), 1000, NOW_MS, f)).toMatchObject({
      state: "live",
      pid: 50,
      owner: "indus-update",
    });
    expect(await judgeMarker(body(1, NOW_S - 999), 1000, NOW_MS, f)).toEqual({
      state: "ours",
      pid: 1,
    });
    // pid reused by another process: its start time differs by more than 2 s
    expect(await judgeMarker(body(50, NOW_S - 500), 1000, NOW_MS, f)).toMatchObject({
      state: "stale",
      reason: "its process id now belongs to another process",
    });
    expect(await judgeMarker(body(77, NOW_S - 5), 1000, NOW_MS, f)).toMatchObject({
      state: "stale",
      reason: "its process has ended",
    });
  });

  it("a zombie owner is not live", async () => {
    const f = facts({ 60: { start: NOW_S - 5, zombie: true } });
    expect((await judgeMarker(body(60, NOW_S - 5), 1000, NOW_MS, f)).state).toBe("stale");
  });

  it("no comparable start time: live only for 20 minutes from creation", async () => {
    const f = facts({ 70: { start: null } });
    expect((await judgeMarker(body(70, null, NOW_S - 60), 0, NOW_MS, f)).state).toBe("live");
    const old = NOW_S - UNVERIFIED_MARKER_CEILING_MS / 1000 - 1;
    expect((await judgeMarker(body(70, null, old), 0, NOW_MS, f)).state).toBe("stale");
    // The writer recorded a start time but this machine cannot read one: same ceiling.
    expect((await judgeMarker(body(70, NOW_S - 30, NOW_S - 60), 0, NOW_MS, f)).state).toBe("live");
  });

  it("an empty marker is a claim being written for 5 s; garbage is stale", async () => {
    const f = facts({});
    expect((await judgeMarker("", EMPTY_MARKER_GRACE_MS - 1, NOW_MS, f)).state).toBe("live");
    expect((await judgeMarker("", EMPTY_MARKER_GRACE_MS, NOW_MS, f)).state).toBe("stale");
    expect((await judgeMarker("{not json", 0, NOW_MS, f)).state).toBe("stale");
    expect((await judgeMarker('{"pid": -1, "created_at": 1}', 0, NOW_MS, f)).state).toBe("stale");
  });

  it("start-time readers", () => {
    expect(parseLstartUtc("Mon Oct 21 03:45:00 2026")).toBe(Date.UTC(2026, 9, 21, 3, 45, 0) / 1000);
    expect(parseLstartUtc("garbage")).toBeNull();
    const stat = `123 (my (odd) name) S ${Array.from({ length: 18 }, (_, i) => i).join(" ")} 5000 rest`;
    expect(linuxStartSeconds(stat, "cpu 1\nbtime 1700000000\n", 100)).toBe(1_700_000_050);
  });
});

describe("marker on disk", () => {
  let home: string;
  const children: ChildProcess[] = [];

  beforeEach(() => {
    home = fs.mkdtempSync(path.join(os.tmpdir(), "marker-"));
  });

  afterEach(() => {
    for (const child of children.splice(0)) if (child.exitCode === null) child.kill("SIGKILL");
    fs.rmSync(home, { recursive: true, force: true });
  });

  it("is only read: a stale marker is left byte for byte", async () => {
    const text = body(999_999_7, NOW_S - 5);
    fs.writeFileSync(markerPath(home), text);
    const judged = await inspectMarker(home, facts({}), NOW_MS);
    expect(judged.state).toBe("stale");
    expect(fs.readFileSync(markerPath(home), "utf8")).toBe(text);
  });

  it.runIf(process.platform === "linux")(
    "real processes: a live owner is live, the same pid with another start time is stale",
    async () => {
      const child = spawn(process.execPath, ["-e", "setTimeout(() => {}, 30000)"], {
        stdio: "ignore",
      });
      children.push(child);
      const pid = child.pid as number;
      const real = nodeProcessFacts("linux");
      const start = (await real.startTime(pid)) as number;
      expect(typeof start).toBe("number");
      expect(Math.abs(start - Date.now() / 1000)).toBeLessThan(60);

      fs.writeFileSync(markerPath(home), body(pid, start, Date.now() / 1000));
      expect((await inspectMarker(home, real)).state).toBe("live");
      fs.writeFileSync(markerPath(home), body(pid, start - 3600, Date.now() / 1000));
      expect((await inspectMarker(home, real)).state).toBe("stale");
      fs.writeFileSync(
        markerPath(home),
        body(process.pid, (await real.startTime(process.pid)) as number),
      );
      expect((await inspectMarker(home, real)).state).toBe("ours");
    },
  );
});

describe("update gate", () => {
  let home: string;
  let timers: FakeTimers;
  let table: Record<number, { start: number | null }>;
  let gate: UpdateGate;

  beforeEach(() => {
    home = fs.mkdtempSync(path.join(os.tmpdir(), "gate-"));
    timers = new FakeTimers();
    timers.now = NOW_MS;
    table = { 42: { start: NOW_S - 50 } };
    gate = new UpdateGate({
      home,
      facts: facts(table),
      timers,
      now: () => timers.now,
      log: () => {},
    });
  });

  afterEach(() => fs.rmSync(home, { recursive: true, force: true }));

  async function tick(ms: number): Promise<void> {
    for (let i = 0; i < 10; i += 1) await Promise.resolve();
    timers.advance(ms);
    for (let i = 0; i < 10; i += 1) await Promise.resolve();
  }

  it("clear at once when nothing holds the runtime", async () => {
    expect(await gate.waitUntilClear()).toBe("clear");
    expect(gate.isRunning()).toBe(false);
  });

  it("a live owner holds backend starts until its marker goes; the clear is announced", async () => {
    fs.writeFileSync(markerPath(home), body(42, NOW_S - 50));
    let cleared = 0;
    gate.onCleared(() => {
      cleared += 1;
    });
    let outcome: string | null = null;
    const waits: string[] = [];
    void gate
      .waitUntilClear((reason) => waits.push(reason))
      .then((value) => {
        outcome = value;
      });
    // Far beyond any in-process ceiling: a live marker has no deadline.
    for (let i = 0; i < 5; i += 1) await tick(IN_FLIGHT_WAIT_MAX_MS);
    expect(outcome).toBeNull();
    expect(gate.isRunning()).toBe(true);
    fs.rmSync(markerPath(home));
    await tick(GATE_POLL_MS);
    expect(outcome).toBe("finished");
    expect(cleared).toBe(1);
    expect(waits.length).toBeGreaterThan(0);
  });

  it("a stale marker (pid reused) does not hold anything and stays on disk", async () => {
    const text = body(42, NOW_S - 9000);
    fs.writeFileSync(markerPath(home), text);
    expect(await gate.waitUntilClear()).toBe("clear");
    expect(fs.readFileSync(markerPath(home), "utf8")).toBe(text);
  });

  it("the in-process flag holds starts too, at most 20 minutes", async () => {
    gate.setInFlight(true);
    expect(gate.isRunning()).toBe(true);
    let outcome: string | null = null;
    void gate.waitUntilClear().then((value) => {
      outcome = value;
    });
    await tick(GATE_POLL_MS);
    expect(outcome).toBeNull();
    for (let i = 0; i <= IN_FLIGHT_WAIT_MAX_MS / GATE_POLL_MS; i += 1) await tick(GATE_POLL_MS);
    expect(outcome).toBe("timeout");
  });
});
