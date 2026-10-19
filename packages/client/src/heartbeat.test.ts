import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { IndusClient } from "./client.ts";
import { Heartbeat } from "./heartbeat.ts";
import { FakeServer } from "./testing/fakeServer.ts";

describe("Heartbeat", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("stays alive for 10 minutes on inbound traffic alone, pings never answered", async () => {
    const dead: string[] = [];
    const beat = new Heartbeat({
      sendPing: () => new Promise(() => undefined),
      onDead: (why) => dead.push(why),
    });
    beat.start();
    for (let second = 0; second < 600; second += 1) {
      await vi.advanceTimersByTimeAsync(1_000);
      beat.noteInbound();
    }
    expect(dead).toEqual([]);
    expect(beat.active).toBe(true);
    beat.stop();
  });

  it("closes after exactly 45,000 ms of silence", async () => {
    const dead: number[] = [];
    const beat = new Heartbeat({
      sendPing: () => new Promise(() => undefined),
      onDead: () => dead.push(Date.now()),
    });
    const started = Date.now();
    beat.start();
    await vi.advanceTimersByTimeAsync(7_000);
    beat.noteInbound();
    const lastFrame = Date.now();
    await vi.advanceTimersByTimeAsync(44_999);
    expect(dead).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    expect(dead).toEqual([lastFrame + 45_000]);
    expect(lastFrame - started).toBe(7_000);
    expect(beat.active).toBe(false);
  });

  it("does not send a ninth ping while eight are unanswered", async () => {
    const beat = new Heartbeat({
      sendPing: () => new Promise(() => undefined),
      onDead: () => undefined,
    });
    beat.start();
    for (let i = 0; i < 10; i += 1) {
      await vi.advanceTimersByTimeAsync(15_000);
      beat.noteInbound();
    }
    expect(beat.sent).toBe(8);
    expect(beat.unanswered).toBe(8);
    beat.stop();
  });

  it("frees a slot when a ping is answered, and dies when a ping cannot be sent", async () => {
    let answer: () => void = () => undefined;
    const dead: string[] = [];
    let failing = false;
    const beat = new Heartbeat({
      sendPing: () => {
        if (failing) throw new Error("socket gone");
        return new Promise<void>((resolve) => (answer = resolve));
      },
      onDead: (why) => dead.push(why),
    });
    beat.start();
    await vi.advanceTimersByTimeAsync(15_000);
    expect(beat.unanswered).toBe(1);
    answer();
    await vi.advanceTimersByTimeAsync(0);
    expect(beat.unanswered).toBe(0);
    failing = true;
    beat.noteInbound();
    await vi.advanceTimersByTimeAsync(15_000);
    expect(dead[0]).toContain("socket gone");
  });

  it("client starts pinging only when ready advertises it, and closes a silent link", async () => {
    const server = new FakeServer();
    const client = new IndusClient({ connect: server.connect });
    client.start();
    server.ready({ heartbeat: true });
    await vi.advanceTimersByTimeAsync(15_000);
    expect(server.calls("gateway.ping")).toHaveLength(1);
    server.stall();
    await vi.advanceTimersByTimeAsync(45_000);
    expect(server.alive).toBe(false);
    expect(client.connected).toBe(false);
    await client.close();

    const quiet = new FakeServer();
    const plain = new IndusClient({ connect: quiet.connect });
    plain.start();
    quiet.ready();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(quiet.calls("gateway.ping")).toHaveLength(0);
    await plain.close();
  });
});
