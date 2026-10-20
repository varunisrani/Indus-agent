// A Transport over one WebSocket to the daemon (P17-20). Browser-safe: it uses the host's WebSocket
// constructor (global in browsers and in Node 22+) or one the caller passes, nothing Node-only.
//
// The ticket travels only as a subprotocol (`indus-gateway-ticket.<t>`, single use, minted through
// the owner-only control socket); the URL must carry no user-info, query or fragment, and neither
// the subprotocols nor the URL's secret parts ever reach a log line or an event.

import type { CloseInfo, OutboundFrame, Transport, Unsubscribe } from "./channel.ts";
import { type Clock, systemClock, type TimerHandle } from "./clock.ts";
import { CLOSE_GRACE_MS, MAX_FRAME_LINE_BYTES } from "./limits.ts";
import type { TransportEvent } from "./localEvents.ts";
import { LogRing, redactUrls } from "./logRing.ts";
import { parseFrameLine, preview } from "./ndjson.ts";

/** Our protocol name, the only subprotocol the daemon may echo. */
export const GATEWAY_SUBPROTOCOL = "indus-gateway-v1";
/** The prefix of the single-use ticket subprotocol. */
export const TICKET_SUBPROTOCOL_PREFIX = "indus-gateway-ticket.";
/** How long the opening handshake may take (research 08 §3.9: connect 15,000 ms). */
export const WS_OPEN_TIMEOUT_MS = 15_000;

/** Close codes the daemon uses, with the reason a person reads. */
export const CLOSE_REASONS: Readonly<Record<number, string>> = {
  1000: "closed",
  1001: "the daemon is going away",
  1003: "unsupported data",
  1006: "connection lost",
  1009: "message too big",
  1011: "too slow to read (send deadline)",
  1013: "the daemon is busy; try again later",
  4401: "ticket refused (missing, used or expired)",
  4403: "forbidden (not a loopback peer or a disallowed origin)",
};

/** Close codes no retry can fix. */
export const TERMINAL_CLOSE_CODES: ReadonlySet<number> = new Set([4403]);

/** The parts of a WHATWG WebSocket this transport uses. */
export interface WebSocketLike {
  readonly readyState: number;
  readonly protocol: string;
  binaryType?: string;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  addEventListener(
    type: "open" | "message" | "close" | "error",
    listener: (event: never) => void,
  ): void;
}

export type WebSocketFactory = (url: string, protocols: string[]) => WebSocketLike;

export interface WsTransportOptions {
  /** `ws://127.0.0.1:<port>/ws` (or `wss:`): no user-info, query or fragment. */
  url: string;
  /** `indus-gateway-v1` first, then at most one ticket subprotocol. */
  protocols: readonly string[];
  /** Make the socket; default: the global `WebSocket`. */
  webSocket?: WebSocketFactory;
  clock?: Clock;
  /** Opening handshake deadline; default 15,000 ms. */
  openTimeoutMs?: number;
  /** How long `close()` waits for the close handshake. */
  closeGraceMs?: number;
  /** Largest inbound message accepted, in UTF-16 units (default 64 MiB). */
  maxMessageChars?: number;
  logs?: LogRing;
  onEvent?: (event: TransportEvent) => void;
}

/** Why a URL may not be dialled, or null when it may. */
export function attachUrlProblem(url: string): string | null {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return "not a URL";
  }
  if (parsed.protocol !== "ws:" && parsed.protocol !== "wss:") return "not a ws: or wss: URL";
  if (parsed.username || parsed.password) return "the URL carries user-info";
  if (parsed.search || url.includes("?")) return "the URL carries a query";
  if (parsed.hash || url.includes("#")) return "the URL carries a fragment";
  return null;
}

/** Why a subprotocol list may not be offered, or null when it may. */
export function protocolsProblem(protocols: readonly string[]): string | null {
  if (protocols[0] !== GATEWAY_SUBPROTOCOL)
    return `the first subprotocol must be ${GATEWAY_SUBPROTOCOL}`;
  const rest = protocols.slice(1);
  if (rest.length > 1) return "at most one ticket subprotocol";
  if (
    rest.some((p) => !p.startsWith(TICKET_SUBPROTOCOL_PREFIX) || p === TICKET_SUBPROTOCOL_PREFIX)
  ) {
    return "unexpected subprotocol";
  }
  return null;
}

/** A readable reason for a close code. */
export function closeReason(code: number, reason = ""): string {
  const known = CLOSE_REASONS[code] ?? "closed";
  const said = reason.trim();
  return said && said !== known ? `${known}: ${said} (${code})` : `${known} (${code})`;
}

const OPEN = 1;

function globalFactory(): WebSocketFactory | null {
  const ctor = (
    globalThis as { WebSocket?: new (url: string, protocols: string[]) => WebSocketLike }
  ).WebSocket;
  return ctor === undefined ? null : (url, protocols) => new ctor(url, protocols);
}

type Listener<T> = (value: T) => void;

export class WsTransport implements Transport {
  readonly logs: LogRing;
  private readonly options: WsTransportOptions;
  private readonly clock: Clock;
  private readonly shownUrl: string;
  private readonly frameListeners = new Set<Listener<unknown>>();
  private readonly closeListeners = new Set<Listener<CloseInfo>>();
  private socket: WebSocketLike | null = null;
  private opened = false;
  private closed = false;
  private openTimer: TimerHandle | undefined;
  private closing: Promise<void> | null = null;
  private readonly closeWaiters: Array<() => void> = [];

  constructor(options: WsTransportOptions) {
    this.options = options;
    this.clock = options.clock ?? systemClock;
    this.logs = options.logs ?? new LogRing();
    this.shownUrl = redactUrls(options.url);
    const problem = attachUrlProblem(options.url) ?? protocolsProblem(options.protocols);
    if (problem !== null) {
      this.log(`[lifecycle] not dialling ${this.shownUrl}: ${problem}`);
      this.finishSoon({ reason: `refused: ${problem}`, terminal: true });
      return;
    }
    const factory = options.webSocket ?? globalFactory();
    if (factory === null) {
      this.finishSoon({ reason: "refused: no WebSocket implementation", terminal: true });
      return;
    }
    let socket: WebSocketLike;
    try {
      socket = factory(options.url, [...options.protocols]);
    } catch (error) {
      // Only the error's name: its message may quote the subprotocols (the ticket).
      const why = error instanceof Error ? error.name : "error";
      this.log(`[lifecycle] could not dial ${this.shownUrl}: ${why}`);
      this.finishSoon({ reason: "dial failed" });
      return;
    }
    this.socket = socket;
    socket.binaryType = "arraybuffer";
    this.log(`[lifecycle] dialling ${this.shownUrl}`);
    socket.addEventListener("open", () => this.onOpen());
    socket.addEventListener("message", (event: { data: unknown }) => this.onMessage(event.data));
    socket.addEventListener("close", (event: { code?: number; reason?: string }) =>
      this.onClosed(event.code ?? 1006, event.reason ?? ""),
    );
    socket.addEventListener("error", () => this.log("[lifecycle] WebSocket error"));
    this.openTimer = this.clock.setTimeout(() => {
      this.openTimer = undefined;
      if (this.opened || this.closed) return;
      this.log(`[lifecycle] no answer from ${this.shownUrl} within the connect deadline`);
      this.abandon("connect timeout");
    }, options.openTimeoutMs ?? WS_OPEN_TIMEOUT_MS);
  }

  /** True between the accepted handshake and the close. */
  get open(): boolean {
    return this.opened && !this.closed;
  }

  get alive(): boolean {
    return !this.closed;
  }

  send(frame: OutboundFrame): void {
    const socket = this.socket;
    if (this.closed || !this.opened || socket === null || socket.readyState !== OPEN) {
      throw new Error("the WebSocket is not open");
    }
    socket.send(JSON.stringify(frame));
  }

  onFrame(listener: Listener<unknown>): Unsubscribe {
    this.frameListeners.add(listener);
    return () => this.frameListeners.delete(listener);
  }

  onClose(listener: Listener<CloseInfo>): Unsubscribe {
    this.closeListeners.add(listener);
    return () => this.closeListeners.delete(listener);
  }

  /** Close the socket (1000) and wait for the close handshake, at most the grace period. */
  close(): Promise<void> {
    if (this.closing) return this.closing;
    this.closing = new Promise<void>((resolve) => {
      if (this.closed) {
        resolve();
        return;
      }
      this.closeWaiters.push(resolve);
      try {
        this.socket?.close(1000, "closed by client");
      } catch {
        // already closing
      }
      this.clock.setTimeout(
        () => this.finish({ reason: "closed by client", code: 1000 }),
        this.options.closeGraceMs ?? CLOSE_GRACE_MS,
      );
    });
    return this.closing;
  }

  private onOpen(): void {
    if (this.closed) return;
    this.clearOpenTimer();
    const chosen = this.socket?.protocol ?? "";
    if (chosen !== GATEWAY_SUBPROTOCOL) {
      this.log(`[lifecycle] the server chose another subprotocol; closing`);
      this.abandon("unexpected subprotocol", true);
      return;
    }
    this.opened = true;
    this.log("[lifecycle] WebSocket open");
  }

  private onMessage(data: unknown): void {
    if (this.closed) return;
    if (typeof data !== "string") {
      this.protocolError("binary message ignored");
      return;
    }
    if (data.length > (this.options.maxMessageChars ?? MAX_FRAME_LINE_BYTES)) {
      this.protocolError(
        `message of ${data.length} characters dropped: ${preview(data.slice(0, 200))}`,
      );
      return;
    }
    const parsed = parseFrameLine(data);
    if (!parsed.ok) {
      this.protocolError(parsed.preview);
      return;
    }
    for (const listener of [...this.frameListeners]) listener(parsed.frame);
  }

  private onClosed(code: number, reason: string): void {
    const shown = closeReason(code, reason);
    this.log(`[lifecycle] WebSocket closed: ${shown}`);
    this.finish({ reason: shown, code, terminal: TERMINAL_CLOSE_CODES.has(code) });
  }

  /** Give up on the socket from our side and report the close at once. */
  private abandon(reason: string, terminal = false): void {
    try {
      this.socket?.close(1000, reason);
    } catch {
      // never opened
    }
    this.finish({ reason, ...(terminal ? { terminal } : {}) });
  }

  private protocolError(text: string): void {
    const shown = preview(redactUrls(text));
    this.log(`[protocol] not a frame: ${shown}`);
    this.options.onEvent?.({ type: "transport.protocol_error", preview: shown });
  }

  private finishSoon(info: CloseInfo): void {
    queueMicrotask(() => this.finish(info));
  }

  /** Report the close exactly once. */
  private finish(info: CloseInfo): void {
    if (this.closed) return;
    this.closed = true;
    this.clearOpenTimer();
    this.options.onEvent?.({
      type: "transport.closed",
      code: info.code ?? null,
      reason: info.reason,
      terminal: info.terminal === true,
    });
    for (const listener of [...this.closeListeners]) listener(info);
    for (const resolve of this.closeWaiters.splice(0)) resolve();
  }

  private clearOpenTimer(): void {
    if (this.openTimer !== undefined) this.clock.clearTimeout(this.openTimer);
    this.openTimer = undefined;
  }

  private log(line: string): void {
    this.logs.push(line);
  }
}
