// The one seam between the bridge and a WhatsApp Web library.
//
// The session (session.ts) talks only to `WaSocket`; a provider builds one. No provider ships with
// this package and none is a dependency: the operator supplies a module that exports
// `createWaSocket` (a `WaProvider`) and names it with INDUS_WA_PROVIDER (an absolute module path or
// a package name). Without one the bridge refuses to start (main.ts).

import { isAbsolute } from "node:path";
import { pathToFileURL } from "node:url";

export type MediaKind = "image" | "video" | "audio" | "voice" | "document" | "sticker";
export type OutboundKind = "image" | "video" | "audio" | "voice" | "document";

export interface SelfIds {
  jid: string;
  lid: string | null;
}

export type ConnectionUpdate =
  | { state: "connecting" }
  | { state: "open"; self: SelfIds }
  | { state: "qr"; qr: string }
  | { state: "closed"; loggedOut: boolean; reason: string };

export interface RawMedia {
  kind: MediaKind;
  mime: string;
  name: string;
  size: number | null;
  download(): Promise<Uint8Array>;
}

/** One message as the library delivered it, reduced to what the bridge reads. */
export interface RawMessage {
  id: string;
  chat: string;
  fromMe: boolean;
  /** The sender inside a group (null in a direct chat). */
  participant: string | null;
  /** The sender's other address (phone JID ↔ LID) when the library knows it. */
  senderAlt: string | null;
  pushName: string;
  text: string;
  /** Seconds since the epoch. */
  timestamp: number;
  mentions: string[];
  quoted: { id: string; participant: string | null; text: string } | null;
  media: RawMedia | null;
  /** Set when this message edits an earlier one. */
  editOf: string | null;
}

export interface MessageRef {
  id: string;
  fromMe: boolean;
  participant: string | null;
}

export interface OutboundMedia {
  kind: OutboundKind;
  path: string;
  caption?: string;
  fileName?: string;
  mime?: string;
}

export interface WaSocket {
  /** Returns the new message's id. */
  sendText(chat: string, text: string, quoted?: MessageRef): Promise<string>;
  sendMedia(chat: string, media: OutboundMedia, quoted?: MessageRef): Promise<string>;
  presence(chat: string, state: "composing" | "paused"): Promise<void>;
  react(chat: string, target: MessageRef, emoji: string): Promise<void>;
  markRead(chat: string, target: MessageRef): Promise<void>;
  requestPairingCode(phone: string): Promise<string>;
  close(): Promise<void>;
}

export interface SocketHandlers {
  onUpdate(update: ConnectionUpdate): void;
  onMessages(messages: RawMessage[]): void;
}

export interface ProviderOptions {
  authDir: string;
  handlers: SocketHandlers;
}

export type WaProvider = (options: ProviderOptions) => Promise<WaSocket>;

export type WaErrorCode =
  | "not_connected"
  | "chat_not_found"
  | "message_not_found"
  | "forbidden"
  | "rate_limited"
  | "too_large"
  | "timeout"
  | "bad_request"
  | "failed";

/** A library failure with our code; `detail` never holds message text or a number. */
export class WaError extends Error {
  readonly code: WaErrorCode;
  readonly retryAfter: number | null;

  constructor(code: WaErrorCode, detail = "", retryAfter: number | null = null) {
    super(detail || code);
    this.name = "WaError";
    this.code = code;
    this.retryAfter = retryAfter;
  }
}

// -- loading -----------------------------------------------------------------------------------

/** No provider was named: the bridge has nothing to reach WhatsApp with. */
export class ProviderMissing extends Error {
  constructor() {
    super("no WhatsApp provider is configured (INDUS_WA_PROVIDER)");
    this.name = "ProviderMissing";
  }
}

/** The module URL or name to import for `spec`. */
export function resolveProvider(spec: string): string {
  const text = spec.trim();
  if (!text) throw new ProviderMissing();
  return isAbsolute(text) ? pathToFileURL(text).href : text;
}

export async function loadProvider(spec: string): Promise<WaProvider> {
  const loaded = (await import(resolveProvider(spec))) as Record<string, unknown>;
  return providerFrom(loaded);
}

export function providerFrom(loaded: Record<string, unknown>): WaProvider {
  const own = loaded.createWaSocket;
  if (typeof own === "function") return own as WaProvider;
  throw new Error("the provider module does not export createWaSocket");
}
