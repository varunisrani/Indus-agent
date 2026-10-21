// The chat terminal's socket to `/api/pty` (P18-26), without React or xterm.
//
// `PtyConnection` dials, keeps and redials one PTY socket and reports to its owner through
// `PtyListener`: terminal text to write, the phase to show, an announced resume id, notices.
//
// One dial: `channel`, `attach`, `resume`, `profile`, `cwd` (only without a resume), `fresh=1`
// (only on the first dial after "new chat") go in the query; the credential goes only in the
// subprotocol list (`lib/wsUrl.ts`). From the start of the dial — a gated ticket fetch included —
// the socket has 8 s to open; past that the attempt is abandoned as failed. Once open, the in-band
// resize escape is sent at once (the TUI needs a size for its first paint), again after two
// animation frames (layout settled) and when fonts have loaded (cell size changed), and after any
// 20 s without a frame in either direction (a keep-alive the server consumes).
//
// Text frames are terminal text, except the server's `{"type":"resume","id":…}` announcement of
// an implicit resume. Binary frames are UTF-8 decoded as a stream. While a resume replays
// (explicit or announced) the text goes through the `ResumeSanitizer` for 30 s.
//
// After a close the verdict (`reconnect.ts`) decides: reload once, show copy, show an overlay, stay
// quiet, or climb the ladder. Redials never run while the page is hidden or the chat is not the
// current page: the connection parks and dials once when both are true again, at most once per
// second. Redials repeat the first dial's query without `fresh`, so the server's keep-alive key is
// the same and it reattaches the same PTY (ring replay, then a redraw).

import { TOKEN_RELOAD_KEY } from "../lib/api.ts";
import { authRequired } from "../lib/bootstrap.ts";
import { type WsTarget, wsTarget } from "../lib/wsUrl.ts";
import {
  CLOSE,
  type CloseVerdict,
  COPY,
  judgeClose,
  KEEPALIVE_MS,
  OPEN_DEADLINE_MS,
  RESUME_THROTTLE_MS,
  ReconnectLadder,
} from "./reconnect.ts";
import { ResumeSanitizer } from "./resumeSanitizer.ts";

export const PTY_PATH = "/api/pty";
export const PTY_PURPOSE = "web-pty";

const SOCKET_CONNECTING = 0;
const SOCKET_OPEN = 1;

/** What the server needs to pick (or start) the PTY. */
export interface PtyRequest {
  channel: string;
  attach: string;
  resume?: string;
  profile?: string;
  /** Folder for a fresh chat; ignored (not sent) with a resume. */
  cwd?: string;
  /** Start a new conversation in this channel (drops its remembered session). */
  fresh?: boolean;
}

/** The query of one dial; `first` is false for redials. */
export function ptyParams(request: PtyRequest, first: boolean): Record<string, string> {
  const params: Record<string, string> = { channel: request.channel, attach: request.attach };
  if (request.resume) params.resume = request.resume;
  if (request.profile) params.profile = request.profile;
  if (!request.resume && request.cwd) params.cwd = request.cwd;
  if (first && request.fresh && !request.resume) params.fresh = "1";
  return params;
}

/** The exact frame the server consumes as a size change (never written to the child). */
export function resizeFrame(cols: number, rows: number): string {
  return `\x1b[RESIZE:${Math.max(1, Math.floor(cols))};${Math.max(1, Math.floor(rows))}]`;
}

/** The id of a `{"type":"resume","id":…}` control frame, else null. */
export function resumeAnnouncement(text: string): string | null {
  if (!text.startsWith("{")) return null;
  try {
    const value: unknown = JSON.parse(text);
    if (typeof value !== "object" || value === null) return null;
    const record = value as { type?: unknown; id?: unknown };
    return record.type === "resume" && typeof record.id === "string" && record.id
      ? record.id
      : null;
  } catch {
    return null;
  }
}

export type PtyPhase =
  /** Dialling (first time or after a manual reconnect). */
  | "connecting"
  | "open"
  /** Waiting for the next automatic attempt. */
  | "reconnecting"
  /** Dropped while hidden or on another page; dials when the chat is visible again. */
  | "paused"
  /** Not dialling any more: refused, superseded, or the ladder ran out (see `PtyStatus.stop`). */
  | "stopped"
  /** The TUI process ended or could not start; a new session is needed. */
  | "ended";

export type PtyStop =
  | { kind: "gave-up" }
  | { kind: "superseded" }
  | { kind: "rejected"; message: string; reload: boolean }
  | { kind: "exited" }
  | { kind: "start-failed" };

export interface PtyStatus {
  phase: PtyPhase;
  /** Why it stopped or ended. */
  stop?: PtyStop;
}

export interface PtyListener {
  output(text: string): void;
  status(status: PtyStatus): void;
  /** The server reopened the channel's last session on its own (implicit resume). */
  resumed?(id: string): void;
  /** One-line notices (input refused while not connected). */
  notice?(text: string): void;
}

/** The minimum of `WebSocket` the connection uses (the fake server implements it too). */
export interface PtySocket {
  readonly readyState: number;
  binaryType: BinaryType;
  onopen: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent) => void) | null;
  onclose: ((event: CloseEvent) => void) | null;
  onerror: ((event: Event) => void) | null;
  send(data: string): void;
  close(code?: number, reason?: string): void;
}

export interface PtyDeps {
  target(path: string, purpose: string, params: Record<string, string>): Promise<WsTarget>;
  socket(url: string, protocols: string[]): PtySocket;
  /** Run `callback` on the next animation frame. */
  frame(callback: () => void): void;
  /** Settles when the page's fonts have loaded. */
  fontsReady(): Promise<unknown>;
  /** Reload the page if the one allowed loopback reload has not happened; false otherwise. */
  reloadOnce(): boolean;
  /** Whether the one reload is still available (read before deciding). */
  mayReload(): boolean;
  gated(): boolean;
  hidden(): boolean;
  now(): number;
}

function storage(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

export const browserPtyDeps: PtyDeps = {
  target: (path, purpose, params) => wsTarget(path, purpose, params),
  socket: (url, protocols) => new WebSocket(url, protocols),
  frame: (callback) => {
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => callback());
    else setTimeout(callback, 16);
  },
  fontsReady: () => document.fonts?.ready ?? Promise.resolve(),
  mayReload: () => {
    const store = storage();
    if (!store) return false;
    try {
      return store.getItem(TOKEN_RELOAD_KEY) === null;
    } catch {
      return false;
    }
  },
  reloadOnce: () => {
    const store = storage();
    try {
      if (!store || store.getItem(TOKEN_RELOAD_KEY) !== null) return false;
      store.setItem(TOKEN_RELOAD_KEY, String(Date.now()));
    } catch {
      return false;
    }
    window.location.reload();
    return true;
  },
  gated: authRequired,
  hidden: () => typeof document !== "undefined" && document.visibilityState === "hidden",
  now: () => Date.now(),
};

type Timer = ReturnType<typeof setTimeout>;

export class PtyConnection {
  readonly #deps: PtyDeps;
  readonly #listener: PtyListener;
  readonly #size: () => { cols: number; rows: number };
  readonly #ladder = new ReconnectLadder();
  readonly #sanitizer: ResumeSanitizer;

  #request: PtyRequest | null = null;
  #socket: PtySocket | null = null;
  /** Bumped by every dial and by dispose; a stale socket's or ticket's callbacks see a mismatch. */
  #generation = 0;
  #status: PtyStatus = { phase: "stopped" };
  #first = true;
  #active = true;
  #disposed = false;
  #lastResumeAt = Number.NEGATIVE_INFINITY;
  #noticeShown = false;
  #decoder = new TextDecoder();

  #deadline: Timer | null = null;
  #retry: Timer | null = null;
  #keepalive: Timer | null = null;
  #window: Timer | null = null;

  constructor(
    listener: PtyListener,
    size: () => { cols: number; rows: number },
    deps: PtyDeps = browserPtyDeps,
  ) {
    this.#listener = listener;
    this.#size = size;
    this.#deps = deps;
    this.#sanitizer = new ResumeSanitizer(() => deps.now());
  }

  get status(): PtyStatus {
    return this.#status;
  }

  get request(): PtyRequest | null {
    return this.#request;
  }

  /** Start a new connection for `request` (drops the current socket without reacting to it). */
  connect(request: PtyRequest): void {
    if (this.#disposed) return;
    this.#request = { ...request };
    this.#first = true;
    this.#ladder.reset();
    this.#dropSocket();
    this.#clear("retry");
    this.#setStatus({ phase: "connecting" });
    void this.#dial();
  }

  /** Manual reconnect (after the ladder stopped, or "try again"): same request, fresh ladder. */
  reconnect(): void {
    if (this.#disposed || !this.#request) return;
    this.#ladder.reset();
    this.#dropSocket();
    this.#clear("retry");
    this.#setStatus({ phase: "connecting" });
    void this.#dial();
  }

  /** Whether the chat is the page on screen; leaving parks a pending redial. */
  setActive(active: boolean): void {
    this.#active = active;
    if (active) this.pageResumed();
    else if (this.#status.phase === "reconnecting" && this.#retry !== null) this.#park();
  }

  /**
   * The page came back (visible, focus, online) or the chat became the current page: a parked
   * connection dials again, at most once per `RESUME_THROTTLE_MS` (a burst of such events makes one
   * dial; one inside the throttle is deferred to its end, not dropped).
   */
  pageResumed(): void {
    if (this.#disposed || !this.#request || !this.#mayDial()) return;
    if (this.#status.phase !== "paused" || this.#retry !== null) return;
    const wait = this.#lastResumeAt + RESUME_THROTTLE_MS - this.#deps.now();
    if (wait > 0) {
      this.#retry = setTimeout(() => {
        this.#retry = null;
        this.#resumeDial();
      }, wait);
      return;
    }
    this.#resumeDial();
  }

  #resumeDial(): void {
    if (!this.#mayDial()) {
      this.#park();
      return;
    }
    this.#lastResumeAt = this.#deps.now();
    this.#setStatus({ phase: "reconnecting" });
    void this.#dial();
  }

  /** Send keystrokes; false (with one notice per outage) when not connected. */
  send(data: string): boolean {
    const socket = this.#socket;
    if (this.#status.phase !== "open" || !socket || socket.readyState !== SOCKET_OPEN) {
      if (!this.#noticeShown) {
        this.#noticeShown = true;
        this.#listener.notice?.(COPY.inputRefused);
      }
      return false;
    }
    socket.send(data);
    this.#touch();
    return true;
  }

  /** Tell the server the terminal's current size (no-op while not open). */
  sendSize(): void {
    const socket = this.#socket;
    if (!socket || socket.readyState !== SOCKET_OPEN) return;
    const { cols, rows } = this.#size();
    socket.send(resizeFrame(cols, rows));
    this.#touch();
  }

  dispose(): void {
    this.#disposed = true;
    this.#generation += 1;
    this.#dropSocket();
    for (const name of ["deadline", "retry", "keepalive", "window"] as const) this.#clear(name);
  }

  // -- dialling ---------------------------------------------------------------------------------

  #mayDial(): boolean {
    return this.#active && !this.#deps.hidden();
  }

  async #dial(): Promise<void> {
    const request = this.#request;
    if (!request) return;
    this.#dropSocket();
    const generation = ++this.#generation;
    const params = ptyParams(request, this.#first);
    this.#clear("deadline");
    this.#deadline = setTimeout(() => this.#onDeadline(generation), OPEN_DEADLINE_MS);

    let target: WsTarget;
    try {
      target = await this.#deps.target(PTY_PATH, PTY_PURPOSE, params);
    } catch (error) {
      if (generation !== this.#generation) return;
      // biome-ignore lint/suspicious/noConsole: diagnosis without server access; no credential in it
      console.warn("[indus chat] could not prepare the chat connection", error);
      this.#clear("deadline");
      this.#afterFailure();
      return;
    }
    if (generation !== this.#generation || this.#disposed) return;

    const socket = this.#deps.socket(target.url, target.protocols);
    socket.binaryType = "arraybuffer";
    this.#socket = socket;
    this.#decoder = new TextDecoder();
    socket.onopen = () => {
      if (generation === this.#generation) this.#onOpen(socket, request);
    };
    socket.onmessage = (event) => {
      if (generation === this.#generation) this.#onMessage(event.data);
    };
    socket.onclose = (event) => {
      if (generation === this.#generation) this.#onClose(event.code, event.wasClean, event.reason);
    };
    socket.onerror = () => {
      // The close event that follows carries the code; nothing to do here.
    };
  }

  #onDeadline(generation: number): void {
    this.#deadline = null;
    if (generation !== this.#generation) return;
    const socket = this.#socket;
    if (socket && socket.readyState === SOCKET_CONNECTING) {
      // Stuck handshake: abandon it; its own close (if it ever comes) is ignored.
      this.#dropSocket();
      this.#afterFailure();
    } else if (!socket) {
      // The ticket never came: this attempt is over, a late ticket must not open a socket.
      this.#generation += 1;
      this.#afterFailure();
    }
  }

  #onOpen(socket: PtySocket, request: PtyRequest): void {
    this.#clear("deadline");
    this.#ladder.reset();
    this.#first = false;
    this.#noticeShown = false;
    this.#setStatus({ phase: "open" });
    this.sendSize();
    this.#deps.frame(() => this.#deps.frame(() => this.#sizeIfCurrent(socket)));
    void this.#deps.fontsReady().then(
      () => this.#sizeIfCurrent(socket),
      () => undefined,
    );
    if (request.resume) this.#beginReplay();
  }

  #sizeIfCurrent(socket: PtySocket): void {
    if (this.#socket === socket) this.sendSize();
  }

  #onMessage(data: unknown): void {
    this.#touch();
    let text: string;
    if (typeof data === "string") {
      const announced = resumeAnnouncement(data);
      if (announced !== null) {
        this.#beginReplay();
        this.#listener.resumed?.(announced);
        return;
      }
      text = data;
    } else if (data instanceof ArrayBuffer) {
      text = this.#decoder.decode(new Uint8Array(data), { stream: true });
    } else if (ArrayBuffer.isView(data)) {
      text = this.#decoder.decode(data, { stream: true });
    } else {
      return;
    }
    const out = this.#sanitizer.active ? this.#sanitizer.feed(text) : text;
    if (out) this.#listener.output(out);
  }

  #beginReplay(): void {
    this.#sanitizer.begin();
    this.#clear("window");
    this.#window = setTimeout(() => {
      this.#window = null;
      const released = this.#sanitizer.settle();
      if (released) this.#listener.output(released);
    }, this.#sanitizer.remainingMs);
  }

  #onClose(code: number, clean: boolean, reason: string): void {
    this.#socket = null;
    this.#clear("deadline");
    this.#clear("keepalive");
    this.#clear("window");
    const tail = this.#sanitizer.end();
    if (tail) this.#listener.output(tail);
    // biome-ignore lint/suspicious/noConsole: close codes go to the console, never into the copy
    console.warn(`[indus chat] terminal socket closed: code=${code}${reason ? ` ${reason}` : ""}`);

    const verdict: CloseVerdict = judgeClose(code, clean, {
      gated: this.#deps.gated(),
      mayReload: this.#deps.mayReload(),
    });
    switch (verdict.kind) {
      case "reload":
        if (this.#deps.reloadOnce()) {
          this.#setStatus({ phase: "stopped" });
          return;
        }
        this.#setStatus({
          phase: "stopped",
          stop: { kind: "rejected", message: COPY.loginExpired, reload: true },
        });
        return;
      case "rejected":
        this.#setStatus({ phase: "stopped", stop: verdict });
        return;
      case "superseded":
        this.#setStatus({ phase: "stopped", stop: { kind: "superseded" } });
        return;
      case "start-failed":
        this.#setStatus({ phase: "ended", stop: { kind: "start-failed" } });
        return;
      case "exited":
        if (code === CLOSE.exited || code === CLOSE.normal) {
          this.#listener.output(`\r\n\x1b[2m${COPY.endedLine}\x1b[0m\r\n`);
        }
        this.#setStatus({ phase: "ended", stop: { kind: "exited" } });
        return;
      case "retry":
        this.#afterFailure();
        return;
    }
  }

  /** A transient failure: park, climb or stop. */
  #afterFailure(): void {
    if (this.#disposed) return;
    if (!this.#mayDial()) {
      this.#park();
      return;
    }
    const delay = this.#ladder.climb();
    if (delay === null) {
      this.#setStatus({ phase: "stopped", stop: { kind: "gave-up" } });
      return;
    }
    this.#setStatus({ phase: "reconnecting" });
    this.#clear("retry");
    this.#retry = setTimeout(() => {
      this.#retry = null;
      if (!this.#mayDial()) {
        this.#park();
        return;
      }
      void this.#dial();
    }, delay);
  }

  #park(): void {
    this.#clear("retry");
    this.#setStatus({ phase: "paused" });
  }

  // -- keep-alive and housekeeping --------------------------------------------------------------

  /** Some frame went either way: the keep-alive waits another full period of silence. */
  #touch(): void {
    this.#clear("keepalive");
    if (!this.#socket || this.#socket.readyState !== SOCKET_OPEN) return;
    this.#keepalive = setTimeout(() => {
      this.#keepalive = null;
      this.sendSize();
    }, KEEPALIVE_MS);
  }

  #dropSocket(): void {
    const socket = this.#socket;
    this.#socket = null;
    this.#clear("keepalive");
    if (!socket) return;
    socket.onopen = null;
    socket.onmessage = null;
    socket.onclose = null;
    socket.onerror = null;
    try {
      socket.close(1000, "client closed");
    } catch {
      // already closing
    }
  }

  #clear(name: "deadline" | "retry" | "keepalive" | "window"): void {
    switch (name) {
      case "deadline":
        if (this.#deadline !== null) clearTimeout(this.#deadline);
        this.#deadline = null;
        return;
      case "retry":
        if (this.#retry !== null) clearTimeout(this.#retry);
        this.#retry = null;
        return;
      case "keepalive":
        if (this.#keepalive !== null) clearTimeout(this.#keepalive);
        this.#keepalive = null;
        return;
      case "window":
        if (this.#window !== null) clearTimeout(this.#window);
        this.#window = null;
        return;
    }
  }

  #setStatus(status: PtyStatus): void {
    this.#status = status;
    if (!this.#disposed) this.#listener.status(status);
  }
}
