// P17-20: replay cursors and the client's resume from them after a reconnect.

import { describe, expect, it } from "vitest";
import { IndusClient } from "./client.ts";
import type { ClientEvent } from "./localEvents.ts";
import { REPLAY_CURSOR_CAPACITY, REPLAY_GAP_EVENT, ReplayCursors } from "./replay.ts";
import { FakeServer } from "./testing/fakeServer.ts";

const tick = () => new Promise<void>((done) => queueMicrotask(done));
const settle = async (rounds = 8) => {
  for (let i = 0; i < rounds; i += 1) await tick();
};

describe("ReplayCursors", () => {
  it("applies new events, drops repeats and passes untracked ones", () => {
    const cursors = new ReplayCursors();
    cursors.setEpoch("e1");
    expect(cursors.admit({ type: "message.delta", session_id: "s1", seq: 1 })).toBe("apply");
    expect(cursors.admit({ type: "message.delta", session_id: "s1", seq: 2 })).toBe("apply");
    expect(cursors.admit({ type: "message.delta", session_id: "s1", seq: 2 })).toBe("duplicate");
    expect(cursors.admit({ type: "message.delta", session_id: "s1", seq: 1 })).toBe("duplicate");
    expect(cursors.admit({ type: "transport.exit" })).toBe("untracked");
    expect(cursors.admit({ type: "notice", session_id: "s1" })).toBe("untracked");
    expect(cursors.admit("junk")).toBe("untracked");
    expect(cursors.cursor("s1")).toEqual({ epoch: "e1", seq: 2 });
    // An event that names its own epoch: another daemon run starts again from its seq.
    expect(
      cursors.admit({ type: "message.delta", session_id: "s1", seq: 1, replay_epoch: "e2" }),
    ).toBe("apply");
    expect(cursors.cursor("s1")).toEqual({ epoch: "e2", seq: 1 });
  });

  it("a gap resets the cursor to the gap's last seq in its epoch", () => {
    const cursors = new ReplayCursors();
    cursors.reset("s1", "e1", 40);
    const gap = { type: REPLAY_GAP_EVENT, session_id: "s1", replay_epoch: "e2", last_seq: 7 };
    expect(cursors.admit(gap)).toBe("gap");
    expect(cursors.cursor("s1")).toEqual({ epoch: "e2", seq: 7 });
    expect(cursors.resumeParams("s1")).toEqual({ session_id: "s1", epoch: "e2", cursor: 7 });
  });

  it("advance never moves back within an epoch", () => {
    const cursors = new ReplayCursors();
    cursors.advance("s1", "e1", 5);
    cursors.advance("s1", "e1", 3);
    expect(cursors.cursor("s1")?.seq).toBe(5);
    cursors.advance("s1", "e2", 1);
    expect(cursors.cursor("s1")).toEqual({ epoch: "e2", seq: 1 });
    cursors.forget("s1");
    expect(cursors.resumeParams("s1")).toEqual({ session_id: "s1" });
    cursors.advance("s2", null, 4);
    expect(cursors.resumeParams("s2")).toEqual({ session_id: "s2", cursor: 4 });
  });

  it("keeps at most 256 sessions, dropping the least recently touched", () => {
    expect(REPLAY_CURSOR_CAPACITY).toBe(256);
    const cursors = new ReplayCursors();
    for (let i = 0; i < 256; i += 1) cursors.advance(`s${i}`, "e", 1);
    cursors.admit({ session_id: "s0", seq: 2, type: "x" }); // s0 is fresh again
    cursors.advance("s256", "e", 1);
    expect(cursors.size).toBe(256);
    expect(cursors.cursor("s0")?.seq).toBe(2);
    expect(cursors.cursor("s1")).toBeUndefined();
    expect(cursors.evicted).toBe(1);
  });
});

/** The daemon's event log for one session, as far as replay is concerned. */
class FakeLog {
  epoch: string;
  readonly events: Array<Record<string, unknown>> = [];
  constructor(epoch: string) {
    this.epoch = epoch;
  }
  add(server: FakeServer, text: string): void {
    const event = {
      type: "message.delta",
      session_id: "s1",
      seq: this.events.length + 1,
      replay_epoch: this.epoch,
      text,
    };
    this.events.push(event);
    server.sendRaw({ jsonrpc: "2.0", method: event.type, params: event });
  }
}

function daemon(log: FakeLog, overlap = 0) {
  const server = new FakeServer();
  server.handle("client.capabilities", (params) => ({ features: params.features }));
  server.handle("session.subscribe", (params) => {
    const cursor = typeof params.cursor === "number" ? params.cursor : null;
    const last = log.events.length;
    if (cursor !== null && params.epoch !== log.epoch) {
      const gap = { reason: "epoch_changed", replay_epoch: log.epoch, last_seq: last };
      server.sendRaw({
        jsonrpc: "2.0",
        method: REPLAY_GAP_EVENT,
        params: { type: REPLAY_GAP_EVENT, session_id: "s1", ...gap },
      });
      return { session_id: "s1", replay_epoch: log.epoch, last_seq: last, replayed: 0, gap };
    }
    const from = cursor === null ? last : Math.max(0, cursor - overlap);
    const missed = log.events.slice(from);
    for (const event of missed) {
      server.sendRaw({ jsonrpc: "2.0", method: String(event.type), params: event });
    }
    return { session_id: "s1", replay_epoch: log.epoch, last_seq: last, replayed: missed.length };
  });
  server.handle("events.since", (params) => {
    const cursor = Number(params.cursor);
    return {
      session_id: "s1",
      replay_epoch: log.epoch,
      last_seq: log.events.length,
      events: log.events.slice(Math.max(0, cursor - overlap)),
    };
  });
  return server;
}

const ready = (server: FakeServer, epoch: string) =>
  server.sendRaw({
    jsonrpc: "2.0",
    method: "gateway.ready",
    params: { type: "gateway.ready", replay_epoch: epoch, heartbeat: false },
  });

const texts = (events: ClientEvent[]): string[] =>
  events.filter((e) => e.type === "message.delta").map((e) => (e as { text: string }).text);

describe("IndusClient replay", () => {
  it("resumes every tracked session from its cursor after a reconnect, without duplicates", async () => {
    const log = new FakeLog("e1");
    const server = daemon(log, 2);
    const client = new IndusClient({ connect: server.connect, replay: { resubscribe: true } });
    const seen: ClientEvent[] = [];
    client.subscribe((event) => seen.push(event));
    client.start();
    ready(server, "e1");
    await client.request("client.capabilities", { features: ["server_requests"], surface: "tui" });
    await client.subscribeSession("s1");
    log.add(server, "a");
    log.add(server, "b");
    log.add(server, "b"); // the same text again is a new event, not a duplicate
    await settle();
    expect(client.replay?.cursor("s1")).toEqual({ epoch: "e1", seq: 3 });

    server.die("socket cut");
    await settle();
    log.events.push(
      { type: "message.delta", session_id: "s1", seq: 4, replay_epoch: "e1", text: "c" },
      { type: "message.delta", session_id: "s1", seq: 5, replay_epoch: "e1", text: "d" },
    );
    client.start();
    ready(server, "e1");
    await settle(20);
    const methods = server.received.map((frame) => frame.method).filter(Boolean);
    expect(methods).toEqual([
      "client.capabilities",
      "session.subscribe",
      "client.capabilities",
      "session.subscribe",
    ]);
    expect(server.calls("session.subscribe").at(-1)?.params).toEqual({
      session_id: "s1",
      epoch: "e1",
      cursor: 3,
    });
    expect(texts(seen)).toEqual(["a", "b", "b", "c", "d"]);
    const seqs = seen
      .filter((e) => e.type === "message.delta")
      .map((e) => (e as { seq: number }).seq);
    expect(seqs).toEqual([1, 2, 3, 4, 5]);
  });

  it("another daemon run gives a gap: the surface sees it and the cursor moves to the new epoch", async () => {
    const log = new FakeLog("e1");
    const server = daemon(log);
    const client = new IndusClient({ connect: server.connect, replay: { resubscribe: true } });
    const seen: ClientEvent[] = [];
    client.subscribe((event) => seen.push(event));
    client.start();
    ready(server, "e1");
    await client.subscribeSession("s1");
    log.add(server, "a");
    await settle();
    server.die();
    await settle();
    log.epoch = "e2";
    log.events.splice(0, log.events.length);
    client.start();
    ready(server, "e2");
    await settle(20);
    expect(seen.some((event) => (event.type as string) === REPLAY_GAP_EVENT)).toBe(true);
    expect(client.replay?.cursor("s1")).toEqual({ epoch: "e2", seq: 0 });
    log.add(server, "fresh");
    await settle();
    expect(texts(seen)).toEqual(["a", "fresh"]);
  });

  it("without resubscribe a reconnect sends nothing by itself; duplicates are still dropped", async () => {
    const log = new FakeLog("e1");
    const server = daemon(log);
    const client = new IndusClient({ connect: server.connect, replay: true });
    const seen: ClientEvent[] = [];
    client.subscribe((event) => seen.push(event));
    client.start();
    ready(server, "e1");
    log.add(server, "a");
    server.sendRaw({ jsonrpc: "2.0", method: "message.delta", params: log.events[0] });
    await settle();
    server.die();
    await settle();
    client.start();
    ready(server, "e1");
    await settle();
    expect(server.received).toEqual([]);
    expect(texts(seen)).toEqual(["a"]);
  });

  it("events.since pulls what was missed through the same filter", async () => {
    const log = new FakeLog("e1");
    const server = daemon(log, 1);
    const client = new IndusClient({ connect: server.connect, replay: true });
    const seen: ClientEvent[] = [];
    client.subscribe((event) => seen.push(event));
    client.start();
    ready(server, "e1");
    log.add(server, "a");
    await settle();
    log.events.push({
      type: "message.delta",
      session_id: "s1",
      seq: 2,
      replay_epoch: "e1",
      text: "b",
    });
    const result = await client.eventsSince("s1");
    expect(result.last_seq).toBe(2);
    expect(server.calls("events.since")[0]?.params).toEqual({
      session_id: "s1",
      cursor: 1,
      epoch: "e1",
    });
    expect(texts(seen)).toEqual(["a", "b"]);
  });

  it("a ready without an epoch (the stdio bridge) drops every cursor", async () => {
    const server = new FakeServer();
    const client = new IndusClient({ connect: server.connect, replay: true });
    const seen: ClientEvent[] = [];
    client.subscribe((event) => seen.push(event));
    client.start();
    server.ready();
    server.sendRaw({
      jsonrpc: "2.0",
      method: "message.delta",
      params: { type: "message.delta", session_id: "s1", seq: 9, text: "old" },
    });
    await settle();
    server.die();
    await settle();
    client.start();
    server.ready();
    server.sendRaw({
      jsonrpc: "2.0",
      method: "message.delta",
      params: { type: "message.delta", session_id: "s1", seq: 1, text: "new" },
    });
    await settle();
    expect(texts(seen)).toEqual(["old", "new"]);
  });

  it("resume sets the cursor to the session's last seq", async () => {
    const server = new FakeServer();
    server.handle("session.resume", () => ({ session_id: "s9", message_count: 2, last_seq: 12 }));
    const client = new IndusClient({ connect: server.connect, replay: true });
    client.start();
    ready(server, "e7");
    await settle();
    await client.resume({ session_id: "s9" });
    expect(client.replay?.cursor("s9")).toEqual({ epoch: "e7", seq: 12 });
  });
});
