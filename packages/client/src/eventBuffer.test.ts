import type { ServerRequest } from "@indus/protocol";
import { describe, expect, it } from "vitest";
import { IndusClient } from "./client.ts";
import { EventBuffer } from "./eventBuffer.ts";
import type { ClientEvent } from "./localEvents.ts";
import { Ring } from "./ring.ts";
import { FakeServer } from "./testing/fakeServer.ts";

const settle = async (rounds = 4) => {
  for (let i = 0; i < rounds; i += 1) await Promise.resolve();
};

describe("EventBuffer", () => {
  it("keeps the newest 2,000 of 2,001 early events, in order", () => {
    const buffer = new EventBuffer<number>();
    for (let i = 0; i < 2_001; i += 1) buffer.push(i);
    expect(buffer.waiting).toBe(2_000);
    expect(buffer.dropped).toBe(1);
    const seen: number[] = [];
    buffer.subscribe((event) => seen.push(event));
    expect(seen).toHaveLength(2_000);
    expect(seen[0]).toBe(1);
    expect(seen.at(-1)).toBe(2_000);
    expect(
      seen.every((value, index) => index === 0 || value === (seen[index - 1] as number) + 1),
    ).toBe(true);
  });

  it("delivers live events after the first subscribe and never buffers again", () => {
    const buffer = new EventBuffer<string>();
    buffer.push("early");
    const seen: string[] = [];
    const stop = buffer.subscribe((event) => seen.push(event));
    buffer.push("live");
    stop();
    buffer.push("nobody listening");
    expect(buffer.subscribed).toBe(true);
    expect(buffer.waiting).toBe(0);
    const later: string[] = [];
    buffer.subscribe((event) => later.push(event));
    buffer.push("next");
    expect(seen).toEqual(["early", "live"]);
    expect(later).toEqual(["next"]);
  });

  it("keeps delivering to the others when one listener throws", () => {
    const errors: unknown[] = [];
    const buffer = new EventBuffer<number>({ onListenerError: (error) => errors.push(error) });
    const seen: number[] = [];
    buffer.subscribe(() => {
      throw new Error("bad listener");
    });
    buffer.subscribe((event) => seen.push(event));
    buffer.push(7);
    expect(seen).toEqual([7]);
    expect(errors).toHaveLength(1);
  });

  it("ring rejects a bad capacity", () => {
    expect(() => new Ring(0)).toThrow(RangeError);
  });
});

describe("IndusClient early arrivals", () => {
  const approval: ServerRequest = {
    kind: "approval",
    request_id: "early-1",
    session_id: "s-fake",
    command: "make deploy",
    description: "deploy",
    choices: ["once", "deny"],
  };

  it("answers a server request that came before the handlers were installed", async () => {
    const server = new FakeServer();
    const client = new IndusClient({ connect: server.connect });
    client.start();
    const answer = server.ask(approval);
    await settle();
    expect(server.calls("server_request.answer")).toHaveLength(0);
    client.handle({ serverRequests: { approval: () => ({ kind: "approval", choice: "once" }) } });
    await expect(answer).resolves.toEqual({ kind: "approval", choice: "once" });
    await client.close();
  });

  it("delivers events after a reconnect without a second subscribe", async () => {
    const server = new FakeServer();
    const client = new IndusClient({ connect: server.connect });
    client.start();
    server.ready();
    await settle();
    const seen: string[] = [];
    client.subscribe((event: ClientEvent) => seen.push(event.type));
    server.die();
    await settle();
    client.start();
    server.ready();
    server.emit({ type: "notice", level: "info", text: "back" });
    await settle();
    expect(seen).toEqual(["gateway.ready", "gateway.ready", "notice"]);
    expect(server.connections).toBe(2);
    await client.close();
  });
});
