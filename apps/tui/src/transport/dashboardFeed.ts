// The dashboard feed (P18-09 follow-up): when the dashboard's Chat tab starts this TUI, the TUI
// publishes the agent's events to the dashboard so the chat's right rail can show them.
//
// The dashboard hands the child two variables: `INDUS_TUI_SIDECAR_URL` (`ws://…/api/pub?channel=…`,
// no credential in it) and `INDUS_TUI_SIDECAR_CREDENTIAL`. The credential is taken out of the
// environment at once (the bridge child and every process it starts must never inherit it) and
// travels only as a WebSocket subprotocol (`indus-dashboard-internal.<value>`, after
// `indus-dashboard-v1`): never in the URL, a log line, an event or an error text.
//
// Each protocol event the client sees (both transports: the daemon's WebSocket and the local
// bridge) is sent as one JSON text frame, `{"type": …, …}`; the client's own `transport.*` events
// and `gateway.ready` stay inside the TUI. It is best-effort and silent — nothing here ever reaches
// the terminal: frames wait (at most 256, the oldest dropped) until the socket opens, a frame over
// 1 MiB is skipped (the dashboard would close the socket), a socket more than 4 MiB behind drops
// frames, and after a lost link it dials again after 1 s, 2 s, 4 s … 30 s, at most 8 times per
// outage. 4400 (bad channel), 4401 (credential refused) and 4403 (forbidden) stop it for good.
//
// This file imports nothing at run time, so Node can run it as it is (the contract test's driver).

export const SIDECAR_URL_ENV = "INDUS_TUI_SIDECAR_URL";
export const SIDECAR_CREDENTIAL_ENV = "INDUS_TUI_SIDECAR_CREDENTIAL";
export const DASHBOARD_SUBPROTOCOL = "indus-dashboard-v1";
export const INTERNAL_SUBPROTOCOL_PREFIX = "indus-dashboard-internal.";
export const FEED_QUEUE_FRAMES = 256;
export const MAX_FEED_FRAME_BYTES = 1024 * 1024;
export const MAX_BUFFERED_BYTES = 4 * 1024 * 1024;
export const FEED_RETRIES = 8;
export const FEED_BASE_MS = 1_000;
export const FEED_CAP_MS = 30_000;
/** Close codes no retry can fix. */
export const FEED_STOP_CODES: ReadonlySet<number> = new Set([4400, 4401, 4403]);

const OPEN = 1;
const CREDENTIAL_SHAPE = /^[A-Za-z0-9._~-]{1,256}$/;
const KEPT_LOCAL = new Set(["gateway.ready"]);

export interface FeedPlan {
  readonly url: string;
  readonly protocols: readonly string[];
}

/** Why the feed URL may not be dialled, or null when it may. */
export function feedUrlProblem(url: string): string | null {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return "not a URL";
  }
  if (parsed.protocol !== "ws:" && parsed.protocol !== "wss:") return "not a ws: or wss: URL";
  if (parsed.username || parsed.password) return "the URL carries user-info";
  if (parsed.hash || url.includes("#")) return "the URL carries a fragment";
  return null;
}

/**
 * The feed to open, or null when this TUI was not started by the dashboard (or what it was given
 * cannot be used). Always removes the credential from `env`.
 */
export function takeFeedPlan(env: Record<string, string | undefined>): FeedPlan | null {
  const url = env[SIDECAR_URL_ENV]?.trim() ?? "";
  const credential = env[SIDECAR_CREDENTIAL_ENV]?.trim() ?? "";
  delete env[SIDECAR_CREDENTIAL_ENV];
  if (!url || !credential) return null;
  if (feedUrlProblem(url) !== null || !CREDENTIAL_SHAPE.test(credential)) return null;
  return { url, protocols: [DASHBOARD_SUBPROTOCOL, `${INTERNAL_SUBPROTOCOL_PREFIX}${credential}`] };
}

/** True for an event the dashboard may see: a protocol event, not one the client made up. */
export function isPublishable(event: { readonly type?: unknown }): boolean {
  const type = event.type;
  return (
    typeof type === "string" &&
    type !== "" &&
    !type.startsWith("transport.") &&
    !KEPT_LOCAL.has(type)
  );
}

/** Wait before dial `attempt` (1-based) of an outage. */
export function feedDelay(attempt: number): number {
  const step = Math.max(0, Math.floor(attempt) - 1);
  return Math.min(FEED_CAP_MS, FEED_BASE_MS * 2 ** Math.min(step, 16));
}

/** The parts of a WHATWG WebSocket the feed uses. */
export interface FeedSocket {
  readonly readyState: number;
  readonly bufferedAmount?: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  addEventListener(type: "open" | "close" | "error", listener: (event: never) => void): void;
}

export type FeedSocketFactory = (url: string, protocols: string[]) => FeedSocket;

export interface FeedTimers {
  set(run: () => void, ms: number): unknown;
  clear(handle: unknown): void;
}

export interface FeedDeps {
  socket?: FeedSocketFactory;
  timers?: FeedTimers;
}

export type FeedState = "idle" | "connecting" | "open" | "retrying" | "stopped";

function globalSocket(): FeedSocketFactory | null {
  const ctor = (globalThis as { WebSocket?: new (url: string, protocols: string[]) => FeedSocket })
    .WebSocket;
  return ctor === undefined ? null : (url, protocols) => new ctor(url, protocols);
}

const systemTimers: FeedTimers = {
  set: (run, ms) => {
    const handle = setTimeout(run, ms);
    // A pending redial never keeps the program alive.
    (handle as { unref?: () => void }).unref?.();
    return handle;
  },
  clear: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

const encoder = new TextEncoder();

function byteLength(text: string): number {
  return text.length * 3 <= MAX_FEED_FRAME_BYTES ? text.length : encoder.encode(text).length;
}

export class DashboardFeed {
  readonly #plan: FeedPlan;
  readonly #socketFactory: FeedSocketFactory | null;
  readonly #timers: FeedTimers;
  #socket: FeedSocket | null = null;
  #state: FeedState = "idle";
  #queue: string[] = [];
  #attempts = 0;
  #retry: unknown = null;
  #generation = 0;
  /** Frames left out: queue overflow, too large, or a socket too far behind. */
  dropped = 0;
  /** Frames handed to the socket. */
  sent = 0;

  constructor(plan: FeedPlan, deps: FeedDeps = {}) {
    this.#plan = plan;
    this.#socketFactory = deps.socket ?? globalSocket();
    this.#timers = deps.timers ?? systemTimers;
  }

  get state(): FeedState {
    return this.#state;
  }

  /** Frames waiting for the socket to open. */
  get queued(): number {
    return this.#queue.length;
  }

  start(): void {
    if (this.#state !== "idle") return;
    this.#dial();
  }

  /** Send one client event (anything that is not publishable is ignored). Never throws. */
  publish(event: { readonly type?: unknown }): void {
    if (this.#state === "stopped" || !isPublishable(event)) return;
    let text: string;
    try {
      text = JSON.stringify(event);
    } catch {
      this.dropped += 1;
      return;
    }
    if (byteLength(text) > MAX_FEED_FRAME_BYTES) {
      this.dropped += 1;
      return;
    }
    const socket = this.#socket;
    if (this.#state === "open" && socket !== null && socket.readyState === OPEN) {
      this.#send(socket, text);
      return;
    }
    this.#queue.push(text);
    if (this.#queue.length > FEED_QUEUE_FRAMES) {
      this.#queue.shift();
      this.dropped += 1;
    }
  }

  /** Stop for good and close the socket. */
  close(): void {
    this.#stop();
    const socket = this.#socket;
    this.#socket = null;
    if (socket === null) return;
    try {
      socket.close(1000, "tui closed");
    } catch {
      // already closing
    }
  }

  /** Resolves once every queued frame was handed over and the socket buffer is empty (or the feed
   * stopped, or `timeoutMs` passed). */
  settled(timeoutMs = 5_000, pollMs = 20): Promise<void> {
    // (tests and the contract driver; the TUI itself never waits on the feed)
    const deadline = Date.now() + timeoutMs;
    return new Promise((resolve) => {
      const check = () => {
        const socket = this.#socket;
        const idle =
          this.#state === "open" && this.#queue.length === 0 && (socket?.bufferedAmount ?? 0) === 0;
        if (idle || this.#state === "stopped" || Date.now() >= deadline) {
          resolve();
          return;
        }
        // A plain timer: the caller waits on it, so it keeps the program alive meanwhile.
        setTimeout(check, pollMs);
      };
      check();
    });
  }

  #stop(): void {
    this.#state = "stopped";
    this.#generation += 1;
    this.#queue = [];
    if (this.#retry !== null) {
      this.#timers.clear(this.#retry);
      this.#retry = null;
    }
  }

  #send(socket: FeedSocket, text: string): void {
    if ((socket.bufferedAmount ?? 0) > MAX_BUFFERED_BYTES) {
      this.dropped += 1;
      return;
    }
    try {
      socket.send(text);
      this.sent += 1;
    } catch {
      this.dropped += 1;
    }
  }

  #dial(): void {
    const generation = ++this.#generation;
    this.#state = "connecting";
    const factory = this.#socketFactory;
    if (factory === null) {
      this.#stop();
      return;
    }
    let socket: FeedSocket;
    try {
      socket = factory(this.#plan.url, [...this.#plan.protocols]);
    } catch {
      this.#lost(generation, null);
      return;
    }
    this.#socket = socket;
    socket.addEventListener("open", () => {
      if (generation !== this.#generation) return;
      this.#state = "open";
      this.#attempts = 0;
      const waiting = this.#queue;
      this.#queue = [];
      for (const text of waiting) this.#send(socket, text);
    });
    socket.addEventListener("close", (event: { code?: number }) => {
      if (generation !== this.#generation) return;
      this.#socket = null;
      this.#lost(generation, typeof event.code === "number" ? event.code : null);
    });
    socket.addEventListener("error", () => {
      // the close event follows
    });
  }

  #lost(generation: number, code: number | null): void {
    if (generation !== this.#generation || this.#state === "stopped") return;
    if (code !== null && FEED_STOP_CODES.has(code)) {
      this.#stop();
      return;
    }
    if (this.#attempts >= FEED_RETRIES) {
      this.#stop();
      return;
    }
    this.#attempts += 1;
    this.#state = "retrying";
    this.#retry = this.#timers.set(() => {
      this.#retry = null;
      if (generation === this.#generation && this.#state === "retrying") this.#dial();
    }, feedDelay(this.#attempts));
  }
}
