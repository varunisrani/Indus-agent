import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { backoffDelay, type ReconnectingEvent, Reconnector } from "./backoff.ts";
import { IndusClient } from "./client.ts";
import type { ClientEvent } from "./localEvents.ts";
import { FakeServer } from "./testing/fakeServer.ts";

describe("backoffDelay", () => {
  it("doubles from 1 s and stops at 30 s", () => {
    const ladder = [1, 2, 3, 4, 5, 6, 7].map((attempt) => backoffDelay(attempt) / 1_000);
    expect(ladder).toEqual([1, 2, 4, 8, 16, 30, 30]);
    expect(backoffDelay(500)).toBe(30_000);
  });

  it("full jitter stays inside the step", () => {
    expect(backoffDelay(3, { jitter: "full", random: () => 0 })).toBe(0);
    expect(backoffDelay(3, { jitter: "full", random: () => 0.999 })).toBe(3_996);
    for (let i = 0; i < 50; i += 1) {
      const delay = backoffDelay(6, { jitter: "full" });
      expect(delay).toBeGreaterThanOrEqual(0);
      expect(delay).toBeLessThan(30_000);
    }
  });
});

describe("Reconnector", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("announces each attempt, keeps one timer, and resets after ready", async () => {
    const events: ReconnectingEvent[] = [];
    let dials = 0;
    const reconnector = new Reconnector({
      reconnect: () => {
        dials += 1;
      },
      onReconnecting: (event) => events.push(event),
    });
    expect(reconnector.schedule()?.delay_ms).toBe(1_000);
    expect(reconnector.schedule()).toBeNull();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(dials).toBe(1);
    reconnector.schedule();
    await vi.advanceTimersByTimeAsync(2_000);
    reconnector.markReady();
    reconnector.schedule();
    reconnector.cancel();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(dials).toBe(2);
    expect(events).toEqual([
      { type: "transport.reconnecting", attempt: 1, delay_ms: 1_000 },
      { type: "transport.reconnecting", attempt: 2, delay_ms: 2_000 },
      { type: "transport.reconnecting", attempt: 1, delay_ms: 1_000 },
    ]);
  });

  it("drives client redials along the ladder until ready", async () => {
    const server = new FakeServer();
    const seen: ClientEvent[] = [];
    const client = new IndusClient({ connect: server.connect, reconnect: {} });
    client.subscribe((event) => seen.push(event));
    client.start();
    server.die();
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(1_000);
    server.die();
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(2_000);
    server.ready();
    await vi.advanceTimersByTimeAsync(0);
    server.die();
    await vi.advanceTimersByTimeAsync(0);
    const delays = seen
      .filter((event) => event.type === "transport.reconnecting")
      .map((event) => (event as ReconnectingEvent).delay_ms);
    expect(delays).toEqual([1_000, 2_000, 1_000]);
    expect(server.connections).toBe(3);
    await client.close();
    await vi.advanceTimersByTimeAsync(5_000);
    expect(server.connections).toBe(3);
  });
});
