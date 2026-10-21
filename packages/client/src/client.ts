// One connection to the agent: channel + event buffer + log ring + heartbeat + reconnect.
// Browser-safe; the transport factory decides what kind of link it is.

import type {
  ClientCapabilitiesParams,
  EventsSinceResult,
  MethodName,
  MethodTypes,
  SessionResumeParams,
  SessionResumeResult,
  SessionSubscribeResult,
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
import { REPLAY_GAP_EVENT, ReplayCursors } from "./replay.ts";

/** Replay cursors per session (P17-20). */
export interface ClientReplayOptions {
  /** Sessions with a cursor at most (default 256). */
  capacity?: number;
  /**
   * On every ready after the first (a reconnect), send the last `client.capabilities` again and
   * `session.subscribe` each tracked session from its cursor: the daemon replays what was missed
   * once and offers open requests again. Off: the surface resumes sessions itself and the cursors
   * only drop duplicates.
   */
  resubscribe?: boolean;
}

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
  /** Deadline for sending a server-request answer (default: `timeoutMs`). */
  answerTimeoutMs?: number;
  /** Redial after an unexpected close; off by default (the TUI app owns child restarts). */
  reconnect?: false | BackoffOptions;
  heartbeat?: { intervalMs?: number; deadlineMs?: number; maxOutstanding?: number };
  logs?: LogRing;
  /** An error frame carried a code outside the generated table. */
  onUnknownErrorCode?: (error: GatewayRpcError, method: string) => void;
  /** Track replay cursors (WebSocket attach); off by default. */
  replay?: boolean | ClientReplayOptions;
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
  /** A ready arrived on some earlier connection (the next ready is a reconnect). */
  private everReady = false;
  /** Counts every ready frame ever (never reset), to stop work begun for an older connection. */
  private readySerial = 0;
  private readonly cursors: ReplayCursors | null;
  private readonly resubscribeOnReady: boolean;
  private lastCapabilities: ClientCapabilitiesParams | null = null;

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
      ...(options.answerTimeoutMs === undefined
        ? {}
        : { answerTimeoutMs: options.answerTimeoutMs }),
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
    const replay = options.replay === true ? {} : options.replay || null;
    this.cursors =
      replay === null
        ? null
        : new ReplayCursors(replay.capacity === undefined ? {} : { capacity: replay.capacity });
    this.resubscribeOnReady = replay?.resubscribe === true;
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

  /** The replay cursors, when the client tracks them. */
  get replay(): ReplayCursors | null {
    return this.cursors;
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
    if (method === "client.capabilities") {
      this.lastCapabilities = params as ClientCapabilitiesParams;
    }
    return this.channel.call(method, params, options) as Promise<MethodTypes[M]["result"]>;
  }

  /** Resume a session and hand its still-open server requests to the handlers again. */
  async resume(params: SessionResumeParams, options?: CallOptions): Promise<SessionResumeResult> {
    const result = await this.request("session.resume", params, options);
    this.channel.redeliver(result.pending_requests ?? []);
    // Events after `last_seq` are still to come on this connection.
    this.cursors?.advance(result.session_id, this.cursors.epoch, result.last_seq);
    return result;
  }

  /** Watch a session from its cursor (or live from now when there is none). */
  async subscribeSession(
    sessionId: string,
    options?: CallOptions,
  ): Promise<SessionSubscribeResult> {
    const params = this.cursors?.resumeParams(sessionId) ?? { session_id: sessionId };
    const result = await this.request("session.subscribe", params, options);
    const cursors = this.cursors;
    if (cursors !== null) {
      if (result.gap)
        cursors.reset(result.session_id, result.gap.replay_epoch, result.gap.last_seq);
      else cursors.advance(result.session_id, result.replay_epoch, result.last_seq);
    }
    return result;
  }

  /**
   * Pull the events of a watched session after its cursor (`events.since`). They reach the
   * listeners like live ones (duplicates dropped); a gap is passed on as `session.replay_gap`.
   */
  async eventsSince(sessionId: string, options?: CallOptions): Promise<EventsSinceResult> {
    const known = this.cursors?.cursor(sessionId);
    const result = await this.request(
      "events.since",
      {
        session_id: sessionId,
        cursor: known?.seq ?? 0,
        ...(known?.epoch ? { epoch: known.epoch } : {}),
      },
      options,
    );
    if (result.gap) {
      this.onNotification(REPLAY_GAP_EVENT, {
        type: REPLAY_GAP_EVENT,
        session_id: result.session_id,
        ...result.gap,
      });
      return result;
    }
    for (const event of result.events ?? []) {
      this.onNotification(String((event as { type?: unknown }).type ?? ""), event);
    }
    this.cursors?.advance(result.session_id, result.replay_epoch, result.last_seq);
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
      this.readySerial += 1;
      const reconnect = this.everReady;
      this.everReady = true;
      this.reconnector?.markReady();
      this.channel.setServerCapabilities(
        Array.isArray(ready.capabilities) ? ready.capabilities.map(String) : [],
      );
      this.cursors?.setEpoch(ready.replay_epoch);
      // A server without an epoch (the stdio bridge) numbers each process from 1: no cursor of
      // an earlier link can be compared with its events.
      if (!ready.replay_epoch) this.cursors?.clear();
      if (ready.heartbeat === true) this.heartbeat.start();
      this.events.push(ready);
      if (reconnect && this.resubscribeOnReady) void this.resubscribe();
      return;
    }
    if (this.cursors !== null) {
      const verdict = this.cursors.admit(body);
      if (verdict === "duplicate") {
        this.logs.push(`[replay] ${method} seq ${String(body.seq)} already applied; dropped`);
        return;
      }
    }
    if (body.type !== method) {
      this.logs.push(`[protocol] notification ${method} without a matching type; passed on as is`);
    }
    this.events.push({ ...body, type: method } as ClientEvent);
  }

  /** After a reconnect: capabilities again, then every tracked session from its cursor. */
  private async resubscribe(): Promise<void> {
    const cursors = this.cursors;
    if (cursors === null) return;
    const mine = this.readySerial;
    try {
      if (this.lastCapabilities !== null) {
        await this.request("client.capabilities", this.lastCapabilities);
      }
      for (const sessionId of cursors.sessions()) {
        if (mine !== this.readySerial || this.disposed) return;
        await this.subscribeSession(sessionId);
      }
    } catch (error) {
      this.logs.push(
        `[replay] re-subscribing failed: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }

  private onTransportClosed(info: CloseInfo): void {
    this.heartbeat.stop();
    this.readyCount = 0;
    this.logs.push(`[lifecycle] connection closed (${info.reason})`);
    this.transport = null;
    if (!this.disposed) this.reconnector?.schedule();
  }
}
