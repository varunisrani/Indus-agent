// The WhatsApp Web session behind the bridge: connection state, inbound filtering and the event
// ring the adapter long-polls, and the outbound calls with their typed failures.

import { randomBytes } from "node:crypto";
import { lstat, realpath } from "node:fs/promises";
import { isAbsolute, sep } from "node:path";
import { isBroadcast, isGroup, sameUser, stripDevice } from "./jid.ts";
import {
  type ConnectionUpdate,
  type MediaKind,
  type MessageRef,
  type OutboundMedia,
  type RawMedia,
  type RawMessage,
  type SelfIds,
  WaError,
  type WaProvider,
  type WaSocket,
} from "./provider.ts";

export type Status = "starting" | "connecting" | "open" | "pairing" | "logged_out" | "closed";

export interface MediaItem {
  ref: string;
  kind: MediaKind;
  mime: string;
  name: string;
  size: number | null;
}

export interface BridgeEvent {
  seq: number;
  id: string;
  chat: string;
  sender: string;
  senderAlt: string | null;
  senderName: string;
  group: boolean;
  chatName: string;
  text: string;
  timestamp: number;
  fromMe: boolean;
  mentions: string[];
  mentionsMe: boolean;
  quoted: { id: string; sender: string | null; text: string; fromMe: boolean } | null;
  media: MediaItem[];
  edited: boolean;
}

export interface EventsPage {
  epoch: string;
  cursor: number;
  events: BridgeEvent[];
}

export type LifecycleEvent =
  | { event: "connected" }
  | { event: "qr"; qr: string }
  | { event: "pairing_code"; code: string }
  | { event: "pairing_required" }
  | { event: "logged_out" }
  | { event: "disconnected"; reason: string };

export interface SessionOptions {
  provider: WaProvider;
  authDir: string;
  /** Pass the owner's own messages in their own chat ("message yourself"). */
  selfChat?: boolean;
  mode?: "serve" | "pair";
  pairPhone?: string;
  notify?: (event: LifecycleEvent) => void;
  ringSize?: number;
  echoSize?: number;
  mediaKeep?: number;
  mediaTtlMs?: number;
  callTimeoutMs?: number;
  retryFirstMs?: number;
  retryMaxMs?: number;
  now?: () => number;
}

export const MAX_MEDIA_BYTES = 100 * 1024 * 1024;
export const MAX_TEXT = 65_536;
const PAGE = 100;

/** Our own recent message ids, oldest dropped first (echo filter). */
export class OutboundIds {
  private readonly ids = new Set<string>();
  private readonly cap: number;

  constructor(cap = 512) {
    if (!Number.isInteger(cap) || cap < 1) throw new RangeError("cap must be a positive integer");
    this.cap = cap;
  }

  remember(id: string): void {
    if (!id || this.ids.has(id)) return;
    this.ids.add(id);
    while (this.ids.size > this.cap) {
      const oldest = this.ids.values().next().value;
      if (oldest === undefined) break;
      this.ids.delete(oldest);
    }
  }

  has(id: string): boolean {
    return id !== "" && this.ids.has(id);
  }

  get size(): number {
    return this.ids.size;
  }
}

function inside(path: string, root: string): boolean {
  return path === root || path.startsWith(root.endsWith(sep) ? root : root + sep);
}

export class Session {
  readonly epoch = randomBytes(8).toString("hex");
  status: Status = "starting";
  self: SelfIds | null = null;

  private readonly options: SessionOptions;
  private readonly echoes: OutboundIds;
  private readonly ring: BridgeEvent[] = [];
  private readonly waiters = new Set<() => void>();
  private readonly mediaRefs = new Map<string, { media: RawMedia; at: number }>();
  private socket: WaSocket | null = null;
  private seq = 0;
  private stopping = false;
  private retryMs: number;
  private retryTimer: NodeJS.Timeout | null = null;
  private pairRequested = false;
  private inFlight = 0;
  private parked: RawMessage[] = [];
  private paired: (() => void) | null = null;

  constructor(options: SessionOptions) {
    this.options = options;
    this.echoes = new OutboundIds(options.echoSize ?? 512);
    this.retryMs = options.retryFirstMs ?? 1000;
  }

  private now(): number {
    return (this.options.now ?? Date.now)();
  }

  private notify(event: LifecycleEvent): void {
    this.options.notify?.(event);
  }

  get queued(): number {
    return this.ring.length;
  }

  /** Resolves once the account is linked (pair mode). */
  whenPaired(): Promise<void> {
    if (this.status === "open") return Promise.resolve();
    return new Promise((resolve) => {
      this.paired = resolve;
    });
  }

  async start(): Promise<void> {
    this.stopping = false;
    await this.connect();
  }

  private async connect(): Promise<void> {
    if (this.stopping) return;
    this.status = this.status === "pairing" ? "pairing" : "connecting";
    try {
      this.socket = await this.options.provider({
        authDir: this.options.authDir,
        handlers: {
          onUpdate: (update) => this.onUpdate(update),
          onMessages: (messages) => this.onMessages(messages),
        },
      });
    } catch (error) {
      this.socket = null;
      this.notify({
        event: "disconnected",
        reason: error instanceof WaError ? error.code : "start",
      });
      this.scheduleReconnect();
    }
  }

  private scheduleReconnect(): void {
    if (this.stopping || this.retryTimer !== null) return;
    const wait = this.retryMs;
    this.retryMs = Math.min(this.retryMs * 2, this.options.retryMaxMs ?? 30_000);
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      void this.connect();
    }, wait);
  }

  private onUpdate(update: ConnectionUpdate): void {
    if (this.stopping) return;
    switch (update.state) {
      case "connecting":
        if (this.status !== "pairing") this.status = "connecting";
        return;
      case "qr":
        this.status = "pairing";
        this.onPairing(update.qr);
        return;
      case "open":
        this.status = "open";
        this.self = { jid: stripDevice(update.self.jid), lid: update.self.lid };
        this.retryMs = this.options.retryFirstMs ?? 1000;
        this.notify({ event: "connected" });
        this.paired?.();
        this.paired = null;
        return;
      case "closed":
        this.socket = null;
        if (update.loggedOut) {
          this.status = "logged_out";
          this.notify({ event: "logged_out" });
          this.wakeAll();
          return;
        }
        if (this.status !== "pairing") this.status = "connecting";
        this.notify({ event: "disconnected", reason: update.reason });
        this.scheduleReconnect();
        return;
    }
  }

  private onPairing(qr: string): void {
    if (this.options.mode !== "pair") {
      this.notify({ event: "pairing_required" });
      return;
    }
    const phone = this.options.pairPhone;
    if (!phone) {
      this.notify({ event: "qr", qr });
      return;
    }
    if (this.pairRequested || this.socket === null) return;
    this.pairRequested = true;
    this.socket.requestPairingCode(phone).then(
      (code) => this.notify({ event: "pairing_code", code }),
      () => this.notify({ event: "disconnected", reason: "pairing_code" }),
    );
  }

  async stop(): Promise<void> {
    this.stopping = true;
    if (this.retryTimer !== null) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    const socket = this.socket;
    this.socket = null;
    this.status = "closed";
    this.wakeAll();
    if (socket !== null) {
      try {
        await socket.close();
      } catch {
        // closing is best effort
      }
    }
  }

  // -- inbound ---------------------------------------------------------------------------------

  private isSelf(jid: string | null | undefined): boolean {
    const self = this.self;
    return self !== null && (sameUser(jid, self.jid) || sameUser(jid, self.lid));
  }

  private onMessages(messages: RawMessage[]): void {
    for (const raw of messages) {
      if (raw.fromMe && this.inFlight > 0 && !this.echoes.has(raw.id)) {
        // One of our sends may not have returned its id yet: decide once it has.
        this.parked.push(raw);
        continue;
      }
      this.take(raw);
    }
  }

  private take(raw: RawMessage): void {
    const event = this.normalise(raw);
    if (event === null) return;
    this.ring.push(event);
    while (this.ring.length > (this.options.ringSize ?? 1000)) this.ring.shift();
    this.wakeAll();
  }

  /** One library message as a bridge event, or null when it is dropped (see the notes). */
  normalise(raw: RawMessage): BridgeEvent | null {
    const chat = stripDevice(raw.chat);
    if (!chat || isBroadcast(chat)) return null;
    if (raw.fromMe) {
      if (this.echoes.has(raw.id)) return null;
      if (!this.options.selfChat || !this.isSelf(chat)) return null;
    }
    const group = isGroup(chat);
    let sender: string;
    if (raw.fromMe) sender = this.self?.jid ?? chat;
    else if (group) sender = stripDevice(raw.participant ?? "");
    else sender = chat;
    if (!sender) return null;
    if (!raw.text && raw.media === null) return null;
    const media: MediaItem[] = [];
    if (raw.media !== null) media.push(this.keepMedia(raw.media));
    const quoted =
      raw.quoted === null
        ? null
        : {
            id: raw.quoted.id,
            sender: raw.quoted.participant ? stripDevice(raw.quoted.participant) : null,
            text: raw.quoted.text,
            fromMe: this.isSelf(raw.quoted.participant),
          };
    const mentions = raw.mentions.map(stripDevice);
    this.seq += 1;
    return {
      seq: this.seq,
      id: raw.editOf ?? raw.id,
      chat,
      sender,
      senderAlt: raw.senderAlt ? stripDevice(raw.senderAlt) : null,
      senderName: raw.pushName,
      group,
      chatName: group ? "" : raw.pushName,
      text: raw.text,
      timestamp: raw.timestamp,
      fromMe: raw.fromMe,
      mentions,
      mentionsMe: mentions.some((m) => this.isSelf(m)),
      quoted,
      media,
      edited: raw.editOf !== null,
    };
  }

  private keepMedia(media: RawMedia): MediaItem {
    const ref = randomBytes(12).toString("hex");
    const now = this.now();
    this.mediaRefs.set(ref, { media, at: now });
    const ttl = this.options.mediaTtlMs ?? 3_600_000;
    for (const [key, held] of this.mediaRefs) {
      if (this.mediaRefs.size <= (this.options.mediaKeep ?? 500) && now - held.at <= ttl) break;
      this.mediaRefs.delete(key);
    }
    return { ref, kind: media.kind, mime: media.mime, name: media.name, size: media.size };
  }

  /** The bytes of one attachment handed out in an event. */
  async media(ref: string): Promise<{ data: Uint8Array; mime: string; name: string }> {
    const held = this.mediaRefs.get(ref);
    const ttl = this.options.mediaTtlMs ?? 3_600_000;
    if (held === undefined || this.now() - held.at > ttl) {
      this.mediaRefs.delete(ref);
      throw new WaError("message_not_found", "media gone");
    }
    const { media } = held;
    if (media.size !== null && media.size > MAX_MEDIA_BYTES) throw new WaError("too_large");
    const data = await this.guard(media.download());
    if (data.byteLength > MAX_MEDIA_BYTES) throw new WaError("too_large");
    return { data, mime: media.mime, name: media.name };
  }

  private wakeAll(): void {
    const waiting = [...this.waiters];
    this.waiters.clear();
    for (const wake of waiting) wake();
  }

  /** Events after `after` (a cursor of this epoch), waiting up to `waitMs` for one. */
  async events(
    after: number,
    epoch: string,
    waitMs: number,
    signal?: AbortSignal,
  ): Promise<EventsPage> {
    const from = epoch === this.epoch ? after : 0;
    const pick = (): BridgeEvent[] => this.ring.filter((e) => e.seq > from).slice(0, PAGE);
    let found = pick();
    if (found.length === 0 && waitMs > 0 && !this.stopping && this.status !== "logged_out") {
      await new Promise<void>((resolve) => {
        const done = (): void => {
          clearTimeout(timer);
          this.waiters.delete(done);
          signal?.removeEventListener("abort", done);
          resolve();
        };
        const timer = setTimeout(done, waitMs);
        this.waiters.add(done);
        signal?.addEventListener("abort", done);
      });
      found = pick();
    }
    const last = found.at(-1);
    return {
      epoch: this.epoch,
      cursor: last === undefined ? Math.max(from, 0) : last.seq,
      events: found,
    };
  }

  // -- outbound --------------------------------------------------------------------------------

  private open(): WaSocket {
    if (this.status !== "open" || this.socket === null) throw new WaError("not_connected");
    return this.socket;
  }

  private guard<T>(work: Promise<T>): Promise<T> {
    const limit = this.options.callTimeoutMs ?? 60_000;
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => reject(new WaError("timeout", "no answer in time")), limit);
      work.then(
        (value) => {
          clearTimeout(timer);
          resolve(value);
        },
        (error: unknown) => {
          clearTimeout(timer);
          reject(error instanceof WaError ? error : new WaError("failed", "library error"));
        },
      );
    });
  }

  private async sending(work: (socket: WaSocket) => Promise<string>): Promise<string> {
    const socket = this.open();
    this.inFlight += 1;
    try {
      const id = await this.guard(work(socket));
      this.echoes.remember(id);
      return id;
    } finally {
      this.inFlight -= 1;
      if (this.inFlight === 0 && this.parked.length > 0) {
        const parked = this.parked;
        this.parked = [];
        for (const raw of parked) this.take(raw);
      }
    }
  }

  async sendText(chat: string, text: string, quoted?: MessageRef): Promise<string> {
    if (text.length > MAX_TEXT) throw new WaError("too_large", "text too long");
    return this.sending((socket) => socket.sendText(chat, text, quoted));
  }

  async sendMedia(chat: string, media: OutboundMedia, quoted?: MessageRef): Promise<string> {
    await this.checkPath(media.path);
    return this.sending((socket) => socket.sendMedia(chat, media, quoted));
  }

  /** Absolute, a regular file (not a link), ≤100 MiB and never inside the auth folder. */
  async checkPath(path: string): Promise<void> {
    if (!isAbsolute(path) || path.includes("\0")) throw new WaError("bad_request", "path");
    let info: Awaited<ReturnType<typeof lstat>>;
    try {
      info = await lstat(path);
    } catch {
      throw new WaError("bad_request", "file not found");
    }
    if (info.isSymbolicLink() || !info.isFile()) throw new WaError("bad_request", "not a file");
    if (info.size > MAX_MEDIA_BYTES) throw new WaError("too_large", "file too large");
    const real = await realpath(path);
    let auth = this.options.authDir;
    try {
      auth = await realpath(auth);
    } catch {
      // the folder may not exist yet; compare as given
    }
    if (inside(real, auth)) throw new WaError("forbidden", "refused path");
  }

  async typing(chat: string, state: "composing" | "paused"): Promise<void> {
    await this.guard(this.open().presence(chat, state));
  }

  async react(chat: string, target: MessageRef, emoji: string): Promise<void> {
    const socket = this.open();
    await this.guard(socket.react(chat, target, emoji));
  }

  async read(chat: string, target: MessageRef): Promise<void> {
    await this.guard(this.open().markRead(chat, target));
  }
}
