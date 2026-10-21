// An in-memory stand-in for the dashboard's chat sockets (P18-26/27 tests). Nothing opens a port:
// `server.dial(url, protocols)` returns a `FakeSocket` with the browser `WebSocket` surface the
// chat code uses, and the test drives the server side (accept, emit PTY bytes, close with a code).
//
// It follows the contract of the real routes (src/indus/web/ws/*, src/indus/web/pty/*) closely
// enough to test the client against it:
// - credentials: only in the subprotocol list (`indus-dashboard-v1` plus
//   `indus-dashboard-token.<t>` or `indus-dashboard-ticket.<t>`); a `token`/`ticket`/`internal`
//   query parameter or a missing credential is accepted and closed 4401, as the helper does;
// - `/api/pty`: `fresh` drops the channel's remembered session; without `resume` (and not fresh)
//   a remembered session is announced with a `{"type":"resume","id":…}` text frame first; without
//   `attach` the PTY lives as long as its socket; with `attach` the keep-alive registry: key =
//   token + profile + explicit resume, other PTYs of the same token are ended (viewer closed
//   4409), an existing PTY is reattached (its old viewer closed 4409, the ring replayed, then one
//   Ctrl-L), a new one spawned; a detached PTY keeps collecting output; child exit closes the
//   viewer 4410; a frame that is exactly the resize escape is recorded, never "typed";
// - `/api/events`: each subscriber of a channel gets every published frame; what it sends is
//   ignored.

const PROTOCOL = "indus-dashboard-v1";
const CREDENTIAL = /^indus-dashboard-(token|ticket)\.(.+)$/;
// biome-ignore lint/suspicious/noControlCharactersInRegex: terminal streams are escape sequences
const RESIZE = /^\x1b\[RESIZE:(\d{1,9});(\d{1,9})\]$/;
const CHANNEL = /^[A-Za-z0-9._-]{1,128}$/;
const REDRAW = "\x0c";

export const SOCKET_STATES = { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 } as const;

const encoder = new TextEncoder();

function asBuffer(data: string | Uint8Array): ArrayBuffer {
  const bytes = typeof data === "string" ? encoder.encode(data) : data;
  const copy = new Uint8Array(bytes.byteLength);
  copy.set(bytes);
  return copy.buffer;
}

function closeEvent(code: number, reason: string, wasClean: boolean): CloseEvent {
  return { code, reason, wasClean, type: "close" } as unknown as CloseEvent;
}

function messageEvent(data: unknown): MessageEvent {
  return { data, type: "message" } as unknown as MessageEvent;
}

export class FakeSocket {
  readonly url: string;
  readonly path: string;
  readonly query: URLSearchParams;
  readonly protocols: string[];
  readyState: number = SOCKET_STATES.CONNECTING;
  binaryType: BinaryType = "blob";
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  /** Every frame the client sent, in order. */
  readonly sent: string[] = [];
  /** The close the client asked for, if it closed first. */
  clientClose: { code: number | undefined; reason: string | undefined } | null = null;
  /** The close the server sent, if it closed first. */
  serverCloseCode: number | null = null;
  /** Server-side hooks set by the route emulation. */
  onClientFrame: ((data: string) => void) | null = null;
  onClientGone: (() => void) | null = null;

  constructor(url: string, protocols: string[]) {
    this.url = url;
    const parsed = new URL(url);
    this.path = parsed.pathname;
    this.query = parsed.searchParams;
    this.protocols = [...protocols];
  }

  get open(): boolean {
    return this.readyState === SOCKET_STATES.OPEN;
  }

  /** The credential kind and value offered in the subprotocols, if any. */
  credential(): { kind: string; value: string } | null {
    for (const offered of this.protocols) {
      const found = CREDENTIAL.exec(offered);
      if (found?.[1] && found[2]) return { kind: found[1], value: found[2] };
    }
    return null;
  }

  // client side ---------------------------------------------------------------------------------

  send(data: string): void {
    if (this.readyState !== SOCKET_STATES.OPEN)
      throw new Error("send on a socket that is not open");
    this.sent.push(data);
    this.onClientFrame?.(data);
  }

  close(code?: number, reason?: string): void {
    if (this.readyState === SOCKET_STATES.CLOSED) return;
    this.clientClose = { code, reason };
    this.readyState = SOCKET_STATES.CLOSED;
    this.onClientGone?.();
    this.onclose?.(closeEvent(code ?? 1005, reason ?? "", true));
  }

  // server side ---------------------------------------------------------------------------------

  /** Complete the handshake. */
  accept(): void {
    if (this.readyState !== SOCKET_STATES.CONNECTING) return;
    this.readyState = SOCKET_STATES.OPEN;
    this.onopen?.({ type: "open" } as Event);
  }

  /** PTY output as a binary frame. */
  emit(data: string | Uint8Array): void {
    if (this.readyState !== SOCKET_STATES.OPEN) return;
    this.onmessage?.(messageEvent(asBuffer(data)));
  }

  /** A text frame (control frames, red error text, feed events). */
  emitText(text: string): void {
    if (this.readyState !== SOCKET_STATES.OPEN) return;
    this.onmessage?.(messageEvent(text));
  }

  /** The server closes the socket with `code`. */
  serverClose(code: number, reason = "", wasClean = true): void {
    if (this.readyState === SOCKET_STATES.CLOSED) return;
    this.serverCloseCode = code;
    this.readyState = SOCKET_STATES.CLOSED;
    this.onClientGone?.();
    this.onclose?.(closeEvent(code, reason, wasClean));
  }

  /** The connection died without a close frame (network loss): 1006, not clean. */
  drop(): void {
    this.serverClose(1006, "", false);
  }

  /** Frames the client sent that were not resize escapes. */
  input(): string[] {
    return this.sent.filter((frame) => !RESIZE.test(frame));
  }

  /** Sizes the client reported, in order. */
  resizes(): Array<{ cols: number; rows: number }> {
    const out: Array<{ cols: number; rows: number }> = [];
    for (const frame of this.sent) {
      const found = RESIZE.exec(frame);
      if (found) out.push({ cols: Number(found[1]), rows: Number(found[2]) });
    }
    return out;
  }
}

/** One PTY: its output ring and the socket watching it. */
export class FakePty {
  ring = "";
  viewer: FakeSocket | null = null;
  readonly typed: string[] = [];
  alive = true;
  readonly key: string | null;
  readonly spawnedBy: FakeSocket;

  constructor(key: string | null, spawnedBy: FakeSocket) {
    this.key = key;
    this.spawnedBy = spawnedBy;
  }

  /** The child wrote `text`: kept in the ring, forwarded to the viewer. */
  output(text: string): void {
    this.ring += text;
    this.viewer?.emit(text);
  }

  /** The child exited: the viewer is told 4410. */
  exit(): void {
    this.alive = false;
    this.viewer?.serverClose(4410, "the chat process exited");
    this.viewer = null;
  }
}

export interface FakeServerOptions {
  /** Complete handshakes on their own (on a microtask); default true. */
  autoAccept?: boolean;
}

export class FakePtyServer {
  readonly sockets: FakeSocket[] = [];
  readonly ptys: FakePty[] = [];
  /** Channel → the session its last PTY reported (the active-session file). */
  readonly channelFiles = new Map<string, string>();
  readonly #keepAlive = new Map<string, FakePty>();
  readonly #subscribers = new Map<string, Set<FakeSocket>>();
  autoAccept: boolean;

  constructor(options: FakeServerOptions = {}) {
    this.autoAccept = options.autoAccept ?? true;
  }

  /** Use as the socket factory: `new WebSocket(url, protocols)`. */
  readonly dial = (url: string, protocols: string[]): FakeSocket => {
    const socket = new FakeSocket(url, protocols);
    this.sockets.push(socket);
    if (this.autoAccept) queueMicrotask(() => this.accept(socket));
    return socket;
  };

  /** Sockets dialled to `path`, oldest first. */
  socketsAt(path: string): FakeSocket[] {
    return this.sockets.filter((socket) => socket.path === path);
  }

  /** The newest socket dialled to `path`. */
  last(path = "/api/pty"): FakeSocket {
    const found = this.socketsAt(path).at(-1);
    if (!found) throw new Error(`no socket dialled to ${path}`);
    return found;
  }

  /** The PTY the newest `/api/pty` socket is watching. */
  pty(): FakePty {
    const socket = this.last("/api/pty");
    const found = this.ptys.find((pty) => pty.viewer === socket);
    if (!found) throw new Error("the newest PTY socket watches no PTY");
    return found;
  }

  /** Publish one frame to every `/api/events` subscriber of `channel`. */
  publish(channel: string, frame: unknown): number {
    const text = typeof frame === "string" ? frame : JSON.stringify(frame);
    const subs = [...(this.#subscribers.get(channel) ?? [])];
    for (const socket of subs) socket.emitText(text);
    return subs.length;
  }

  /** Accept `socket` and run the route behind its path. */
  accept(socket: FakeSocket): void {
    if (socket.readyState !== SOCKET_STATES.CONNECTING) return;
    socket.accept();
    if (!this.#authorised(socket)) {
      socket.serverClose(4401, "credential refused");
      return;
    }
    if (socket.path.endsWith("/api/pty")) this.#servePty(socket);
    else if (socket.path.endsWith("/api/events")) this.#serveEvents(socket);
  }

  #authorised(socket: FakeSocket): boolean {
    for (const name of ["token", "ticket", "internal"]) {
      if (socket.query.has(name)) return false;
    }
    return socket.protocols.includes(PROTOCOL) && socket.credential() !== null;
  }

  #servePty(socket: FakeSocket): void {
    const query = socket.query;
    const fresh = ["1", "true", "yes", "on"].includes((query.get("fresh") ?? "").toLowerCase());
    const explicit = fresh ? "" : (query.get("resume") ?? "").trim();
    const channelRaw = query.get("channel") ?? "";
    const channel = CHANNEL.test(channelRaw) ? channelRaw : null;
    if (channel && fresh) this.channelFiles.delete(channel);
    if (channel && !explicit && !fresh) {
      const remembered = this.channelFiles.get(channel);
      if (remembered) socket.emitText(JSON.stringify({ type: "resume", id: remembered }));
    }

    const token = (query.get("attach") ?? "").trim();
    let pty: FakePty;
    if (!token) {
      pty = new FakePty(null, socket);
      this.ptys.push(pty);
      pty.viewer = socket;
      socket.onClientGone = () => {
        pty.viewer = null;
        pty.alive = false;
      };
    } else {
      const key = `${token}\u0000${query.get("profile") ?? ""}\u0000${explicit}`;
      for (const [other, stale] of this.#keepAlive) {
        if (other !== key && other.startsWith(`${token}\u0000`)) {
          this.#keepAlive.delete(other);
          stale.viewer?.serverClose(4409, "opened in another tab");
          stale.viewer = null;
          stale.alive = false;
        }
      }
      const existing = this.#keepAlive.get(key);
      if (existing?.alive) {
        pty = existing;
        const old = pty.viewer;
        if (old && old !== socket) old.serverClose(4409, "opened in another tab");
        pty.viewer = socket;
        if (pty.ring) socket.emit(pty.ring);
        socket.emit(REDRAW);
      } else {
        pty = new FakePty(key, socket);
        this.ptys.push(pty);
        this.#keepAlive.set(key, pty);
        pty.viewer = socket;
      }
      socket.onClientGone = () => {
        if (pty.viewer === socket) pty.viewer = null; // detach only
      };
    }
    socket.onClientFrame = (data) => {
      if (!RESIZE.test(data)) pty.typed.push(data);
    };
  }

  #serveEvents(socket: FakeSocket): void {
    const channel = socket.query.get("channel") ?? "";
    if (!CHANNEL.test(channel)) {
      socket.serverClose(4400, "bad channel");
      return;
    }
    const subs = this.#subscribers.get(channel) ?? new Set<FakeSocket>();
    subs.add(socket);
    this.#subscribers.set(channel, subs);
    socket.onClientGone = () => {
      subs.delete(socket);
      if (subs.size === 0) this.#subscribers.delete(channel);
    };
  }
}
