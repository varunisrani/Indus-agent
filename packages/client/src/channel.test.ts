import type { ServerRequest } from "@indus/protocol";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  type CloseInfo,
  declineAnswer,
  type OutboundFrame,
  RpcChannel,
  type Transport,
} from "./channel.ts";
import {
  GatewayRpcError,
  NotConnected,
  RequestAborted,
  RpcTimeout,
  TransportClosed,
  TransportReplaced,
} from "./errors.ts";
import { RPC_TIMEOUT_FLOOR_MS, rpcTimeoutFromEnv } from "./limits.ts";

/** Synchronous in-memory transport: `sent` records frames, `feed` plays the server. */
class MemoryTransport implements Transport {
  readonly sent: Array<Record<string, unknown>> = [];
  private readonly frames = new Set<(frame: unknown) => void>();
  private readonly closes = new Set<(info: CloseInfo) => void>();
  failSends = false;

  send(frame: OutboundFrame): void {
    if (this.failSends) throw new Error("pipe gone");
    this.sent.push(frame as Record<string, unknown>);
  }
  onFrame(listener: (frame: unknown) => void) {
    this.frames.add(listener);
    return () => this.frames.delete(listener);
  }
  onClose(listener: (info: CloseInfo) => void) {
    this.closes.add(listener);
    return () => this.closes.delete(listener);
  }
  close(): void {
    this.fireClose("closed");
  }
  feed(frame: Record<string, unknown>): void {
    for (const listener of [...this.frames]) listener({ jsonrpc: "2.0", ...frame });
  }
  fireClose(reason: string): void {
    for (const listener of [...this.closes]) listener({ reason });
  }
  last(): Record<string, unknown> {
    return this.sent.at(-1) ?? {};
  }
}

const tick = () => Promise.resolve();

/** The rejection of a call that must fail. */
const caught = <E extends Error = Error>(promise: Promise<unknown>): Promise<E> =>
  promise.then(
    () => {
      throw new Error("expected the call to fail");
    },
    (error: unknown) => error as E,
  );

function approval(id: string): ServerRequest {
  return {
    kind: "approval",
    request_id: id,
    session_id: "s1",
    command: "rm -rf build",
    description: "delete the build folder",
    choices: ["once", "deny"],
  };
}

describe("RpcChannel calls", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("times out at the env floor when the env asks for less", async () => {
    const timeoutMs = rpcTimeoutFromEnv("1000");
    expect(timeoutMs).toBe(RPC_TIMEOUT_FLOOR_MS);
    const link = new MemoryTransport();
    const channel = new RpcChannel({ timeoutMs });
    channel.attach(link);
    const outcome = caught<RpcTimeout>(channel.call("session.resume", { session_id: "s1" }));
    await vi.advanceTimersByTimeAsync(RPC_TIMEOUT_FLOOR_MS - 1);
    expect(channel.pendingCount).toBe(1);
    await vi.advanceTimersByTimeAsync(1);
    const error = await outcome;
    expect(error).toBeInstanceOf(RpcTimeout);
    expect(error.timeoutMs).toBe(30_000);
    expect(channel.pendingCount).toBe(0);
  });

  it("uses the default timeout without env and honours a per-call override", async () => {
    expect(rpcTimeoutFromEnv(undefined)).toBe(120_000);
    expect(rpcTimeoutFromEnv("not a number")).toBe(120_000);
    expect(rpcTimeoutFromEnv("45000")).toBe(45_000);
    const link = new MemoryTransport();
    const channel = new RpcChannel();
    channel.attach(link);
    const quick = caught(channel.call("session.steer", {}, { timeoutMs: 50 }));
    await vi.advanceTimersByTimeAsync(50);
    expect(await quick).toBeInstanceOf(RpcTimeout);
  });

  it("drops an answer that arrives after the timeout", async () => {
    const lines: string[] = [];
    const link = new MemoryTransport();
    const channel = new RpcChannel({ timeoutMs: 100, onDiagnostic: (line) => lines.push(line) });
    channel.attach(link);
    const outcome = caught(channel.call("prompt.submit", {}));
    await vi.advanceTimersByTimeAsync(100);
    expect(await outcome).toBeInstanceOf(RpcTimeout);
    link.feed({ id: 1, result: { turn_id: "t1", generation: 1 } });
    expect(lines.some((line) => line.includes("answer for id 1 dropped"))).toBe(true);
  });

  it("aborts through the caller's signal, before and after sending", async () => {
    const link = new MemoryTransport();
    const channel = new RpcChannel();
    channel.attach(link);
    const already = new AbortController();
    already.abort();
    await expect(
      channel.call("session.create", {}, { signal: already.signal }),
    ).rejects.toBeInstanceOf(RequestAborted);
    expect(link.sent).toHaveLength(0);
    const later = new AbortController();
    const outcome = channel.call("session.create", {}, { signal: later.signal });
    later.abort();
    await expect(outcome).rejects.toBeInstanceOf(RequestAborted);
    expect(channel.pendingCount).toBe(0);
  });

  it("numbers calls with integers from 1 and resolves by id", async () => {
    const link = new MemoryTransport();
    const channel = new RpcChannel();
    channel.attach(link);
    const first = channel.call("session.create", {});
    const second = channel.call("session.create", {});
    expect(link.sent.map((frame) => frame.id)).toEqual([1, 2]);
    link.feed({ id: 2, result: { session_id: "b", created: true } });
    link.feed({ id: 1, result: { session_id: "a", created: true } });
    await expect(first).resolves.toEqual({ session_id: "a", created: true });
    await expect(second).resolves.toEqual({ session_id: "b", created: true });
  });

  it("keeps a null result", async () => {
    const link = new MemoryTransport();
    const channel = new RpcChannel();
    channel.attach(link);
    const outcome = channel.call("x.y");
    link.feed({ id: 1, result: null });
    await expect(outcome).resolves.toBeNull();
  });

  it("fails at once without a transport or when the send throws", async () => {
    const channel = new RpcChannel();
    await expect(channel.call("session.create", {})).rejects.toBeInstanceOf(NotConnected);
    const link = new MemoryTransport();
    link.failSends = true;
    channel.attach(link);
    await expect(channel.call("session.create", {})).rejects.toThrow("pipe gone");
    expect(channel.pendingCount).toBe(0);
  });

  it("turns error frames into GatewayRpcError named from the table", async () => {
    const link = new MemoryTransport();
    const channel = new RpcChannel();
    channel.attach(link);
    const outcome = caught<GatewayRpcError>(channel.call("session.resume", { session_id: "nope" }));
    link.feed({
      id: 1,
      error: { code: 2001, message: "no such session", data: { name: "SESSION_NOT_FOUND" } },
    });
    const error = await outcome;
    expect(error).toBeInstanceOf(GatewayRpcError);
    expect(error.code).toBe(2001);
    expect(error.errorName).toBe("SESSION_NOT_FOUND");
    expect(error.known).toBe(true);
    expect(error.is("SESSION_NOT_FOUND")).toBe(true);
    expect(error.data).toEqual({ name: "SESSION_NOT_FOUND" });
  });

  it("flags an error code the generated table does not know", async () => {
    const flagged: Array<[number, string]> = [];
    const link = new MemoryTransport();
    const channel = new RpcChannel({
      onUnknownErrorCode: (error, method) => flagged.push([error.code, method]),
    });
    channel.attach(link);
    const outcome = caught<GatewayRpcError>(channel.call("prompt.submit", {}));
    link.feed({ id: 1, error: { code: 4999, message: "made up" } });
    const error = await outcome;
    expect(error.known).toBe(false);
    expect(error.errorName).toBeUndefined();
    expect(flagged).toEqual([[4999, "prompt.submit"]]);
  });
});

describe("RpcChannel transports", () => {
  it("rejects every call of a replaced transport and ignores its later close and frames", async () => {
    const closes: string[] = [];
    const lines: string[] = [];
    const notes: string[] = [];
    const old = new MemoryTransport();
    const channel = new RpcChannel({
      onClose: (info) => closes.push(info.reason),
      onDiagnostic: (line) => lines.push(line),
      onNotification: (method) => notes.push(method),
    });
    channel.attach(old);
    const calls = [channel.call("a"), channel.call("b"), channel.call("c")].map((call) =>
      caught(call),
    );
    const fresh = new MemoryTransport();
    channel.attach(fresh);
    for (const outcome of await Promise.all(calls))
      expect(outcome).toBeInstanceOf(TransportReplaced);
    old.fireClose("exit");
    old.feed({ method: "notice", params: { type: "notice" } });
    expect(closes).toEqual([]);
    expect(notes).toEqual([]);
    expect(channel.connected).toBe(true);
    const next = channel.call("d");
    expect(fresh.last()).toMatchObject({ id: 4, method: "d" });
    fresh.feed({ id: 4, result: 1 });
    await expect(next).resolves.toBe(1);
  });

  it("rejects pending calls when the current transport closes", async () => {
    const closes: string[] = [];
    const link = new MemoryTransport();
    const channel = new RpcChannel({ onClose: (info) => closes.push(info.reason) });
    channel.attach(link);
    const outcome = caught(channel.call("prompt.submit", {}));
    link.fireClose("exit");
    expect(await outcome).toBeInstanceOf(TransportClosed);
    expect(closes).toEqual(["exit"]);
    expect(channel.connected).toBe(false);
  });

  it("passes events on as notifications", () => {
    const seen: Array<[string, unknown]> = [];
    const link = new MemoryTransport();
    const channel = new RpcChannel({
      onNotification: (method, params) => seen.push([method, params]),
    });
    channel.attach(link);
    link.feed({ method: "message.delta", params: { type: "message.delta", text: "hi" } });
    expect(seen).toEqual([["message.delta", { type: "message.delta", text: "hi" }]]);
  });
});

describe("RpcChannel server requests (id-bearing frames)", () => {
  it("answers -32601 at once when no handler exists, with the server's string id", async () => {
    const link = new MemoryTransport();
    const channel = new RpcChannel();
    channel.attach(link);
    channel.setHandlers({});
    link.feed({ id: "srq-00000000000a", method: "window.read", params: {} });
    await tick();
    expect(link.last()).toEqual({
      jsonrpc: "2.0",
      id: "srq-00000000000a",
      error: { code: -32601, message: "not handled here: window.read" },
    });
  });

  it("answers -32603 when the handler throws or rejects", async () => {
    const link = new MemoryTransport();
    const channel = new RpcChannel();
    channel.attach(link);
    channel.setHandlers({
      rpc: {
        "boom.sync": () => {
          throw new Error("bad");
        },
        "boom.async": async () => {
          throw new Error("worse");
        },
      },
    });
    link.feed({ id: "srq-000000000001", method: "boom.sync" });
    link.feed({ id: "srq-000000000002", method: "boom.async" });
    await tick();
    await tick();
    const codes = link.sent.map((frame) => [frame.id, (frame.error as { code: number }).code]);
    expect(codes).toEqual([
      ["srq-000000000001", -32603],
      ["srq-000000000002", -32603],
    ]);
  });

  it("sends the handler's result once; a second delivery of the same id is dropped", async () => {
    const link = new MemoryTransport();
    const channel = new RpcChannel();
    channel.attach(link);
    let calls = 0;
    channel.setHandlers({
      rpc: {
        "ui.ask": () => {
          calls += 1;
          return { ok: true };
        },
      },
    });
    link.feed({ id: "srq-000000000003", method: "ui.ask" });
    link.feed({ id: "srq-000000000003", method: "ui.ask" });
    await tick();
    expect(calls).toBe(1);
    expect(link.sent).toEqual([{ jsonrpc: "2.0", id: "srq-000000000003", result: { ok: true } }]);
  });

  it("never confuses a server string id with a client integer id", async () => {
    const link = new MemoryTransport();
    const channel = new RpcChannel();
    channel.attach(link);
    channel.setHandlers({ rpc: { "ui.ask": () => "server got its answer" } });
    const mine = channel.call("x");
    // The server asks with id "1": a request frame, so it can never settle our call 1.
    link.feed({ id: "1", method: "ui.ask" });
    await tick();
    expect(channel.pendingCount).toBe(1);
    expect(link.last()).toEqual({ jsonrpc: "2.0", id: "1", result: "server got its answer" });
    link.feed({ id: 1, result: "mine" });
    await expect(mine).resolves.toBe("mine");
  });
});

describe("RpcChannel server requests (our server_request wire)", () => {
  it("answers through server_request.answer with the handler's answer", async () => {
    const link = new MemoryTransport();
    const channel = new RpcChannel();
    channel.attach(link);
    channel.setHandlers({
      serverRequests: {
        approval: (request) => ({ kind: "approval", choice: request.choices?.[0] ?? "deny" }),
      },
    });
    link.feed({ method: "server_request", params: { ...approval("r1") } });
    await tick();
    expect(link.last()).toEqual({
      jsonrpc: "2.0",
      id: 1,
      method: "server_request.answer",
      params: { session_id: "s1", request_id: "r1", answer: { kind: "approval", choice: "once" } },
    });
  });

  it("sends answers with their own deadline when one is set (answerTimeoutMs)", async () => {
    vi.useFakeTimers();
    try {
      const link = new MemoryTransport();
      const diagnostics: string[] = [];
      const channel = new RpcChannel({
        timeoutMs: 1_000,
        answerTimeoutMs: 5_000,
        onDiagnostic: (line) => diagnostics.push(line),
      });
      channel.attach(link);
      channel.setHandlers({
        serverRequests: { approval: () => ({ kind: "approval", choice: "once" }) },
      });
      link.feed({ method: "server_request", params: { ...approval("r9") } });
      await tick();
      await vi.advanceTimersByTimeAsync(4_999);
      expect(channel.pendingCount).toBe(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(channel.pendingCount).toBe(0);
      expect(diagnostics.some((line) => line.includes("answer for r9 not delivered"))).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("declines a kind it cannot answer, and a crashing handler, without waiting", async () => {
    const link = new MemoryTransport();
    const channel = new RpcChannel();
    channel.attach(link);
    channel.setHandlers({
      serverRequests: {
        sudo: () => {
          throw new Error("no prompt here");
        },
      },
    });
    link.feed({ method: "server_request", params: { ...approval("r2") } });
    link.feed({
      method: "server_request",
      params: { kind: "sudo", request_id: "r3", session_id: "s1", reason: "x" },
    });
    await tick();
    const answers = link.sent.map((frame) => (frame.params as { answer: unknown }).answer);
    expect(answers).toEqual([
      { kind: "approval", choice: "deny" },
      { kind: "sudo", password: null },
    ]);
  });

  it("lists the kinds its handlers can answer", () => {
    const channel = new RpcChannel();
    expect(channel.answerableKinds()).toEqual([]);
    channel.setHandlers({
      serverRequests: {
        sudo: () => ({ kind: "sudo", password: null }),
        approval: () => ({ kind: "approval", choice: "deny" }),
      },
    });
    expect(channel.answerableKinds()).toEqual(["approval", "sudo"]);
  });

  it("builds a decline answer for every kind", () => {
    expect(
      declineAnswer({
        kind: "clarify",
        request_id: "c",
        session_id: "s",
        questions: [
          { id: "q1", text: "?" },
          { id: "q2", text: "??" },
        ],
      }),
    ).toEqual({ kind: "clarify", answers: { q1: [], q2: [] } });
    expect(
      declineAnswer({ kind: "secret", request_id: "k", session_id: "s", name: "N", prompt: "p" }),
    ).toEqual({
      kind: "secret",
      value: null,
    });
    expect(
      declineAnswer({ kind: "preview.read", request_id: "p", session_id: "s", what: "snapshot" }),
    ).toEqual({ kind: "preview.read", ok: false, error: "not shown here", result: {} });
  });

  it("first answer wins when a handler resolves after the request was already answered", async () => {
    const link = new MemoryTransport();
    const channel = new RpcChannel();
    channel.attach(link);
    let release: (value: { kind: "approval"; choice: "once" }) => void = () => undefined;
    channel.setHandlers({
      serverRequests: { approval: () => new Promise((resolve) => (release = resolve)) },
    });
    link.feed({ method: "server_request", params: { ...approval("r4") } });
    // A replay of the same request (resume) must not open a second card.
    channel.redeliver([approval("r4")]);
    release({ kind: "approval", choice: "once" });
    await tick();
    release({ kind: "approval", choice: "once" });
    await tick();
    expect(link.sent.filter((frame) => frame.method === "server_request.answer")).toHaveLength(1);
  });

  it("aborts the request's signal when the server withdraws it", async () => {
    const link = new MemoryTransport();
    const channel = new RpcChannel();
    channel.attach(link);
    let signal: AbortSignal | undefined;
    channel.setHandlers({
      serverRequests: {
        approval: (_request, meta) => {
          signal = meta.signal;
          return new Promise(() => undefined);
        },
      },
    });
    link.feed({ method: "server_request", params: { ...approval("r5") } });
    expect(signal?.aborted).toBe(false);
    link.feed({
      method: "server_request.cancelled",
      params: { type: "server_request.cancelled", request_id: "r5", reason: "interrupted" },
    });
    expect(signal?.aborted).toBe(true);
  });

  it("marks requests re-delivered from a resume as replayed", () => {
    const link = new MemoryTransport();
    const channel = new RpcChannel();
    channel.attach(link);
    const replayed: boolean[] = [];
    channel.setHandlers({
      serverRequests: {
        approval: (_request, meta) => {
          replayed.push(meta.replayed);
          return new Promise(() => undefined);
        },
      },
    });
    link.feed({ method: "server_request", params: { ...approval("live") } });
    channel.redeliver([approval("again")]);
    expect(replayed).toEqual([false, true]);
  });
});
