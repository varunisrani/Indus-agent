// A scriptable in-process peer for tests: answers calls, emits events, asks server questions,
// dies and stalls on cue. No Python, no child process. Every delivery hops one microtask, like a
// real transport.

import {
  ERROR_CODES,
  SERVER_REQUEST_METHOD,
  type ServerRequest,
  type ServerRequestAnswer,
} from "@indus/protocol";
import type { CloseInfo, OutboundFrame, RequestId, Transport, Unsubscribe } from "../channel.ts";
import { PING_METHOD, READY_METHOD } from "../localEvents.ts";

type Frame = Record<string, unknown>;
export type MethodHandler = (params: Record<string, unknown>, frame: Frame) => unknown;

/** Throw this from a handler to answer with an error frame. */
export class FakeRpcError extends Error {
  readonly code: number;
  readonly data: unknown;

  constructor(code: number, message: string, data?: unknown) {
    super(message);
    this.code = code;
    this.data = data;
  }
}

interface Link extends Transport {
  readonly alive: boolean;
  deliver(frame: Frame): void;
  kill(reason: string): void;
}

export class FakeServer {
  /** Every frame the client sent, in order. */
  readonly received: Frame[] = [];
  private readonly methods = new Map<string, MethodHandler>();
  private readonly answers = new Map<string, (answer: ServerRequestAnswer) => void>();
  private readonly responses = new Map<RequestId, (frame: Frame) => void>();
  private link: Link | null = null;
  private stalled = false;
  private seq = 0;
  private serverIds = 0;
  /** Transports handed out so far (one per `connect()`). */
  connections = 0;

  constructor() {
    this.methods.set(PING_METHOD, () => ({}));
    this.methods.set("server_request.answer", (params) => {
      const id = String(params.request_id);
      const settle = this.answers.get(id);
      if (settle === undefined) {
        throw new FakeRpcError(ERROR_CODES.SERVER_REQUEST_NOT_FOUND.code, `no open request ${id}`);
      }
      this.answers.delete(id);
      settle(params.answer as ServerRequestAnswer);
      return { accepted: true };
    });
  }

  /** A fresh transport for the client; the previous one stops working. */
  connect = (): Transport => {
    this.link?.kill("replaced by a new connection");
    this.connections += 1;
    this.stalled = false;
    this.link = this.makeLink();
    return this.link;
  };

  /** Answer `method` with whatever `handler` returns (or throws `FakeRpcError`). */
  handle(method: string, handler: MethodHandler): this {
    this.methods.set(method, handler);
    return this;
  }

  /** Send the ready notification (stand-in shape, see localEvents.ts). */
  ready(params: { protocol_version?: number; heartbeat?: boolean } = {}): void {
    this.push({ jsonrpc: "2.0", method: READY_METHOD, params: { type: READY_METHOD, ...params } });
  }

  /** Emit one event; `seq` and `session_id` are filled in when absent. */
  emit(event: { type: string } & Record<string, unknown>): void {
    this.seq += 1;
    const params = { session_id: "s-fake", seq: this.seq, ...event };
    this.push({ jsonrpc: "2.0", method: event.type, params });
  }

  /** Ask a server question over our P04 wire; resolves with the client's answer. */
  ask(request: ServerRequest): Promise<ServerRequestAnswer> {
    return new Promise((resolve) => {
      this.answers.set(request.request_id, resolve);
      this.push({ jsonrpc: "2.0", method: SERVER_REQUEST_METHOD, params: { ...request } });
    });
  }

  /** Withdraw an open question (the client sees `server_request.cancelled`). */
  cancel(requestId: string, reason = "interrupted"): void {
    this.answers.delete(requestId);
    this.emit({ type: "server_request.cancelled", request_id: requestId, reason });
  }

  /** A fresh server-minted id: `srq-` and 12 hex digits. */
  nextServerId(): string {
    this.serverIds += 1;
    return `srq-${this.serverIds.toString(16).padStart(12, "0")}`;
  }

  /** Send an id-bearing JSON-RPC request; resolves with the client's response frame. */
  request(method: string, params: object = {}): Promise<Frame> {
    const id = this.nextServerId();
    return new Promise((resolve) => {
      this.responses.set(id, resolve);
      this.push({ jsonrpc: "2.0", id, method, params });
    });
  }

  /** Put any frame (or garbage) on the wire as is. */
  sendRaw(frame: Frame): void {
    this.push(frame);
  }

  /** Drop the connection, as if the process died or the socket was cut. */
  die(reason = "exit"): void {
    this.link?.kill(reason);
    this.link = null;
  }

  /** Go silent: nothing is answered or sent until `wake()` (a hung peer). */
  stall(): void {
    this.stalled = true;
  }

  wake(): void {
    this.stalled = false;
  }

  get alive(): boolean {
    return this.link?.alive ?? false;
  }

  /** Frames the client sent for one method. */
  calls(method: string): Frame[] {
    return this.received.filter((frame) => frame.method === method);
  }

  private push(frame: Frame): void {
    if (this.stalled) return;
    this.link?.deliver(frame);
  }

  private onClientFrame(frame: Frame): void {
    this.received.push(frame);
    if (this.stalled) return;
    if (typeof frame.method !== "string") {
      const resolve = this.responses.get(frame.id as RequestId);
      if (resolve) {
        this.responses.delete(frame.id as RequestId);
        resolve(frame);
      }
      return;
    }
    if (frame.id === undefined) return;
    const id = frame.id as RequestId;
    const handler = this.methods.get(frame.method);
    if (handler === undefined) {
      const code = ERROR_CODES.METHOD_NOT_FOUND.code;
      this.push({ jsonrpc: "2.0", id, error: { code, message: `unknown method ${frame.method}` } });
      return;
    }
    const params = (frame.params ?? {}) as Record<string, unknown>;
    Promise.resolve()
      .then(() => handler(params, frame))
      .then(
        (result) => this.push({ jsonrpc: "2.0", id, result: result ?? null }),
        (error: unknown) => {
          const failure =
            error instanceof FakeRpcError
              ? { code: error.code, message: error.message, data: error.data }
              : { code: ERROR_CODES.INTERNAL_ERROR.code, message: String(error) };
          this.push({ jsonrpc: "2.0", id, error: failure });
        },
      );
  }

  private makeLink(): Link {
    const frames = new Set<(frame: unknown) => void>();
    const closes = new Set<(info: CloseInfo) => void>();
    let alive = true;
    const kill = (reason: string): void => {
      if (!alive) return;
      alive = false;
      queueMicrotask(() => {
        for (const listener of [...closes]) listener({ reason });
      });
    };
    return {
      get alive() {
        return alive;
      },
      send: (frame: OutboundFrame) => {
        if (!alive) throw new Error("fake connection is closed");
        const copy = JSON.parse(JSON.stringify(frame)) as Frame;
        queueMicrotask(() => {
          if (alive) this.onClientFrame(copy);
        });
      },
      onFrame: (listener): Unsubscribe => {
        frames.add(listener);
        return () => frames.delete(listener);
      },
      onClose: (listener): Unsubscribe => {
        closes.add(listener);
        return () => closes.delete(listener);
      },
      close: () => kill("closed by client"),
      deliver: (frame: Frame) => {
        if (!alive) return;
        const copy = JSON.parse(JSON.stringify(frame)) as Frame;
        queueMicrotask(() => {
          if (!alive) return;
          for (const listener of [...frames]) listener(copy);
        });
      },
      kill,
    };
  }
}
