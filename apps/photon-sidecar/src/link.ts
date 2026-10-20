// The one seam between the sidecar and a Photon Spectrum client.
//
// Everything else in the sidecar talks to `SpectrumLink`. No Spectrum package ships with this
// workspace (its licences and availability are not cleared; docs/notes/P16/photon.md): the operator
// supplies a module that exports `createSpectrumLink` (a `LinkProvider`) and names it with
// INDUS_PHOTON_PROVIDER (an absolute module path or a package name). Without one the sidecar refuses
// to start (main.ts).

import { isAbsolute } from "node:path";
import { pathToFileURL } from "node:url";

/** The upstream connection as the provider sees it. */
export type LinkState = "connecting" | "open" | "degraded" | "auth_failed" | "closed";

export interface RawSpace {
  id: string;
  type: "dm" | "group";
  name?: string;
}

export interface RawPerson {
  id: string;
  name?: string;
}

export interface RawFile {
  type: "file";
  name: string;
  mime: string;
  size: number | null;
  /** An iMessage voice note rather than an audio file. */
  voice: boolean;
  read(): Promise<Uint8Array>;
}

export type RawContent =
  | { type: "text"; text: string }
  | RawFile
  | { type: "link"; url: string; title?: string; summary?: string }
  | { type: "group"; items: RawContent[] }
  | { type: "reaction"; target: string; emoji: string; targetFromUs: boolean; targetText?: string }
  | { type: "receipt"; target: string }
  | { type: "other"; name: string };

/** One message as the provider delivered it (the provider may deliver one more than once). */
export interface RawMessage {
  id: string;
  space: RawSpace;
  sender: RawPerson;
  /** Sent by this line (an echo of our own send). */
  fromUs: boolean;
  /** Milliseconds since the epoch. */
  at: number;
  content: RawContent;
  replyTo: { id: string; text?: string; fromUs?: boolean } | null;
}

export interface LinkHandlers {
  onState(state: LinkState, detail?: string): void;
  onMessage(message: RawMessage): void;
}

export interface LinkOptions {
  projectId: string;
  projectSecret: string;
  handlers: LinkHandlers;
}

export interface OutboundFile {
  path: string;
  voice: boolean;
  name?: string;
  mime?: string;
}

export interface SpectrumLink {
  /** Each send returns the new message's id. */
  sendText(space: string, text: string, format: "markdown" | "plain"): Promise<string>;
  sendLink(space: string, url: string): Promise<string>;
  sendFile(space: string, file: OutboundFile): Promise<string>;
  typing(space: string, on: boolean): Promise<void>;
  react(space: string, messageId: string, emoji: string): Promise<void>;
  unreact(space: string, messageId: string): Promise<void>;
  markRead(space: string, messageId: string): Promise<void>;
  close(): Promise<void>;
}

export type LinkProvider = (options: LinkOptions) => Promise<SpectrumLink>;

export type LinkErrorCode =
  | "not_connected"
  | "space_not_found"
  | "message_not_found"
  | "forbidden"
  | "target_not_allowed"
  | "auth_failed"
  | "rate_limited"
  | "too_large"
  | "timeout"
  | "bad_request"
  | "failed";

/** A provider failure with our code; `detail` never holds message text, a number or a secret. */
export class LinkError extends Error {
  readonly code: LinkErrorCode;
  readonly retryAfter: number | null;

  constructor(code: LinkErrorCode, detail = "", retryAfter: number | null = null) {
    super(detail || code);
    this.name = "LinkError";
    this.code = code;
    this.retryAfter = retryAfter;
  }
}

// -- loading -----------------------------------------------------------------------------------

/** No provider was named: the sidecar has nothing to reach Photon with. */
export class ProviderMissing extends Error {
  constructor() {
    super("no Photon provider is configured (INDUS_PHOTON_PROVIDER)");
    this.name = "ProviderMissing";
  }
}

/** The module URL or name to import for `spec`. */
export function resolveProvider(spec: string): string {
  const text = spec.trim();
  if (!text) throw new ProviderMissing();
  return isAbsolute(text) ? pathToFileURL(text).href : text;
}

export function providerFrom(loaded: Record<string, unknown>): LinkProvider {
  const make = loaded.createSpectrumLink;
  if (typeof make === "function") return make as LinkProvider;
  throw new Error("the provider module does not export createSpectrumLink");
}

export async function loadProvider(spec: string): Promise<LinkProvider> {
  const loaded = (await import(resolveProvider(spec))) as Record<string, unknown>;
  return providerFrom(loaded);
}
