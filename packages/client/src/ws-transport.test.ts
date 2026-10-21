// P17-20: the WebSocket transport (fake WebSocket) and the channel on the daemon's wire.

import type { ServerRequest } from "@indus/protocol";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { type CloseInfo, DECLINE_CAPABILITY, RpcChannel, type Transport } from "./channel.ts";
import { IndusClient } from "./client.ts";
import { TransportReplaced } from "./errors.ts";
import type { TransportEvent } from "./localEvents.ts";
import { LogRing } from "./logRing.ts";
import { FakeServer } from "./testing/fakeServer.ts";
import {
  attachUrlProblem,
  closeReason,
  DASHBOARD_SUBPROTOCOL,
  DASHBOARD_TICKET_PREFIX,
  GATEWAY_SUBPROTOCOL,
  protocolsProblem,
  TICKET_SUBPROTOCOL_PREFIX,
  type WebSocketLike,
  WsTransport,
} from "./ws-transport.ts";

type Listener = (event: never) => void;

/** A scriptable stand-in for the WHATWG WebSocket: the test plays the daemon. */
class FakeSocket implements WebSocketLike {
  static made: FakeSocket[] = [];
  readyState = 0;
  protocol = "";
  binaryType = "blob";
  readonly sent: string[] = [];
  closedWith: { code?: number; reason?: string } | null = null;
  private readonly listeners = new Map<string, Listener[]>();

  readonly url: string;
  readonly protocols: string[];

  constructor(url: string, protocols: string[]) {
    this.url = url;
    this.protocols = protocols;
    FakeSocket.made.push(this);
  }

  addEventListener(type: string, listener: Listener): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(code?: number, reason?: string): void {
    this.closedWith = { ...(code === undefined ? {} : { code }), ...(reason ? { reason } : {}) };
    this.readyState = 2;
  }

  private fire(type: string, event: object): void {
    for (const listener of this.listeners.get(type) ?? []) (listener as (e: object) => void)(event);
  }

  accept(protocol = GATEWAY_SUBPROTOCOL): void {
    this.protocol = protocol;
    this.readyState = 1;
    this.fire("open", {});
  }

  push(data: unknown): void {
    this.fire("message", { data: typeof data === "string" ? data : data });
  }

  frame(value: object): void {
    this.push(JSON.stringify({ jsonrpc: "2.0", ...value }));
  }

  hangUp(code: number, reason = ""): void {
    this.readyState = 3;
    this.fire("close", { code, reason });
  }

  lastSent(): Record<string, unknown> {
    return JSON.parse(this.sent.at(-1) ?? "{}") as Record<string, unknown>;
  }
}

const factory = (url: string, protocols: string[]): WebSocketLike => new FakeSocket(url, protocols);
const URL_OK = "ws://127.0.0.1:47011/ws";
const ticket = (): string => `t${"A".repeat(20)}${Math.random().toString(36).slice(2, 12)}`;
const tick = () => new Promise<void>((done) => queueMicrotask(done));

function dial(protocols: string[], extra: { logs?: LogRing; events?: TransportEvent[] } = {}) {
  const events = extra.events ?? [];
  const transport = new WsTransport({
    url: URL_OK,
    protocols,
    webSocket: factory,
    logs: extra.logs ?? new LogRing(),
    onEvent: (event) => events.push(event),
  });
  const socket = FakeSocket.made.at(-1) as FakeSocket;
  return { transport, socket, events };
}

beforeEach(() => {
  FakeSocket.made = [];
});

describe("WsTransport", () => {
  it("offers our protocol and the ticket only as subprotocols, and never logs them", async () => {
    const logs = new LogRing();
    const secret = ticket();
    const { transport, socket } = dial([GATEWAY_SUBPROTOCOL, TICKET_SUBPROTOCOL_PREFIX + secret], {
      logs,
    });
    expect(socket.url).toBe(URL_OK);
    expect(socket.protocols).toEqual([GATEWAY_SUBPROTOCOL, TICKET_SUBPROTOCOL_PREFIX + secret]);
    socket.accept();
    expect(transport.open).toBe(true);
    const frames: unknown[] = [];
    transport.onFrame((frame) => frames.push(frame));
    socket.frame({ method: "gateway.ready", params: { heartbeat: true } });
    expect(frames).toEqual([
      { jsonrpc: "2.0", method: "gateway.ready", params: { heartbeat: true } },
    ]);
    transport.send({ jsonrpc: "2.0", id: 1, method: "gateway.ping" });
    expect(socket.lastSent()).toEqual({ jsonrpc: "2.0", id: 1, method: "gateway.ping" });
    socket.hangUp(1001, "daemon stopping");
    await tick();
    expect(logs.tail(50).join("\n")).not.toContain(secret);
    expect(logs.tail(50).join("\n")).not.toContain(TICKET_SUBPROTOCOL_PREFIX);
  });

  it("refuses a URL with secrets in it or an odd subprotocol list before dialling", async () => {
    expect(attachUrlProblem("ws://127.0.0.1:1/ws")).toBeNull();
    expect(attachUrlProblem("wss://127.0.0.1:1/ws")).toBeNull();
    expect(attachUrlProblem("ws://u:p@127.0.0.1:1/ws")).toMatch(/user-info/);
    expect(attachUrlProblem("ws://127.0.0.1:1/ws?ticket=x")).toMatch(/query/);
    expect(attachUrlProblem("ws://127.0.0.1:1/ws#x")).toMatch(/fragment/);
    expect(attachUrlProblem("http://127.0.0.1:1/ws")).toMatch(/ws:/);
    expect(protocolsProblem([GATEWAY_SUBPROTOCOL])).toBeNull();
    expect(protocolsProblem([DASHBOARD_SUBPROTOCOL, `${DASHBOARD_TICKET_PREFIX}r1`])).toBeNull();
    expect(protocolsProblem([DASHBOARD_SUBPROTOCOL, `${TICKET_SUBPROTOCOL_PREFIX}r1`])).toMatch(
      /unexpected/,
    );
    expect(protocolsProblem(["other"])).toMatch(/first/);
    expect(protocolsProblem([GATEWAY_SUBPROTOCOL, "x", "y"])).toMatch(/at most one/);
    expect(protocolsProblem([GATEWAY_SUBPROTOCOL, TICKET_SUBPROTOCOL_PREFIX])).toMatch(
      /unexpected/,
    );

    const closes: CloseInfo[] = [];
    const transport = new WsTransport({
      url: "ws://127.0.0.1:1/ws?ticket=abc",
      protocols: [GATEWAY_SUBPROTOCOL],
      webSocket: factory,
    });
    transport.onClose((info) => closes.push(info));
    await tick();
    expect(FakeSocket.made).toEqual([]);
    expect(closes).toEqual([{ reason: "refused: the URL carries a query", terminal: true }]);
    expect(transport.logs.tail(5).join("\n")).not.toContain("abc");
  });

  it("closes when the server echoes another subprotocol", () => {
    const { transport, socket, events } = dial([GATEWAY_SUBPROTOCOL]);
    const closes: CloseInfo[] = [];
    transport.onClose((info) => closes.push(info));
    socket.accept("something-else");
    expect(socket.closedWith).toEqual({ code: 1000, reason: "unexpected subprotocol" });
    expect(closes).toEqual([{ reason: "unexpected subprotocol", terminal: true }]);
    expect(events.at(-1)).toMatchObject({ type: "transport.closed", terminal: true });
    expect(() => transport.send({ jsonrpc: "2.0", method: "x" })).toThrow(/not open/);
  });

  it("reports binary and junk messages as protocol errors and keeps going", () => {
    const { transport, socket, events } = dial([GATEWAY_SUBPROTOCOL]);
    socket.accept();
    const frames: unknown[] = [];
    transport.onFrame((frame) => frames.push(frame));
    socket.push(new ArrayBuffer(4));
    socket.push("not json");
    socket.push("[1,2]");
    socket.frame({ method: "after" });
    expect(events.map((e) => e.type)).toEqual([
      "transport.protocol_error",
      "transport.protocol_error",
      "transport.protocol_error",
    ]);
    expect(frames).toEqual([{ jsonrpc: "2.0", method: "after" }]);
  });

  it("maps the daemon's close codes; 4403 is terminal, 4401 is not", async () => {
    expect(closeReason(4401)).toMatch(/ticket refused/);
    expect(closeReason(1013, "too many requests waiting")).toMatch(/busy.*too many/);
    for (const [code, terminal] of [
      [4401, false],
      [4403, true],
      [1013, false],
    ] as const) {
      const { transport, socket } = dial([GATEWAY_SUBPROTOCOL]);
      const closes: CloseInfo[] = [];
      transport.onClose((info) => closes.push(info));
      socket.accept();
      socket.hangUp(code, "x");
      socket.hangUp(code, "again");
      expect(closes).toHaveLength(1);
      expect(closes[0]?.code).toBe(code);
      expect(closes[0]?.terminal).toBe(terminal);
    }
  });

  it("gives up when the handshake does not finish in time and closes idempotently", async () => {
    vi.useFakeTimers();
    try {
      const transport = new WsTransport({
        url: URL_OK,
        protocols: [GATEWAY_SUBPROTOCOL],
        webSocket: factory,
        openTimeoutMs: 1_000,
      });
      const closes: CloseInfo[] = [];
      transport.onClose((info) => closes.push(info));
      vi.advanceTimersByTime(999);
      expect(closes).toEqual([]);
      vi.advanceTimersByTime(1);
      expect(closes).toEqual([{ reason: "connect timeout" }]);
      await transport.close();
      await transport.close();
      expect(closes).toHaveLength(1);

      const second = dial([GATEWAY_SUBPROTOCOL]);
      second.socket.accept();
      const closing = second.transport.close();
      expect(second.socket.closedWith).toEqual({ code: 1000, reason: "closed by client" });
      vi.advanceTimersByTime(2_000);
      await closing;
      expect(second.transport.alive).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });
});

// -- the channel on the daemon's wire ---------------------------------------------------------------

function approval(id: string, sessionId = "s1"): ServerRequest {
  return {
    kind: "approval",
    request_id: id,
    session_id: sessionId,
    command: "rm -r build",
    description: "clean up",
    choices: ["once", "deny"],
  };
}

const offer = (request: ServerRequest, generation: number, replayed = false) => ({
  jsonrpc: "2.0",
  method: "server_request",
  params: { request, replayed, generation },
});

const settle = async (rounds = 6) => {
  for (let i = 0; i < rounds; i += 1) await tick();
};

describe("channel over the daemon wire", () => {
  it("unwraps an offer and answers with its generation", async () => {
    const server = new FakeServer();
    const answers: Record<string, unknown>[] = [];
    server.handle("server_request.answer", (params) => {
      answers.push(params);
      return { accepted: true };
    });
    const client = new IndusClient({ connect: server.connect });
    const seen: { replayed: boolean; generation: number | undefined }[] = [];
    client.handle({
      serverRequests: {
        approval: (_request, meta) => {
          seen.push({ replayed: meta.replayed, generation: meta.generation });
          return { kind: "approval", choice: "once" };
        },
      },
    });
    client.start();
    server.sendRaw(offer(approval("srq-00000000000a"), 3));
    await settle();
    expect(seen).toEqual([{ replayed: false, generation: 3 }]);
    expect(answers).toEqual([
      {
        session_id: "s1",
        request_id: "srq-00000000000a",
        answer: { kind: "approval", choice: "once" },
        generation: 3,
      },
    ]);
  });

  it("delivers one id once across live, re-offer and resume copies; first answer wins", async () => {
    const server = new FakeServer();
    const answers: Record<string, unknown>[] = [];
    server.handle("server_request.answer", (params) => {
      answers.push(params);
      return { accepted: true };
    });
    server.handle("session.resume", () => ({
      session_id: "s1",
      message_count: 0,
      last_seq: 0,
      pending_requests: [approval("srq-00000000000b")],
    }));
    const client = new IndusClient({ connect: server.connect });
    let calls = 0;
    let release: (() => void) | undefined;
    client.handle({
      serverRequests: {
        approval: () => {
          calls += 1;
          return new Promise((done) => {
            release = () => done({ kind: "approval", choice: "deny" });
          });
        },
      },
    });
    client.start();
    server.sendRaw(offer(approval("srq-00000000000b"), 7));
    server.sendRaw(offer(approval("srq-00000000000b"), 7, true));
    await settle();
    await client.resume({ session_id: "s1" });
    await settle();
    expect(calls).toBe(1);
    release?.();
    await settle();
    server.sendRaw(offer(approval("srq-00000000000b"), 7, true));
    await settle();
    expect(calls).toBe(1);
    expect(answers).toHaveLength(1);
    expect(answers[0]).toMatchObject({ request_id: "srq-00000000000b", generation: 7 });
  });

  it("shows a re-offer again when the earlier answer never reached the daemon", async () => {
    const server = new FakeServer();
    const answers: Record<string, unknown>[] = [];
    server.handle("server_request.answer", (params) => {
      answers.push(params);
      return { accepted: true };
    });
    const client = new IndusClient({ connect: server.connect });
    const pending: Array<(choice: string) => void> = [];
    client.handle({
      serverRequests: {
        approval: () =>
          new Promise((done) => {
            pending.push((choice) => done({ kind: "approval", choice: choice as "deny" }));
          }),
      },
    });
    client.start();
    server.sendRaw(offer(approval("srq-00000000000c"), 2));
    await settle();
    server.die("socket cut");
    await settle();
    pending[0]?.("deny"); // the card was settled while the link was down: nothing is sent
    await settle();
    client.start();
    server.sendRaw(offer(approval("srq-00000000000c"), 2, true));
    await settle();
    expect(pending).toHaveLength(2);
    pending[1]?.("once");
    await settle();
    expect(answers).toEqual([
      {
        session_id: "s1",
        request_id: "srq-00000000000c",
        answer: { kind: "approval", choice: "once" },
        generation: 2,
      },
    ]);
  });

  it("answers a kind nobody here renders with -32601 at once, under the request id", async () => {
    const server = new FakeServer();
    const client = new IndusClient({ connect: server.connect });
    client.handle({ serverRequests: {} });
    client.start();
    server.sendRaw(
      offer({ kind: "sudo", request_id: "srq-00000000000d", session_id: "s1" } as ServerRequest, 1),
    );
    await settle();
    const refusal = server.received.find((frame) => frame.id === "srq-00000000000d");
    expect(refusal).toMatchObject({ error: { code: -32601 } });
    expect(server.calls("server_request.answer")).toEqual([]);
  });

  it("declines with server_request.decline only when the daemon advertised it", async () => {
    for (const advertised of [true, false]) {
      const server = new FakeServer();
      server.handle("server_request.decline", () => ({ accepted: true }));
      const client = new IndusClient({ connect: server.connect });
      client.handle({ serverRequests: { approval: () => new Promise(() => {}) } });
      client.start();
      server.sendRaw({
        jsonrpc: "2.0",
        method: "gateway.ready",
        params: {
          type: "gateway.ready",
          ...(advertised ? { capabilities: [DECLINE_CAPABILITY] } : {}),
        },
      });
      server.sendRaw(offer(approval("srq-00000000000e"), 4));
      await settle();
      expect(client.channel.decline("srq-00000000000e")).toBe(true);
      expect(client.channel.decline("srq-00000000000e")).toBe(false);
      await settle();
      if (advertised) {
        expect(server.calls("server_request.decline")).toHaveLength(1);
        expect(server.received.some((f) => f.id === "srq-00000000000e")).toBe(false);
      } else {
        expect(server.calls("server_request.decline")).toEqual([]);
        expect(server.received.find((f) => f.id === "srq-00000000000e")).toMatchObject({
          error: { code: -32601 },
        });
      }
    }
  });

  it("closes the card when the daemon settles the request elsewhere", async () => {
    const server = new FakeServer();
    const client = new IndusClient({ connect: server.connect });
    let signal: AbortSignal | undefined;
    client.handle({
      serverRequests: {
        approval: (_r, meta) => {
          signal = meta.signal;
          return new Promise(() => {});
        },
      },
    });
    client.start();
    server.sendRaw(offer(approval("srq-00000000000f"), 1));
    await settle();
    server.emit({ type: "request.settled", request_id: "srq-00000000000f", outcome: "answered" });
    await settle();
    expect(signal?.aborted).toBe(true);
    expect(client.channel.decline("srq-00000000000f")).toBe(false);
  });

  it("a transport swap rejects in-flight calls; the old socket's close is ignored", async () => {
    const sockets: FakeSocket[] = [];
    const lost: CloseInfo[] = [];
    const client = new IndusClient({
      connect: () => {
        const transport = new WsTransport({
          url: URL_OK,
          protocols: [GATEWAY_SUBPROTOCOL],
          webSocket: factory,
        });
        sockets.push(FakeSocket.made.at(-1) as FakeSocket);
        transport.onClose((info) => lost.push(info));
        return transport;
      },
    });
    client.start();
    sockets[0]?.accept();
    sockets[0]?.frame({ method: "gateway.ready", params: { heartbeat: false } });
    const waiting = client.channel.call("session.list", {}).catch((error: unknown) => error);
    client.start();
    expect(await waiting).toBeInstanceOf(TransportReplaced);
    sockets[1]?.accept();
    sockets[0]?.hangUp(1006);
    await settle();
    expect(client.connected).toBe(true);
    const events: string[] = [];
    client.subscribe((event) => events.push(event.type));
    sockets[1]?.frame({ method: "gateway.ready", params: {} });
    sockets[1]?.frame({ method: "notice", params: { type: "notice", text: "hi" } });
    // The first ready waited for the first listener (R05.2); then live ones flow at once.
    expect(events).toEqual(["gateway.ready", "gateway.ready", "notice"]);
    // Subscribed once, never reset: a later reconnect still delivers at once.
    client.start();
    sockets[2]?.accept();
    sockets[2]?.frame({ method: "gateway.ready", params: {} });
    expect(events.at(-1)).toBe("gateway.ready");
    expect(lost.length).toBeGreaterThanOrEqual(1);
  });

  it("counts any inbound frame as liveness on a WebSocket (R05.9)", () => {
    vi.useFakeTimers();
    try {
      const sockets: FakeSocket[] = [];
      const client = new IndusClient({
        connect: () => {
          const transport = new WsTransport({
            url: URL_OK,
            protocols: [GATEWAY_SUBPROTOCOL],
            webSocket: factory,
          });
          sockets.push(FakeSocket.made.at(-1) as FakeSocket);
          return transport;
        },
      });
      client.start();
      const socket = sockets[0] as FakeSocket;
      socket.accept();
      socket.frame({ method: "gateway.ready", params: { heartbeat: true } });
      // The daemon streams deltas but never answers a ping: still alive.
      for (let second = 0; second < 120; second += 1) {
        vi.advanceTimersByTime(1_000);
        socket.frame({ method: "message.delta", params: { type: "message.delta", text: "." } });
      }
      expect(socket.closedWith).toBeNull();
      // Silence for the whole 45 s deadline closes the socket.
      vi.advanceTimersByTime(45_000);
      expect(socket.closedWith).not.toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("RpcChannel on a plain transport", () => {
  it("still declines with the kind's answer on the bridge wire", () => {
    const sent: Record<string, unknown>[] = [];
    const listeners: Array<(frame: unknown) => void> = [];
    const transport: Transport = {
      send: (frame) => void sent.push(frame as Record<string, unknown>),
      onFrame: (listener) => {
        listeners.push(listener);
        return () => {};
      },
      onClose: () => () => {},
      close: () => {},
    };
    const channel = new RpcChannel();
    channel.attach(transport);
    channel.setHandlers({ serverRequests: {} });
    for (const listener of listeners) {
      listener({ jsonrpc: "2.0", method: "server_request", params: approval("srq-000000000010") });
    }
    expect(sent.at(-1)).toMatchObject({
      method: "server_request.answer",
      params: { answer: { kind: "approval", choice: "deny" } },
    });
    expect(sent.at(-1)?.params).not.toHaveProperty("generation");
  });
});
