// The renderer's gateway connections (P20-17): one live WebSocket per (connection, profile).
//
// Each dial asks main for a fresh single-use ticket (`connection.ticket`) and offers it only as
// the second subprotocol (R08.4); the ticket never lands in a URL, a log line or the state. The
// link itself is `@indus/client` (`IndusClient` + `WsTransport`: channel, replay cursors, server
// requests); this module adds the desktop policy around it:
//
// - reconnect ladder with full jitter (base 300 ms, cap 15 s) that resets only after a socket
//   stayed open 5 s, plus a non-blocking warning after 300 s of continuous failure;
// - after every ready, each watched session is subscribed again from its cursor (replay.ts);
// - liveness: during a turn one missed probe only schedules a re-probe, a streak rebuilds;
// - turn watchdog: a turn silent for 5 minutes is checked with a bounded `events.since`;
// - generation fencing: work begun for an older transport never touches newer state.

import {
  backoffDelay,
  type CallOptions,
  type ClientEvent,
  type Clock,
  type ConnectHooks,
  DESKTOP_BACKOFF,
  GatewayRpcError,
  IndusClient,
  PING_METHOD,
  systemClock,
  type Transport,
  type WebSocketFactory,
  WsTransport,
} from "@indus/client";
import type { EventTypes, MethodName, MethodTypes } from "@indus/protocol";
import { desktopBridge } from "../app/bridge.ts";
import type { IndusConnectionDescriptor, IndusDesktopConnection } from "../global.d.ts";
import { windowPreviewAutomation } from "../panes/preview/automation.ts";
import { replayWatchedSessions } from "./replay.ts";
import { ServerRequestStore } from "./serverRequests.ts";
import {
  LIVENESS_FAILURE_STREAK,
  LIVENESS_PROBE_TIMEOUT_MS,
  LIVENESS_REPROBE_DELAY_MS,
  methodTimeoutMs,
  PROMPT_SUBMIT_TIMEOUT_MS,
  RECONNECT_BASE_MS,
  RECONNECT_CAP_MS,
  RECONNECT_RESET_AFTER_OPEN_MS,
  RECONNECT_WARNING_AFTER_MS,
  REPLAY_CURSOR_LIMIT,
  RPC_DEFAULT_TIMEOUT_MS,
  RPC_STARTUP_TIMEOUT_MS,
  SERVER_REQUEST_ANSWER_TIMEOUT_MS,
  TURN_WATCHDOG_PROBE_TIMEOUT_MS,
  TURN_WATCHDOG_SILENCE_MS,
} from "./timing.ts";

export type ConnectionPhase = "idle" | "connecting" | "open" | "reconnecting" | "failed" | "closed";

export interface ConnectionState {
  /** The profile name, or `default`. */
  readonly key: string;
  readonly profile: string | null;
  readonly phase: ConnectionPhase;
  /** Open and the daemon's `gateway.ready` arrived. */
  readonly ready: boolean;
  readonly descriptor: IndusConnectionDescriptor | null;
  readonly capabilities: readonly string[];
  /** The daemon counts a viewer's "not shown here" decline (`request.decline`). */
  readonly countsDeclines: boolean;
  /** Reconnect attempts in the current failure streak (0 when healthy). */
  readonly attempt: number;
  /** When the current failure streak began (ms), null when healthy. */
  readonly failingSince: number | null;
  /** The streak has lasted 300 s: show the non-blocking warning. */
  readonly warning: boolean;
  readonly lastError: string | null;
  /** Sessions whose running turn went silent and did not answer the watchdog with news. */
  readonly stalledSessions: readonly string[];
  /** Bumps on every dial. */
  readonly generation: number;
}

/** The credential-free half of `indusDesktop.connection` this store needs. */
// `ticket` is the single-use dial: the URL and the EXACT subprotocol list to offer (local
// `indus-gateway-v1` + `indus-gateway-ticket.<t>`, remote `indus-dashboard-v1` +
// `indus-dashboard-ticket.<reservation>`). The renderer never builds credentials itself.
export type ConnectionBridge = Pick<IndusDesktopConnection, "get" | "ticket" | "onChanged">;

export interface DialRequest {
  readonly url: string;
  /** `indus-gateway-v1`, then the ticket subprotocol. */
  readonly protocols: readonly string[];
}

export type TransportFactory = (dial: DialRequest, hooks: ConnectHooks) => Transport;

export interface ConnectionStoreOptions {
  /** Default: `window.indusDesktop.connection`. */
  bridge?: ConnectionBridge | null;
  /** The window's profile (`facts.profile`); the primary connection uses it. */
  defaultProfile?: string | null;
  clock?: Clock;
  random?: () => number;
  /** Default: a `WsTransport` over the host's WebSocket. */
  transport?: TransportFactory;
  webSocket?: WebSocketFactory;
}

type EventListener = (event: ClientEvent) => void;
type SessionListener = (event: SessionEvent) => void;

/**
 * What this viewer renders (every kind a person answers, plus the preview kinds its preview pane
 * answers, P24-25) and accepts (declines).
 */
export const CLIENT_FEATURES: readonly string[] = [
  "server_requests",
  "server_requests.preview.act",
  "server_requests.preview.read",
  "request.decline",
];

const READY = "gateway.ready";
const TURN_STARTED = "turn.started";
const TURN_DONE = "turn.done";

function sessionOf(event: ClientEvent): string | null {
  const id = (event as { session_id?: unknown }).session_id;
  return typeof id === "string" && id !== "" ? id : null;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function defaultBridge(): ConnectionBridge | null {
  return desktopBridge()?.connection ?? null;
}

/** A transport that reports its close at once (no ticket could be had). */
function refusedTransport(reason: string): Transport {
  const closes = new Set<(info: { reason: string }) => void>();
  queueMicrotask(() => {
    for (const listener of [...closes]) listener({ reason });
  });
  return {
    send: () => {
      throw new Error(reason);
    },
    onFrame: () => () => undefined,
    onClose: (listener) => {
      closes.add(listener);
      return () => closes.delete(listener);
    },
    close: () => undefined,
  };
}

// -- what the chat surfaces see of a connection (slice C) ------------------------------------------

export type ConnectionMode = IndusConnectionDescriptor["mode"];
export type RequestOptions = CallOptions;
/** One event of a session (what `onSessionEvent` delivers). */
export type SessionEvent = EventTypes[keyof EventTypes];

/** The call deadlines the chat surfaces name explicitly (the same numbers as `timing.ts`). */
export const REQUEST_TIMEOUTS = {
  default: RPC_DEFAULT_TIMEOUT_MS,
  startup: RPC_STARTUP_TIMEOUT_MS,
  promptSubmit: PROMPT_SUBMIT_TIMEOUT_MS,
  approvalRespond: SERVER_REQUEST_ANSWER_TIMEOUT_MS,
} as const;

/**
 * The part of a `GatewayConnection` the composer, cards, model picker and status bar use. The
 * real connection implements it; tests hand in a scripted fake.
 */
export interface ChatConnection {
  /** `local` or `remote` once main described the backend, else null. */
  readonly mode: ConnectionMode | null;
  readonly codeVersion: string | null;
  request<M extends MethodName>(
    method: M,
    params: MethodTypes[M]["params"],
    options?: RequestOptions,
  ): Promise<MethodTypes[M]["result"]>;
  onSessionEvent(sessionId: string, listener: (event: SessionEvent) => void): () => void;
  reconnect(): void;
}

export class GatewayConnection implements ChatConnection {
  readonly key: string;
  readonly profile: string | null;
  readonly serverRequests: ServerRequestStore;
  /** The underlying client (tests and diagnostics; product code uses the methods below). */
  readonly client: IndusClient;

  private readonly bridge: ConnectionBridge | null;
  private readonly clock: Clock;
  private readonly random: () => number;
  private readonly makeLink: TransportFactory;
  private readonly stateListeners = new Set<() => void>();
  private readonly eventListeners = new Set<EventListener>();
  private readonly sessionListeners = new Map<string, Set<SessionListener>>();
  private readonly watched = new Set<string>();
  private readonly running = new Map<string, number>();
  private readonly watchdogTimers = new Map<string, unknown>();
  private readonly readyWaiters = new Set<{
    resolve: () => void;
    reject: (error: Error) => void;
  }>();
  private state: ConnectionState;
  private started = false;
  private disposed = false;
  private everReady = false;
  private dialSerial = 0;
  private pendingDial: DialRequest | null = null;
  private transport: Transport | null = null;
  private reconnectTimer: unknown;
  private startupTimer: unknown;
  private openResetTimer: unknown;
  private warningTimer: unknown;
  private reprobeTimer: unknown;
  private probeFailures = 0;
  private stalled = new Set<string>();

  constructor(
    profile: string | null,
    options: {
      bridge: ConnectionBridge | null;
      clock: Clock;
      random: () => number;
      transport: TransportFactory;
    },
  ) {
    this.profile = profile;
    this.key = profile ?? "default";
    this.bridge = options.bridge;
    this.clock = options.clock;
    this.random = options.random;
    this.makeLink = options.transport;
    this.state = {
      key: this.key,
      profile,
      phase: "idle",
      ready: false,
      descriptor: null,
      capabilities: [],
      countsDeclines: false,
      attempt: 0,
      failingSince: null,
      warning: false,
      lastError: null,
      stalledSessions: [],
      generation: 0,
    };
    this.client = new IndusClient({
      connect: (hooks) => this.openTransport(hooks),
      clock: this.clock,
      timeoutMs: RPC_DEFAULT_TIMEOUT_MS,
      answerTimeoutMs: SERVER_REQUEST_ANSWER_TIMEOUT_MS,
      reconnect: false,
      replay: { capacity: REPLAY_CURSOR_LIMIT, resubscribe: false },
    });
    this.serverRequests = new ServerRequestStore({
      now: () => this.clock.now(),
      channel: () => this.client.channel,
      preview: windowPreviewAutomation,
    });
    this.client.handle({ serverRequests: this.serverRequests.handlers() });
    this.client.subscribe((event) => this.onClientEvent(event));
  }

  getState(): ConnectionState {
    return this.state;
  }

  /** `local` or `remote`, once main described the backend. */
  get mode(): IndusConnectionDescriptor["mode"] | null {
    return this.state.descriptor?.mode ?? null;
  }

  /** The backend's code version, once known. */
  get codeVersion(): string | null {
    return this.state.descriptor?.codeVersion ?? null;
  }

  /** Drop the current socket and dial again now (a person pressed Reconnect). */
  reconnect(): void {
    if (this.disposed) return;
    if (!this.started) {
      this.start();
      return;
    }
    this.patch({ attempt: 0 });
    this.redialNow("reconnect asked");
  }

  subscribe(listener: () => void): () => void {
    this.stateListeners.add(listener);
    return () => {
      this.stateListeners.delete(listener);
    };
  }

  /** Begin dialling (idempotent; after a terminal failure it dials again). */
  start(): void {
    if (this.disposed) return;
    if (this.started && this.state.phase !== "failed") return;
    this.started = true;
    void this.dial();
  }

  async close(): Promise<void> {
    if (this.disposed) return;
    this.disposed = true;
    this.dialSerial += 1;
    this.clearTimers();
    for (const timer of this.watchdogTimers.values()) this.clock.clearTimeout(timer);
    this.watchdogTimers.clear();
    for (const waiter of [...this.readyWaiters]) waiter.reject(new Error("connection closed"));
    this.readyWaiters.clear();
    this.serverRequests.clear();
    this.patch({ phase: "closed", ready: false });
    await this.client.close();
  }

  /** A typed call; the method's own default deadline unless `options` names one. */
  request<M extends MethodName>(
    method: M,
    params: MethodTypes[M]["params"],
    options: CallOptions = {},
  ): Promise<MethodTypes[M]["result"]> {
    return this.client.request(method, params, { timeoutMs: methodTimeoutMs(method), ...options });
  }

  /** Every event (after duplicate removal), in arrival order. */
  onEvent(listener: EventListener): () => void {
    this.eventListeners.add(listener);
    return () => {
      this.eventListeners.delete(listener);
    };
  }

  /** The events of one session (server events that carry its `session_id`). */
  onSessionEvent(sessionId: string, listener: SessionListener): () => void {
    let set = this.sessionListeners.get(sessionId);
    if (set === undefined) {
      set = new Set();
      this.sessionListeners.set(sessionId, set);
    }
    set.add(listener);
    return () => {
      const current = this.sessionListeners.get(sessionId);
      current?.delete(listener);
      if (current?.size === 0) this.sessionListeners.delete(sessionId);
    };
  }

  /** Watch a session (live events, server requests); kept and replayed across reconnects. */
  async watchSession(sessionId: string): Promise<void> {
    this.watched.add(sessionId);
    if (!this.state.ready) return;
    const generation = this.dialSerial;
    try {
      await this.client.subscribeSession(sessionId);
    } catch (error) {
      if (generation === this.dialSerial) throw error;
    }
  }

  unwatchSession(sessionId: string): void {
    if (!this.watched.delete(sessionId)) return;
    this.client.replay?.forget(sessionId);
    this.running.delete(sessionId);
    this.clearWatchdog(sessionId);
    if (this.state.ready) {
      this.client.request("session.unsubscribe", { session_id: sessionId }).catch(() => undefined);
    }
  }

  /** Resolves at the next (or current) ready; rejects on timeout or close. */
  whenReady(timeoutMs: number = RPC_STARTUP_TIMEOUT_MS): Promise<void> {
    if (this.state.ready) return Promise.resolve();
    if (this.disposed) return Promise.reject(new Error("connection closed"));
    return new Promise<void>((resolve, reject) => {
      const waiter = {
        resolve: () => {
          this.clock.clearTimeout(timer);
          resolve();
        },
        reject: (error: Error) => {
          this.clock.clearTimeout(timer);
          reject(error);
        },
      };
      const timer = this.clock.setTimeout(() => {
        this.readyWaiters.delete(waiter);
        reject(new Error("not connected in time"));
      }, timeoutMs);
      this.readyWaiters.add(waiter);
    });
  }

  /**
   * Check the link with one bounded ping (window woke, network came back, a turn went quiet).
   * True when the daemon answered. During a turn the first miss only schedules a re-probe;
   * only a streak (or a miss with no turn running) closes the socket for the ladder to rebuild.
   */
  async probeLiveness(timeoutMs: number = LIVENESS_PROBE_TIMEOUT_MS): Promise<boolean> {
    if (!this.state.ready) return false;
    const generation = this.dialSerial;
    try {
      await this.client.channel.call(PING_METHOD, {}, { timeoutMs });
      if (generation === this.dialSerial) this.probeFailures = 0;
      return true;
    } catch (error) {
      if (generation !== this.dialSerial || this.disposed) return false;
      // An error frame is an answer: the daemon is alive (an older one may lack the method).
      if (error instanceof GatewayRpcError) {
        this.probeFailures = 0;
        return true;
      }
      this.probeFailures += 1;
      if (this.running.size > 0 && this.probeFailures < LIVENESS_FAILURE_STREAK) {
        if (this.reprobeTimer === undefined) {
          this.reprobeTimer = this.clock.setTimeout(() => {
            this.reprobeTimer = undefined;
            if (generation === this.dialSerial) void this.probeLiveness(timeoutMs);
          }, LIVENESS_REPROBE_DELAY_MS);
        }
        return false;
      }
      this.probeFailures = 0;
      void this.transport?.close();
      return false;
    }
  }

  /** Main moved the backend (new daemon, other port): rebuild the socket now. */
  onDescriptorChanged(next: IndusConnectionDescriptor | null): void {
    if (this.disposed || !this.started) return;
    const current = this.state.descriptor;
    // Main announces the backend when its boot ends, often before this dial's own
    // `connection.get` answers: that dial is about to read the same descriptor, so no redial.
    if (current === null && this.state.phase === "connecting") return;
    if (next !== null && current !== null && (next.profile ?? null) !== (current.profile ?? null))
      return;
    if (
      next !== null &&
      current !== null &&
      next.instanceId === current.instanceId &&
      next.wsUrl === current.wsUrl
    )
      return;
    this.patch({ descriptor: next });
    this.redialNow("the backend changed");
  }

  // -- dialling ---------------------------------------------------------------------------------

  private async dial(): Promise<void> {
    if (this.disposed) return;
    this.dialSerial += 1;
    const generation = this.dialSerial;
    this.patch({
      phase: this.everReady ? "reconnecting" : "connecting",
      ready: false,
      generation,
    });
    if (this.bridge === null) {
      // A plain browser preview: there is no main process to ask for a ticket.
      this.patch({ phase: "failed", lastError: "no desktop bridge" });
      return;
    }
    const scope = this.profile === null ? {} : { profile: this.profile };
    let dial: DialRequest;
    try {
      const descriptor = await this.bridge.get(scope);
      if (generation !== this.dialSerial || this.disposed) return;
      this.patch({ descriptor });
      const minted = await this.bridge.ticket(scope);
      if (generation !== this.dialSerial || this.disposed) return;
      if (!Array.isArray(minted.protocols) || minted.protocols.length === 0) {
        throw new Error("the ticket came without subprotocols");
      }
      dial = { url: minted.wsUrl, protocols: [...minted.protocols] };
    } catch (error) {
      if (generation !== this.dialSerial || this.disposed) return;
      const text = errorText(error);
      // The boot itself failed (local, installer, remote, reauth, reset): the boot overlay owns
      // recovery; dialling again would only restart that boot. `start()` resumes after ready.
      if (/^boot-[a-z]+:/.test(text)) {
        this.patch({ phase: "failed", ready: false, lastError: text });
        return;
      }
      this.failAttempt(generation, `no ticket: ${text}`);
      return;
    }
    this.pendingDial = dial;
    this.client.start();
    this.startupTimer = this.clock.setTimeout(() => {
      this.startupTimer = undefined;
      if (generation !== this.dialSerial || this.state.ready) return;
      this.patch({ lastError: "the daemon did not get ready in time" });
      void this.transport?.close();
    }, RPC_STARTUP_TIMEOUT_MS);
  }

  /** `IndusClient`'s connect hook: the ticket is used once and dropped. */
  private openTransport(hooks: ConnectHooks): Transport {
    const dial = this.pendingDial;
    this.pendingDial = null;
    const generation = this.dialSerial;
    const link = dial === null ? refusedTransport("no ticket") : this.makeLink(dial, hooks);
    this.transport = link;
    link.onClose((info) => this.onTransportClosed(generation, info));
    return link;
  }

  private onTransportClosed(
    generation: number,
    info: { reason: string; terminal?: boolean },
  ): void {
    if (generation !== this.dialSerial || this.disposed) return;
    this.transport = null;
    // Running turns stay tracked: they go on in the daemon and the replay brings their end.
    this.clearTimers();
    if (info.terminal === true) {
      this.patch({ phase: "failed", ready: false, lastError: info.reason });
      for (const waiter of [...this.readyWaiters]) waiter.reject(new Error(info.reason));
      this.readyWaiters.clear();
      return;
    }
    this.failAttempt(generation, info.reason);
  }

  private failAttempt(generation: number, reason: string): void {
    if (generation !== this.dialSerial || this.disposed) return;
    const now = this.clock.now();
    const failingSince = this.state.failingSince ?? now;
    const attempt = this.state.attempt + 1;
    const warning = now - failingSince >= RECONNECT_WARNING_AFTER_MS;
    this.patch({
      phase: this.everReady ? "reconnecting" : "connecting",
      ready: false,
      attempt,
      failingSince,
      warning,
      lastError: reason,
    });
    if (!warning && this.warningTimer === undefined) {
      this.warningTimer = this.clock.setTimeout(
        () => {
          this.warningTimer = undefined;
          if (this.state.failingSince !== null && !this.disposed) this.patch({ warning: true });
        },
        RECONNECT_WARNING_AFTER_MS - (now - failingSince),
      );
    }
    const delay = backoffDelay(attempt, {
      baseMs: RECONNECT_BASE_MS,
      capMs: RECONNECT_CAP_MS,
      jitter: DESKTOP_BACKOFF.jitter,
      random: this.random,
    });
    this.reconnectTimer = this.clock.setTimeout(() => {
      this.reconnectTimer = undefined;
      if (generation === this.dialSerial) void this.dial();
    }, delay);
  }

  private redialNow(reason: string): void {
    this.clearTimers();
    this.patch({ lastError: reason });
    const link = this.transport;
    this.transport = null;
    // Bump first so the old link's close is ignored, then dial at once.
    void this.dial();
    void link?.close();
  }

  private clearTimers(): void {
    for (const timer of [this.reconnectTimer, this.startupTimer, this.openResetTimer]) {
      if (timer !== undefined) this.clock.clearTimeout(timer);
    }
    if (this.reprobeTimer !== undefined) this.clock.clearTimeout(this.reprobeTimer);
    this.reconnectTimer = undefined;
    this.startupTimer = undefined;
    this.openResetTimer = undefined;
    this.reprobeTimer = undefined;
    this.probeFailures = 0;
  }

  // -- events -----------------------------------------------------------------------------------

  private onClientEvent(event: ClientEvent): void {
    if (this.disposed) return;
    if (event.type === READY) {
      this.onReady(event as ClientEvent & { capabilities?: unknown });
      this.dispatch(event, null);
      return;
    }
    const sessionId = sessionOf(event);
    if (sessionId !== null) this.trackTurn(sessionId, event);
    this.dispatch(event, sessionId);
  }

  private onReady(event: { capabilities?: unknown }): void {
    const generation = this.dialSerial;
    if (this.startupTimer !== undefined) this.clock.clearTimeout(this.startupTimer);
    this.startupTimer = undefined;
    const capabilities = Array.isArray(event.capabilities) ? event.capabilities.map(String) : [];
    this.everReady = true;
    this.patch({
      phase: "open",
      ready: true,
      capabilities,
      countsDeclines: capabilities.includes("request.decline"),
    });
    if (this.openResetTimer !== undefined) this.clock.clearTimeout(this.openResetTimer);
    this.openResetTimer = this.clock.setTimeout(() => {
      this.openResetTimer = undefined;
      if (generation !== this.dialSerial || !this.state.ready) return;
      if (this.warningTimer !== undefined) this.clock.clearTimeout(this.warningTimer);
      this.warningTimer = undefined;
      this.patch({ attempt: 0, failingSince: null, warning: false, lastError: null });
    }, RECONNECT_RESET_AFTER_OPEN_MS);
    for (const waiter of [...this.readyWaiters]) waiter.resolve();
    this.readyWaiters.clear();
    void this.announce(generation).then(() => this.replay(generation));
  }

  /**
   * Tell the daemon on every new socket that this viewer is the desktop app (`surface: "gui"`:
   * presence, the busy default) and renders every server-request kind, with "not shown here"
   * declines. Without it the daemon offers this window no approval cards. An older daemon
   * without the method still serves the rest.
   */
  private async announce(generation: number): Promise<void> {
    if (generation !== this.dialSerial || this.disposed) return;
    try {
      await this.client.request("client.capabilities", {
        features: [...CLIENT_FEATURES],
        surface: "gui",
      });
    } catch {
      // keep going: replay and calls work without it
    }
  }

  private replay(generation: number): void {
    if (generation !== this.dialSerial || this.disposed) return;
    if (this.watched.size > 0) {
      void replayWatchedSessions(this.client, [...this.watched], {
        isCurrent: () => generation === this.dialSerial && !this.disposed,
        onFailure: (sessionId, error) => {
          // The daemon no longer has the session (deleted, pruned): nothing will ever replay
          // under its cursor, so stop asking on every reconnect.
          if (error instanceof GatewayRpcError && error.is("SESSION_NOT_FOUND")) {
            this.watched.delete(sessionId);
            this.client.replay?.forget(sessionId);
          }
        },
      });
    }
  }

  private trackTurn(sessionId: string, event: ClientEvent): void {
    if (this.stalled.delete(sessionId)) this.patch({ stalledSessions: [...this.stalled] });
    if (event.type === TURN_STARTED) {
      this.running.set(sessionId, this.clock.now());
      this.armWatchdog(sessionId, TURN_WATCHDOG_SILENCE_MS);
      return;
    }
    if (event.type === TURN_DONE) {
      this.running.delete(sessionId);
      this.clearWatchdog(sessionId);
      return;
    }
    if (this.running.has(sessionId)) this.running.set(sessionId, this.clock.now());
  }

  private armWatchdog(sessionId: string, ms: number): void {
    if (this.watchdogTimers.has(sessionId)) return;
    const timer = this.clock.setTimeout(() => {
      this.watchdogTimers.delete(sessionId);
      const last = this.running.get(sessionId);
      if (last === undefined || this.disposed) return;
      const silent = this.clock.now() - last;
      if (silent < TURN_WATCHDOG_SILENCE_MS) {
        this.armWatchdog(sessionId, TURN_WATCHDOG_SILENCE_MS - silent);
        return;
      }
      void this.checkSilentTurn(sessionId);
    }, ms);
    this.watchdogTimers.set(sessionId, timer);
  }

  private clearWatchdog(sessionId: string): void {
    const timer = this.watchdogTimers.get(sessionId);
    if (timer !== undefined) this.clock.clearTimeout(timer);
    this.watchdogTimers.delete(sessionId);
  }

  private async checkSilentTurn(sessionId: string): Promise<void> {
    const generation = this.dialSerial;
    const before = this.running.get(sessionId);
    try {
      const result = await this.client.eventsSince(sessionId, {
        timeoutMs: TURN_WATCHDOG_PROBE_TIMEOUT_MS,
      });
      if (generation !== this.dialSerial || this.disposed) return;
      const news = (result.events?.length ?? 0) > 0 || result.gap != null;
      if (!news && this.running.get(sessionId) === before && this.running.has(sessionId)) {
        this.stalled.add(sessionId);
        this.patch({ stalledSessions: [...this.stalled] });
      }
      if (this.running.has(sessionId)) this.armWatchdog(sessionId, TURN_WATCHDOG_SILENCE_MS);
    } catch {
      if (generation !== this.dialSerial || this.disposed) return;
      void this.probeLiveness();
      if (this.running.has(sessionId)) this.armWatchdog(sessionId, TURN_WATCHDOG_SILENCE_MS);
    }
  }

  private dispatch(event: ClientEvent, sessionId: string | null): void {
    for (const listener of [...this.eventListeners]) listener(event);
    if (sessionId === null) return;
    const set = this.sessionListeners.get(sessionId);
    if (set === undefined) return;
    // Only server events carry a `session_id`; transport and ready events never reach here.
    for (const listener of [...set]) listener(event as SessionEvent);
  }

  private patch(changes: Partial<ConnectionState>): void {
    let changed = false;
    for (const [name, value] of Object.entries(changes)) {
      if (this.state[name as keyof ConnectionState] !== value) {
        changed = true;
        break;
      }
    }
    if (!changed) return;
    this.state = { ...this.state, ...changes };
    for (const listener of [...this.stateListeners]) listener();
  }
}

export class ConnectionStore {
  private readonly options: ConnectionStoreOptions;
  private readonly bridge: ConnectionBridge | null;
  private readonly clock: Clock;
  private readonly connections = new Map<string, GatewayConnection>();
  private readonly unsubscribeChanged: () => void;
  private primaryProfile: string | null | undefined;

  constructor(options: ConnectionStoreOptions = {}) {
    this.options = options;
    this.bridge = options.bridge === undefined ? defaultBridge() : options.bridge;
    this.clock = options.clock ?? systemClock;
    this.unsubscribeChanged =
      this.bridge?.onChanged((descriptor) => {
        for (const connection of this.connections.values()) {
          connection.onDescriptorChanged(descriptor);
        }
      }) ?? (() => undefined);
  }

  /** The one connection for `profile` (null: the backend's default), created on first use. */
  connection(profile: string | null = null): GatewayConnection {
    const key = profile ?? "default";
    let connection = this.connections.get(key);
    if (connection === undefined) {
      const webSocket = this.options.webSocket;
      connection = new GatewayConnection(profile, {
        bridge: this.bridge,
        clock: this.clock,
        random: this.options.random ?? Math.random,
        transport:
          this.options.transport ??
          ((dial, hooks) =>
            new WsTransport({
              url: dial.url,
              protocols: dial.protocols,
              clock: this.clock,
              logs: hooks.logs,
              onEvent: hooks.onEvent,
              ...(webSocket === undefined ? {} : { webSocket }),
            })),
      });
      this.connections.set(key, connection);
    }
    return connection;
  }

  /** The window's own profile (its foreground profile once the switcher set one). */
  primary(): GatewayConnection {
    return this.connection(
      this.primaryProfile !== undefined
        ? this.primaryProfile
        : (this.options.defaultProfile ?? null),
    );
  }

  /** The window now shows `profile` (P20-26 live swap): `primary()` follows it. */
  setPrimary(profile: string | null): void {
    this.primaryProfile = profile;
  }

  all(): GatewayConnection[] {
    return [...this.connections.values()];
  }

  /** Close and forget one profile's connection (its profile was renamed or deleted, P20-26). */
  async release(profile: string | null): Promise<void> {
    const key = profile ?? "default";
    const connection = this.connections.get(key);
    this.connections.delete(key);
    await connection?.close();
  }

  async closeAll(): Promise<void> {
    this.unsubscribeChanged();
    const all = [...this.connections.values()];
    this.connections.clear();
    await Promise.all(all.map((connection) => connection.close()));
  }
}
