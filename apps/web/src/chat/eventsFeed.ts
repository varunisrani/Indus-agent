// The chat rail's event feed (P18-27): a listen-only socket to `/api/events?channel=<id>`.
//
// The backend fans out every event the chat's TUI child publishes for that channel; subscribers
// never speak (anything sent is dropped server-side). Frames are JSON: either a protocol event
// (`{"type": "session.info", …}`, `packages/protocol` EVENTS) or a JSON-RPC notification whose
// method is the event type (`{"method": "session.info", "params": {…}}`); both become a
// `FeedEvent`. Anything else is ignored.
//
// The feed is best-effort and never touches the terminal (R09.1). After a drop it waits 1 s, 2 s,
// 4 s … up to 30 s between attempts, at most 15 attempts per outage (an open resets the count),
// then stops with a notice and a manual reconnect. Each attempt — the gated ticket fetch and the
// handshake — has 15 s. 4401 and 4403 stop at once: only a reload (a new login or token) helps.
// 4400 (bad channel) stops too; it never heals.

import { type WsTarget, wsTarget } from "../lib/wsUrl.ts";
import type { PtySocket } from "./ptySocket.ts";

export const EVENTS_PATH = "/api/events";
export const EVENTS_PURPOSE = "web-events";
export const FEED_BASE_MS = 1_000;
export const FEED_CAP_MS = 30_000;
export const FEED_ATTEMPTS = 15;
export const FEED_CONNECT_TIMEOUT_MS = 15_000;

export const FEED_COPY = {
  loginExpired: "Live chat details stopped: your login expired. Reload the page.",
  gaveUp: `Live chat details stopped after ${FEED_ATTEMPTS} tries to reconnect. Reconnect, or reload the page.`,
  unavailable: "Live chat details are not available for this chat.",
  retrying: (ms: number) => `Live chat details paused; trying again in ${Math.round(ms / 1000)} s.`,
} as const;

/** Wait before attempt `attempt` (1-based) of an outage: 1, 2, 4, 8, 16, 30, 30 … seconds. */
export function feedDelay(attempt: number): number {
  const step = Math.max(0, Math.floor(attempt) - 1);
  return Math.min(FEED_CAP_MS, FEED_BASE_MS * 2 ** Math.min(step, 16));
}

export interface FeedEvent {
  type: string;
  session_id?: string;
  [field: string]: unknown;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The event a frame carries, or null for anything that is not one. */
export function parseFeedFrame(text: string): FeedEvent | null {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return null;
  }
  if (!isRecord(value)) return null;
  if (typeof value.type === "string" && value.type) return value as FeedEvent;
  if (typeof value.method === "string" && value.method && isRecord(value.params)) {
    return { ...value.params, type: value.method };
  }
  return null;
}

export type FeedPhase = "connecting" | "live" | "retrying" | "stopped";

export interface FeedStatus {
  phase: FeedPhase;
  /** Plain-language line for the rail; absent while all is well. */
  notice?: string;
  /** True when the notice asks for a page reload rather than a reconnect. */
  reload?: boolean;
}

export interface FeedListener {
  event(event: FeedEvent): void;
  status(status: FeedStatus): void;
}

export interface FeedDeps {
  target(path: string, purpose: string, params: Record<string, string>): Promise<WsTarget>;
  socket(url: string, protocols: string[]): PtySocket;
}

export const browserFeedDeps: FeedDeps = {
  target: (path, purpose, params) => wsTarget(path, purpose, params),
  socket: (url, protocols) => new WebSocket(url, protocols),
};

const OPEN = 1;
const STOP_CODES = new Set([4400, 4401, 4403]);

export class EventsFeed {
  readonly #channel: string;
  readonly #listener: FeedListener;
  readonly #deps: FeedDeps;
  #socket: PtySocket | null = null;
  #generation = 0;
  #attempts = 0;
  #running = false;
  #retry: ReturnType<typeof setTimeout> | null = null;
  #deadline: ReturnType<typeof setTimeout> | null = null;

  constructor(channel: string, listener: FeedListener, deps: FeedDeps = browserFeedDeps) {
    this.#channel = channel;
    this.#listener = listener;
    this.#deps = deps;
  }

  /** Attempts used in the current outage. */
  get attempts(): number {
    return this.#attempts;
  }

  start(): void {
    if (this.#running) return;
    this.#running = true;
    this.#attempts = 0;
    this.#listener.status({ phase: "connecting" });
    void this.#dial();
  }

  /** Manual reconnect after the feed stopped. */
  reconnect(): void {
    this.stop();
    this.start();
  }

  stop(): void {
    this.#running = false;
    this.#generation += 1;
    this.#clearTimers();
    this.#drop();
  }

  async #dial(): Promise<void> {
    const generation = ++this.#generation;
    this.#clearTimers();
    this.#deadline = setTimeout(() => {
      this.#deadline = null;
      if (generation !== this.#generation) return;
      // Ticket or handshake took too long: abandon the attempt.
      this.#generation += 1;
      this.#drop();
      this.#failed();
    }, FEED_CONNECT_TIMEOUT_MS);

    let target: WsTarget;
    try {
      target = await this.#deps.target(EVENTS_PATH, EVENTS_PURPOSE, { channel: this.#channel });
    } catch {
      if (generation !== this.#generation) return;
      this.#clearTimers();
      this.#failed();
      return;
    }
    if (generation !== this.#generation || !this.#running) return;
    const socket = this.#deps.socket(target.url, target.protocols);
    this.#socket = socket;
    socket.onopen = () => {
      if (generation !== this.#generation) return;
      this.#clearTimers();
      this.#attempts = 0;
      this.#listener.status({ phase: "live" });
    };
    socket.onmessage = (message) => {
      if (generation !== this.#generation || typeof message.data !== "string") return;
      const event = parseFeedFrame(message.data);
      if (event) this.#listener.event(event);
    };
    socket.onclose = (close) => {
      if (generation !== this.#generation) return;
      this.#socket = null;
      this.#clearTimers();
      // biome-ignore lint/suspicious/noConsole: close codes are for diagnosis, never in the copy
      console.warn(`[indus chat] event feed closed: code=${close.code}`);
      if (STOP_CODES.has(close.code)) {
        this.#running = false;
        const auth = close.code !== 4400;
        this.#listener.status({
          phase: "stopped",
          notice: auth ? FEED_COPY.loginExpired : FEED_COPY.unavailable,
          reload: auth,
        });
        return;
      }
      this.#failed();
    };
    socket.onerror = () => {
      // the close event follows
    };
  }

  #failed(): void {
    if (!this.#running) return;
    if (this.#attempts >= FEED_ATTEMPTS) {
      this.#running = false;
      this.#listener.status({ phase: "stopped", notice: FEED_COPY.gaveUp, reload: false });
      return;
    }
    this.#attempts += 1;
    const delay = feedDelay(this.#attempts);
    this.#listener.status({ phase: "retrying", notice: FEED_COPY.retrying(delay) });
    this.#retry = setTimeout(() => {
      this.#retry = null;
      if (this.#running) void this.#dial();
    }, delay);
  }

  #drop(): void {
    const socket = this.#socket;
    this.#socket = null;
    if (!socket) return;
    socket.onopen = null;
    socket.onmessage = null;
    socket.onclose = null;
    socket.onerror = null;
    if (socket.readyState <= OPEN) {
      try {
        socket.close(1000, "feed closed");
      } catch {
        // already closing
      }
    }
  }

  #clearTimers(): void {
    if (this.#retry !== null) clearTimeout(this.#retry);
    if (this.#deadline !== null) clearTimeout(this.#deadline);
    this.#retry = null;
    this.#deadline = null;
  }
}
