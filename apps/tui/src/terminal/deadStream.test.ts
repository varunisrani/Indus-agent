import { EventEmitter } from "node:events";
import { describe, expect, it, vi } from "vitest";
import { DEAD_STREAM_LIMIT, DeadStreamGuard, isDeadStreamError } from "./deadStream.ts";
import { EXIT_DEAD_TERMINAL, Lifecycle } from "./lifecycle.ts";
import { MemoryMonitor } from "./memoryMonitor.ts";

const epipe = () => Object.assign(new Error("write EPIPE"), { code: "EPIPE" });
const eio = () => Object.assign(new Error("write EIO"), { code: "EIO" });

function wired() {
  const exits: number[] = [];
  const bridge = { killed: 0 };
  const writes: string[] = [];
  const life = new Lifecycle({
    out: { isTTY: true, write: (t) => writes.push(t) },
    proc: { on: () => undefined, exit: (code?: number) => void exits.push(code ?? 0) as never },
    log: () => {},
  });
  life.setBridgeStopper(() => {
    bridge.killed += 1;
  });
  const guard = new DeadStreamGuard({
    onDead: () => life.quit(EXIT_DEAD_TERMINAL, "dead-terminal"),
  });
  return { exits, bridge, writes, guard };
}

describe("dead terminal", () => {
  it("knows EIO and EPIPE only", () => {
    expect(isDeadStreamError(epipe())).toBe(true);
    expect(isDeadStreamError(eio())).toBe(true);
    expect(isDeadStreamError(Object.assign(new Error("x"), { code: "EAGAIN" }))).toBe(false);
    expect(isDeadStreamError(null)).toBe(false);
  });

  it("does not exit on the fourth EPIPE", () => {
    const { exits, bridge, guard } = wired();
    for (let i = 0; i < DEAD_STREAM_LIMIT - 1; i++) guard.noteError(epipe());
    expect(exits).toEqual([]);
    expect(bridge.killed).toBe(0);
  });

  it("stops the bridge and exits 1 on the fifth", () => {
    const { exits, bridge, writes, guard } = wired();
    for (let i = 0; i < DEAD_STREAM_LIMIT; i++) guard.noteError(i % 2 === 0 ? epipe() : eio());
    expect(bridge.killed).toBe(1);
    expect(exits).toEqual([1]);
    expect(writes).toHaveLength(1);
    guard.noteError(epipe());
    expect(exits).toEqual([1]);
  });

  it("starts counting again after any other error or a good write", () => {
    const { exits, guard } = wired();
    for (let i = 0; i < 4; i++) guard.noteError(epipe());
    guard.noteError(new Error("something else"));
    for (let i = 0; i < 4; i++) guard.noteError(epipe());
    guard.noteWrite();
    for (let i = 0; i < 4; i++) guard.noteError(epipe());
    expect(exits).toEqual([]);
    expect(guard.count).toBe(4);
  });

  it("counts the stream's own error events until detached", () => {
    const { exits, guard } = wired();
    const stream = new EventEmitter();
    const detach = guard.watch(stream);
    for (let i = 0; i < 4; i++) stream.emit("error", epipe());
    detach();
    expect(stream.listenerCount("error")).toBe(0);
    expect(exits).toEqual([]);
  });
});

describe("memory monitor", () => {
  it("warns once per window on fast growth and goes critical near the limit", () => {
    let now = 0;
    let used = 100;
    const onWarn = vi.fn();
    const onCritical = vi.fn();
    const monitor = new MemoryMonitor({
      read: () => ({ used, limit: 1000 }),
      now: () => now,
      onWarn,
      onCritical,
    });
    expect(monitor.check()).toBe("ok");
    now = 10_000;
    used = 400;
    expect(monitor.check()).toBe("warn");
    now = 20_000;
    used = 700;
    expect(monitor.check()).toBe("ok");
    expect(onWarn).toHaveBeenCalledTimes(1);
    now = 30_000;
    used = 950;
    expect(monitor.check()).toBe("critical");
    expect(monitor.check()).toBe("critical");
    expect(onCritical).toHaveBeenCalledTimes(1);
  });

  it("does not warn on slow growth", () => {
    let now = 0;
    let used = 100;
    const onWarn = vi.fn();
    const monitor = new MemoryMonitor({
      read: () => ({ used, limit: 1000 }),
      now: () => now,
      onWarn,
      onCritical: vi.fn(),
    });
    for (let i = 0; i < 20; i++) {
      now += 30_000;
      used += 50;
      monitor.check();
    }
    expect(onWarn).not.toHaveBeenCalled();
  });
});
