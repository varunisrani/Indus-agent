// JSON-RPC 2.0 peer over any Transport. Browser-safe: no Node-only API in this file.

import {
  ERROR_CODES,
  SERVER_REQUEST_KINDS,
  SERVER_REQUEST_METHOD,
  type ServerRequest,
  type ServerRequestAnswer,
  type ServerRequestKind,
  type ServerRequestTypes,
} from "@indus/protocol";
import { type Clock, systemClock, type TimerHandle } from "./clock.ts";
import {
  GatewayRpcError,
  NotConnected,
  RequestAborted,
  RpcTimeout,
  rpcErrorFromWire,
  TransportClosed,
  TransportReplaced,
} from "./errors.ts";
import { ANSWERED_ID_MEMORY, DEFAULT_RPC_TIMEOUT_MS, HELD_REQUEST_CAPACITY } from "./limits.ts";

export type Unsubscribe = () => void;
export type RequestId = number | string;

export interface CloseInfo {
  /** Short reason for logs ("exit", "heartbeat", "closed by client", ...). */
  reason: string;
  /** The WebSocket close code, when the link was a WebSocket. */
  code?: number;
  /** No retry can help (a forbidden peer, a terminal attach verdict). */
  terminal?: boolean;
}

/** Frames as objects; how they travel (NDJSON, WebSocket text, in-memory) is the transport's job. */
export interface Transport {
  send(frame: OutboundFrame): void;
  onFrame(listener: (frame: unknown) => void): Unsubscribe;
  onClose(listener: (info: CloseInfo) => void): Unsubscribe;
  close(): void | Promise<void>;
}

export type OutboundFrame =
  | { jsonrpc: "2.0"; id: RequestId; method: string; params?: object }
  | { jsonrpc: "2.0"; method: string; params?: object }
  | { jsonrpc: "2.0"; id: RequestId; result: unknown }
  | {
      jsonrpc: "2.0";
      id: RequestId | null;
      error: { code: number; message: string; data?: unknown };
    };

export interface CallOptions {
  /** Overrides the channel default; 0 or less waits forever. */
  timeoutMs?: number;
  signal?: AbortSignal;
}

/** A question from the server, whichever way it arrived. */
export interface IncomingRequest {
  /** The JSON-RPC id (`srq-…`) or the server request's `request_id`. */
  readonly id: string;
  /** The JSON-RPC method, or the server request's `kind`. */
  readonly method: string;
  readonly params: unknown;
  /** True when re-delivered from a resume (`pending_requests`) rather than live. */
  readonly replayed: boolean;
  /** `rpc`: an id-bearing request frame; `server_request`: our P04 notification wire. */
  readonly via: "rpc" | "server_request";
  /** Aborts when the server withdraws the request (`server_request.cancelled`, `request.settled`). */
  readonly signal: AbortSignal;
  /** The generation of the turn that asked, when the daemon named it (sent back with the answer). */
  readonly generation?: number;
}

export type RpcRequestHandler = (request: IncomingRequest) => unknown;

export type ServerRequestHandlers = {
  [K in ServerRequestKind]?: (
    request: ServerRequestTypes[K]["request"],
    meta: IncomingRequest,
  ) => ServerRequestTypes[K]["answer"] | Promise<ServerRequestTypes[K]["answer"]>;
};

export interface HandlerTable {
  /** Handlers for id-bearing server→client request frames, by method. */
  rpc?: Readonly<Record<string, RpcRequestHandler>>;
  /** Handlers for `server_request` notifications, by kind. */
  serverRequests?: ServerRequestHandlers;
}

export interface ChannelOptions {
  clock?: Clock;
  /** Default call timeout (already resolved from env by the host). */
  timeoutMs?: number;
  /** Deadline for sending a server-request answer or decline (default: `timeoutMs`). */
  answerTimeoutMs?: number;
  /** Every inbound notification that is not a server request (events). */
  onNotification?: (method: string, params: unknown) => void;
  /** Called for every inbound frame of the current transport (liveness). */
  onInbound?: () => void;
  /** The current transport closed (not a replacement). */
  onClose?: (info: CloseInfo) => void;
  /** An error frame carried a code that the generated table does not know. */
  onUnknownErrorCode?: (error: GatewayRpcError, method: string) => void;
  /** Diagnostic lines for the log ring. */
  onDiagnostic?: (line: string) => void;
}

const JSONRPC = "2.0";
const ANSWER_METHOD = "server_request.answer";
const DECLINE_METHOD = "server_request.decline";
const CANCELLED_EVENT = "server_request.cancelled";
/** The daemon's settlement event (P17-07): answered elsewhere, withdrawn or undeliverable. */
const SETTLED_EVENT = "request.settled";
/** The capability a daemon advertises when it counts a viewer's "not shown here" decline. */
export const DECLINE_CAPABILITY = "request.decline";

interface Pending {
  method: string;
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  timer: TimerHandle | undefined;
  cleanup: () => void;
}

interface Held {
  request: IncomingRequest;
  controller: AbortController;
  payload: ServerRequest | undefined;
  transport: Transport | null;
  /**
   * `bridge`: the stdio bridge's bare server request (a refusal is the kind's decline answer);
   * `daemon`: the daemon's `{request, replayed, generation}` offer (a refusal is a `-32601` error
   * frame under the request's id, so the daemon offers it elsewhere at once, R05.3).
   */
  wire: "bridge" | "daemon";
}

const isGeneration = (value: unknown): value is number =>
  typeof value === "number" && Number.isInteger(value) && value >= 0;

/** The answer a kind gets when nothing here can answer it: the agent goes on at once. */
export function declineAnswer(request: ServerRequest): ServerRequestAnswer {
  switch (request.kind) {
    case "approval":
      return { kind: "approval", choice: "deny" };
    case "clarify":
      return {
        kind: "clarify",
        answers: Object.fromEntries(request.questions.map((question) => [question.id, []])),
      };
    case "sudo":
      return { kind: "sudo", password: null };
    case "secret":
      return { kind: "secret", value: null };
    case "preview.act":
    case "preview.read":
      // A desktop window's preview requests (P24-25): nothing here shows that preview.
      return { kind: request.kind, ok: false, error: "not shown here", result: {} };
  }
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const isId = (value: unknown): value is RequestId =>
  typeof value === "string" || (typeof value === "number" && Number.isInteger(value));

const errorText = (error: unknown): string =>
  error instanceof Error ? error.message : String(error);

export class RpcChannel {
  private readonly clock: Clock;
  private readonly defaultTimeoutMs: number;
  private readonly options: ChannelOptions;
  private transport: Transport | null = null;
  private unbind: Unsubscribe[] = [];
  private nextId = 0;
  private readonly pending = new Map<RequestId, Pending>();
  private handlers: HandlerTable | null = null;
  private held: Held[] = [];
  /** Server requests delivered and not yet answered, by id. */
  private readonly open = new Map<string, AbortController>();
  /** Recently answered ids, oldest first, to drop a repeat delivery or answer. */
  private readonly answered = new Set<string>();
  /** The id as it came on the wire (number or string) for id-bearing requests. */
  private readonly wireIds = new Map<string, RequestId>();
  /** Open server requests: the generation of the turn that asked, when known. */
  private readonly generations = new Map<string, number>();
  /** Open server requests by id, for `decline`. */
  private readonly entries = new Map<string, Held>();
  /** What the server advertised in its ready frame (`request.decline`, ...). */
  private serverCapabilities: ReadonlySet<string> = new Set();

  constructor(options: ChannelOptions = {}) {
    this.options = options;
    this.clock = options.clock ?? systemClock;
    this.defaultTimeoutMs = options.timeoutMs ?? DEFAULT_RPC_TIMEOUT_MS;
  }

  get connected(): boolean {
    return this.transport !== null;
  }

  get pendingCount(): number {
    return this.pending.size;
  }

  /** Bind a transport. A previous one is dropped: its calls fail with `TransportReplaced`. */
  attach(transport: Transport): void {
    if (this.transport === transport) return;
    if (this.transport !== null) this.release(new TransportReplaced(), "replaced");
    this.transport = transport;
    this.unbind = [
      transport.onFrame((frame) => {
        if (this.transport !== transport) {
          this.diagnose("frame from a replaced connection ignored");
          return;
        }
        this.options.onInbound?.();
        this.receive(frame);
      }),
      transport.onClose((info) => {
        if (this.transport !== transport) {
          this.diagnose(`close of a replaced connection ignored (${info.reason})`);
          return;
        }
        this.release(new TransportClosed(info.reason), "closed");
        this.options.onClose?.(info);
      }),
    ];
  }

  /** Drop the current transport without closing it; its calls fail with `error`. */
  detach(error: Error = new TransportClosed("detached")): void {
    if (this.transport !== null) this.release(error, "detached");
  }

  /** The server's advertised capabilities (from its ready frame), replacing earlier ones. */
  setServerCapabilities(capabilities: readonly string[]): void {
    this.serverCapabilities = new Set(capabilities);
  }

  /** The generation the server named for an open request, if any. */
  generationOf(requestId: string): number | undefined {
    return this.generations.get(requestId);
  }

  /**
   * "Not shown here" for an open server request. Sent as `server_request.decline` only when the
   * server advertised `request.decline`; otherwise the refusal every server understands (a
   * `-32601` error frame on the daemon's wire, the kind's decline answer on the bridge's). The
   * first respond / fail wins: false when the request was already answered or is unknown.
   */
  decline(requestId: string): boolean {
    const entry = this.entries.get(requestId);
    if (entry === undefined || this.answered.has(requestId)) return false;
    if (entry.request.via === "server_request" && this.serverCapabilities.has(DECLINE_CAPABILITY)) {
      if (!this.finish(requestId)) return false;
      const payload = entry.payload as ServerRequest;
      this.call(
        DECLINE_METHOD,
        { session_id: payload.session_id, request_id: requestId },
        this.answerOptions(),
      ).catch((error: unknown) => this.undelivered(requestId, error, "decline"));
      entry.controller.abort();
      return true;
    }
    this.refuse(entry, "declined here");
    entry.controller.abort();
    return true;
  }

  /** Install the server-request handlers; requests held until now are delivered in order. */
  setHandlers(table: HandlerTable): void {
    this.handlers = table;
    const held = this.held;
    this.held = [];
    for (const entry of held) this.dispatch(entry);
  }

  /**
   * Kinds the installed handlers can answer, for the capability declaration the server needs
   * before it sends any (R05.3). The wire feature names are not in the protocol yet.
   */
  answerableKinds(): ServerRequestKind[] {
    const table = this.handlers?.serverRequests ?? {};
    return (Object.keys(SERVER_REQUEST_KINDS) as ServerRequestKind[]).filter(
      (kind) => table[kind] !== undefined,
    );
  }

  /** Call a method and wait for its result. */
  call(method: string, params?: object, options: CallOptions = {}): Promise<unknown> {
    const transport = this.transport;
    if (transport === null) return Promise.reject(new NotConnected());
    const signal = options.signal;
    if (signal?.aborted) return Promise.reject(new RequestAborted(method));
    this.nextId += 1;
    const id = this.nextId;
    const timeoutMs = options.timeoutMs ?? this.defaultTimeoutMs;

    return new Promise<unknown>((resolve, reject) => {
      const onAbort = (): void => {
        if (this.settle(id)) reject(new RequestAborted(method));
      };
      const entry: Pending = {
        method,
        resolve,
        reject,
        timer: undefined,
        cleanup: () => signal?.removeEventListener("abort", onAbort),
      };
      if (timeoutMs > 0) {
        entry.timer = this.clock.setTimeout(() => {
          if (this.settle(id)) reject(new RpcTimeout(method, timeoutMs));
        }, timeoutMs);
      }
      signal?.addEventListener("abort", onAbort, { once: true });
      this.pending.set(id, entry);
      try {
        const frame: OutboundFrame =
          params === undefined
            ? { jsonrpc: JSONRPC, id, method }
            : { jsonrpc: JSONRPC, id, method, params };
        transport.send(frame);
      } catch (error) {
        if (this.settle(id)) reject(error instanceof Error ? error : new Error(String(error)));
      }
    });
  }

  /** Send a notification (no answer expected). */
  notify(method: string, params?: object): void {
    const transport = this.transport;
    if (transport === null) throw new NotConnected();
    transport.send(
      params === undefined ? { jsonrpc: JSONRPC, method } : { jsonrpc: JSONRPC, method, params },
    );
  }

  /** Hand server requests from `session.resume` (`pending_requests`) to the handlers again. */
  redeliver(requests: readonly ServerRequest[]): void {
    for (const payload of requests) this.acceptServerRequest(payload, true);
  }

  /** Route one inbound frame (exposed for transports that are not bound with `attach`). */
  receive(frame: unknown): void {
    if (!isObject(frame) || frame.jsonrpc !== JSONRPC) {
      this.diagnose("frame without jsonrpc 2.0 ignored");
      return;
    }
    if (typeof frame.method === "string") {
      if (frame.id !== undefined) {
        this.acceptRpcRequest(frame.id, frame.method, frame.params);
      } else if (frame.method === SERVER_REQUEST_METHOD) {
        if (isObject(frame.params)) this.acceptOffer(frame.params);
        else this.diagnose("server request without params ignored");
      } else {
        if (
          (frame.method === CANCELLED_EVENT || frame.method === SETTLED_EVENT) &&
          isObject(frame.params)
        ) {
          this.withdraw(String(frame.params.request_id));
        }
        this.options.onNotification?.(frame.method, frame.params);
      }
      return;
    }
    if (isId(frame.id) && ("result" in frame || "error" in frame)) {
      this.acceptResponse(frame.id, frame);
      return;
    }
    if ("error" in frame) {
      this.diagnose(`error frame without a usable id: ${JSON.stringify(frame.error)}`);
      return;
    }
    this.diagnose("frame that is neither request, notification nor response ignored");
  }

  private acceptResponse(id: RequestId, frame: Record<string, unknown>): void {
    const entry = this.pending.get(id);
    if (entry === undefined) {
      this.diagnose(`answer for id ${String(id)} dropped (late, duplicate or unknown)`);
      return;
    }
    this.settle(id);
    if ("error" in frame) {
      const error = rpcErrorFromWire(frame.error);
      if (!error.known) this.options.onUnknownErrorCode?.(error, entry.method);
      entry.reject(error);
    } else {
      entry.resolve(frame.result);
    }
  }

  private acceptRpcRequest(rawId: unknown, method: string, params: unknown): void {
    if (!isId(rawId)) {
      this.diagnose(`server request ${method} with an unusable id ignored`);
      return;
    }
    const controller = new AbortController();
    const request: IncomingRequest = {
      id: String(rawId),
      method,
      params,
      replayed: false,
      via: "rpc",
      signal: controller.signal,
    };
    // Keep the original id type for the answer frame.
    this.track(
      { request, controller, payload: undefined, transport: this.transport, wire: "bridge" },
      rawId,
    );
  }

  /** A `server_request` notification: the bridge's bare request or the daemon's offer. */
  private acceptOffer(params: Record<string, unknown>): void {
    if (isObject(params.request)) {
      this.acceptServerRequest(
        params.request as unknown as ServerRequest,
        params.replayed === true,
        "daemon",
        isGeneration(params.generation) ? params.generation : undefined,
      );
      return;
    }
    this.acceptServerRequest(params as unknown as ServerRequest, false, "bridge", undefined);
  }

  private acceptServerRequest(
    payload: ServerRequest,
    replayed: boolean,
    wire: "bridge" | "daemon" = "bridge",
    generation: number | undefined = undefined,
  ): void {
    if (typeof payload.request_id !== "string" || typeof payload.kind !== "string") {
      this.diagnose("server request without request_id or kind ignored");
      return;
    }
    const controller = new AbortController();
    const request: IncomingRequest = {
      id: payload.request_id,
      method: payload.kind,
      params: payload,
      replayed,
      via: "server_request",
      signal: controller.signal,
      ...(generation === undefined ? {} : { generation }),
    };
    if (generation !== undefined && this.open.has(payload.request_id)) {
      // A re-offer names the generation a bare resume copy lacked.
      this.generations.set(payload.request_id, generation);
    }
    this.track(
      { request, controller, payload, transport: this.transport, wire },
      payload.request_id,
    );
  }

  private track(entry: Held, wireId: RequestId): void {
    const id = entry.request.id;
    if (this.open.has(id) || this.answered.has(id)) {
      this.diagnose(`server request ${id} delivered again; first delivery kept`);
      return;
    }
    this.open.set(id, entry.controller);
    this.entries.set(id, entry);
    if (entry.request.generation !== undefined) this.generations.set(id, entry.request.generation);
    this.wireIds.set(id, wireId);
    if (this.handlers === null) {
      this.held.push(entry);
      if (this.held.length > HELD_REQUEST_CAPACITY) {
        const oldest = this.held.shift();
        if (oldest) this.refuse(oldest, "held too long before handlers were installed");
      }
      return;
    }
    this.dispatch(entry);
  }

  private dispatch(entry: Held): void {
    const { request } = entry;
    const handler =
      request.via === "rpc"
        ? this.handlers?.rpc?.[request.method]
        : (this.handlers?.serverRequests?.[request.method as ServerRequestKind] as
            | ((payload: ServerRequest, meta: IncomingRequest) => unknown)
            | undefined);
    if (handler === undefined) {
      this.refuse(entry, `no handler for ${request.method}`);
      return;
    }
    let outcome: unknown;
    try {
      outcome =
        request.via === "rpc"
          ? (handler as RpcRequestHandler)(request)
          : (handler as (payload: ServerRequest, meta: IncomingRequest) => unknown)(
              entry.payload as ServerRequest,
              request,
            );
    } catch (error) {
      this.crash(entry, error);
      return;
    }
    if (outcome instanceof Promise) {
      outcome.then(
        (value) => this.answer(entry, value),
        (error: unknown) => this.crash(entry, error),
      );
    } else {
      this.answer(entry, outcome);
    }
  }

  /** Nothing here answers this request: tell the server at once (R05.3). */
  private refuse(entry: Held, why: string): void {
    this.diagnose(`server request ${entry.request.method} refused: ${why}`);
    if (entry.request.via === "rpc" || entry.wire === "daemon") {
      this.sendError(
        entry,
        ERROR_CODES.METHOD_NOT_FOUND.code,
        `not handled here: ${entry.request.method}`,
      );
    } else if (entry.payload && entry.payload.kind in SERVER_REQUEST_KINDS) {
      this.answer(entry, declineAnswer(entry.payload));
    } else {
      this.finish(entry.request.id);
    }
  }

  private crash(entry: Held, error: unknown): void {
    this.diagnose(`server request handler for ${entry.request.method} failed: ${errorText(error)}`);
    if (entry.request.via === "rpc" || entry.wire === "daemon") {
      this.sendError(
        entry,
        ERROR_CODES.INTERNAL_ERROR.code,
        `handler failed: ${entry.request.method}`,
      );
    } else if (entry.payload && entry.payload.kind in SERVER_REQUEST_KINDS) {
      this.answer(entry, declineAnswer(entry.payload));
    } else {
      this.finish(entry.request.id);
    }
  }

  private answer(entry: Held, value: unknown): void {
    const { request } = entry;
    const generation = this.generations.get(request.id) ?? request.generation;
    if (!this.finish(request.id)) {
      this.diagnose(`second answer for ${request.id} dropped`);
      return;
    }
    if (request.via === "rpc") {
      this.sendOn(entry, {
        jsonrpc: JSONRPC,
        id: this.wireIdOf(request.id),
        result: value ?? null,
      });
      return;
    }
    const payload = entry.payload as ServerRequest;
    this.call(
      ANSWER_METHOD,
      {
        session_id: payload.session_id,
        request_id: payload.request_id,
        answer: value,
        ...(generation === undefined ? {} : { generation }),
      },
      this.answerOptions(),
    ).catch((error: unknown) => this.undelivered(request.id, error, "answer"));
  }

  /**
   * An answer that never reached the server (no link, or the link went while it waited) does
   * not count: forget it so the server's re-offer after a reconnect is shown again. An error
   * frame from the server means it did arrive.
   */
  private undelivered(id: string, error: unknown, what: string): void {
    this.diagnose(`${what} for ${id} not delivered: ${errorText(error)}`);
    if (error instanceof GatewayRpcError) return;
    this.answered.delete(id);
  }

  private sendError(entry: Held, code: number, message: string): void {
    if (!this.finish(entry.request.id)) return;
    if (entry.request.via === "server_request") {
      // The daemon reads an error frame under the request's id as this viewer's refusal.
      this.sendOn(entry, { jsonrpc: JSONRPC, id: entry.request.id, error: { code, message } });
      return;
    }
    this.sendOn(entry, {
      jsonrpc: JSONRPC,
      id: this.wireIdOf(entry.request.id),
      error: { code, message },
    });
  }

  /** Answers to id-bearing requests belong to the connection that asked. */
  private sendOn(entry: Held, frame: OutboundFrame): void {
    if (entry.transport === null || entry.transport !== this.transport) {
      this.diagnose(`answer for ${entry.request.id} dropped: its connection is gone`);
      this.answered.delete(entry.request.id);
      return;
    }
    try {
      entry.transport.send(frame);
    } catch (error) {
      this.diagnose(`answer for ${entry.request.id} not sent: ${errorText(error)}`);
    }
  }

  private wireIdOf(id: string): RequestId {
    const wire = this.wireIds.get(id) ?? id;
    this.wireIds.delete(id);
    return wire;
  }

  /** Mark a request answered. False when it was already answered. */
  private finish(id: string): boolean {
    if (this.answered.has(id)) return false;
    this.open.delete(id);
    this.entries.delete(id);
    this.remember(id);
    return true;
  }

  private remember(id: string): void {
    this.answered.add(id);
    this.generations.delete(id);
    if (this.answered.size > ANSWERED_ID_MEMORY) {
      const oldest = this.answered.values().next().value;
      if (oldest !== undefined) this.answered.delete(oldest);
    }
  }

  private withdraw(id: string): void {
    const controller = this.open.get(id);
    if (controller === undefined) return;
    this.open.delete(id);
    this.entries.delete(id);
    this.remember(id);
    this.held = this.held.filter((entry) => entry.request.id !== id);
    controller.abort();
  }

  private settle(id: RequestId): boolean {
    const entry = this.pending.get(id);
    if (entry === undefined) return false;
    this.pending.delete(id);
    if (entry.timer !== undefined) this.clock.clearTimeout(entry.timer);
    entry.cleanup();
    return true;
  }

  private release(error: Error, why: string): void {
    for (const unsubscribe of this.unbind) unsubscribe();
    this.unbind = [];
    this.transport = null;
    for (const [id, entry] of [...this.pending]) {
      this.settle(id);
      entry.reject(error);
    }
    // Requests still held belong to the old connection; a resume re-delivers the open ones.
    for (const entry of this.held) {
      this.open.delete(entry.request.id);
      this.entries.delete(entry.request.id);
    }
    this.held = [];
    this.diagnose(`connection ${why}`);
  }

  private answerOptions(): CallOptions {
    return this.options.answerTimeoutMs === undefined
      ? {}
      : { timeoutMs: this.options.answerTimeoutMs };
  }

  private diagnose(line: string): void {
    this.options.onDiagnostic?.(`[rpc] ${line}`);
  }
}
