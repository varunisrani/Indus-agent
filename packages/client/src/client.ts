// One connection to the agent: channel + event buffer + log ring + heartbeat + reconnect.
// Browser-safe; the transport factory decides what kind of link it is.

import type {
  MethodName,
  MethodTypes,
  SessionResumeParams,
  SessionResumeResult,
} from "@indus/protocol";
import { type BackoffOptions, Reconnector } from "./backoff.ts";
import {
  type CallOptions,
  type CloseInfo,
  type HandlerTable,
  RpcChannel,
  type Transport,
  type Unsubscribe,
} from "./channel.ts";
import { type Clock, systemClock } from "./clock.ts";
import type { GatewayRpcError } from "./errors.ts";
import { EventBuffer, type EventListener } from "./eventBuffer.ts";
import { Heartbeat } from "./heartbeat.ts";
import {
  type ClientEvent,
  PING_METHOD,
  READY_METHOD,
  type ReadyEvent,
  type TransportEvent,
} from "./localEvents.ts";
import { LogRing } from "./logRing.ts";

export interface ConnectHooks {
  logs: LogRing;
  /** Where the transport reports its synthetic `transport.*` events. */
  onEvent: (event: TransportEvent) => void;
}

export interface IndusClientOptions {
  /** Make a new transport; called by `start()` and by every reconnect. */
  connect: (hooks: ConnectHooks) => Transport;
  clock?: Clock;
  /** Default call timeout (resolve `INDUS_TUI_RPC_TIMEOUT_MS` with `rpcTimeoutFromEnv`). */
  timeoutMs?: number;
  /** Redial after an unexpected close; off by default (the TUI app owns child restarts). */
  reconnect?: false | BackoffOptions;
  heartbeat?: { intervalMs?: number; deadlineMs?: number; maxOutstanding?: number };
  logs?: LogRing;
  /** An error frame carried a code outside the generated table. */
  onUnknownErrorCode?: (error: GatewayRpcError, method: string) => void;
}

export class IndusClient {
  readonly logs: LogRing;
  readonly channel: RpcChannel;
  private readonly options: IndusClientOptions;
  private readonly events: EventBuffer<ClientEvent>;
  private readonly heartbeat: Heartbeat;
  private readonly reconnector: Reconnector | null;
  private transport: Transport | null = null;
  private disposed = false;
  private readyCount = 0;

  constructor(options: IndusClientOptions) {
    this.options = options;
    const clock = options.clock ?? systemClock;
    this.logs = options.logs ?? new LogRing();
    this.events = new EventBuffer<ClientEvent>({
      onListenerError: (error) =>
        this.logs.push(`[client] event listener failed: ${String(error)}`),
    });
    const channelOptions = {
      clock,
      onNotification: (method: string, params: unknown) => this.onNotification(method, params),
      onInbound: () => this.heartbeat.noteInbound(),
      onClose: (info: CloseInfo) => this.onTransportClosed(info),
      onDiagnostic: (line: string) => this.logs.push(line),
      ...(options.timeoutMs === undefined ? {} : { timeoutMs: options.timeoutMs }),
      ...(options.onUnknownErrorCode === undefined
        ? {}
        : { onUnknownErrorCode: options.onUnknownErrorCode }),
    };
    this.channel = new RpcChannel(channelOptions);
    this.heartbeat = new Heartbeat({
      clock,
      ...options.heartbeat,
      sendPing: () => this.channel.call(PING_METHOD, {}, { timeoutMs: 0 }),
      onDead: (why) => {
        this.logs.push(`[lifecycle] link silent (${why}); closing it`);
        void this.transport?.close();
      },
    });
    this.reconnector =
      options.reconnect === undefined || options.reconnect === false
        ? null
        : new Reconnector({
            ...options.reconnect,
            clock,
            reconnect: () => this.start(),
            onReconnecting: (event) => this.events.push(event),
          });
  }

  /** True once a ready event arrived on the current connection. */
  get ready(): boolean {
    return this.readyCount > 0;
  }

  get connected(): boolean {
    return this.channel.connected;
  }

  /** Open a connection (replacing any current one: its calls fail with `TransportReplaced`). */
  start(): void {
    if (this.disposed) {
      this.logs.push("[lifecycle] start ignored after close");
      return;
    }
    this.heartbeat.stop();
    this.readyCount = 0;
    const previous = this.transport;
    this.transport = this.options.connect({
      logs: this.logs,
      onEvent: (event) => this.events.push(event),
    });
    this.channel.attach(this.transport);
    if (previous !== null) void previous.close();
  }

  /** Listen to every event; the first listener also gets what arrived before it (R05.2). */
  subscribe(listener: EventListener<ClientEvent>): Unsubscribe {
    return this.events.subscribe(listener);
  }

  /** Install the answerers for server questions; early ones are answered now. */
  handle(table: HandlerTable): void {
    this.channel.setHandlers(table);
  }

  /** A typed call to one of the declared methods. */
  request<M extends MethodName>(
    method: M,
    params: MethodTypes[M]["params"],
    options?: CallOptions,
  ): Promise<MethodTypes[M]["result"]> {
    return this.channel.call(method, params, options) as Promise<MethodTypes[M]["result"]>;
  }

  /** Resume a session and hand its still-open server requests to the handlers again. */
  async resume(params: SessionResumeParams, options?: CallOptions): Promise<SessionResumeResult> {
    const result = await this.request("session.resume", params, options);
    this.channel.redeliver(result.pending_requests ?? []);
    return result;
  }

  /** Stop for good: no reconnect, heartbeat off, transport closed. */
  async close(): Promise<void> {
    this.disposed = true;
    this.reconnector?.cancel();
    this.heartbeat.stop();
    const transport = this.transport;
    this.transport = null;
    this.channel.detach();
    if (transport !== null) await transport.close();
  }

  private onNotification(method: string, params: unknown): void {
    const body = (typeof params === "object" && params !== null ? params : {}) as Record<
      string,
      unknown
    >;
    if (method === READY_METHOD) {
      const ready: ReadyEvent = { ...(body as Omit<ReadyEvent, "type">), type: READY_METHOD };
      this.readyCount += 1;
      this.reconnector?.markReady();
      if (ready.heartbeat === true) this.heartbeat.start();
      this.events.push(ready);
      return;
    }
    if (body.type !== method) {
      this.logs.push(`[protocol] notification ${method} without a matching type; passed on as is`);
    }
    this.events.push({ ...body, type: method } as ClientEvent);
  }

  private onTransportClosed(info: CloseInfo): void {
    this.heartbeat.stop();
    this.readyCount = 0;
    this.logs.push(`[lifecycle] connection closed (${info.reason})`);
    this.transport = null;
    if (!this.disposed) this.reconnector?.schedule();
  }
}
