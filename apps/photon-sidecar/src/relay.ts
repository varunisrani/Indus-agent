// What the sidecar knows between the provider and the HTTP API: the link state, the ring of recent
// events (the at-least-once stream replays from it) and the media refs handed out in events.
//
// Provider messages are reduced to our event shape here. Our own echoes (`fromUs`) and read receipts
// never become events; everything else does, in arrival order, numbered by `seq` within one run
// (`epoch`). The adapter dedupes by `id`: a provider that delivers a message twice gives two events.

import { randomBytes } from "node:crypto";
import type { LinkState, RawContent, RawFile, RawMessage } from "./link.ts";
import { LinkError } from "./link.ts";

export type Status = "starting" | LinkState;
export type MediaKind = "image" | "video" | "audio" | "voice" | "file";

export interface MediaItem {
  ref: string;
  kind: MediaKind;
  mime: string;
  name: string;
  size: number | null;
  /** iMessage's rich-link preview art, sent as an attachment right after a link. */
  preview: boolean;
}

export interface SidecarEvent {
  seq: number;
  id: string;
  kind: "message" | "reaction";
  space: { id: string; type: "dm" | "group"; name: string };
  sender: { id: string; name: string };
  at: number;
  text: string;
  media: MediaItem[];
  link: { url: string; title: string; summary: string } | null;
  reply: { id: string; text: string; fromUs: boolean } | null;
  reaction: { target: string; emoji: string; targetFromUs: boolean; targetText: string } | null;
}

export const KEEP_EVENTS = 1000;
export const KEEP_MEDIA = 500;
export const MEDIA_TTL_MS = 60 * 60 * 1000;
export const MAX_MEDIA_BYTES = 100 * 1024 * 1024;
const PREVIEW_MARK = ".pluginpayloadattachment";

interface HeldMedia {
  file: RawFile;
  at: number;
}

export interface RelayOptions {
  now?: () => number;
  keepEvents?: number;
  keepMedia?: number;
}

function kindOf(file: RawFile): MediaKind {
  const mime = file.mime.toLowerCase();
  if (file.voice || mime === "audio/x-caf" || file.name.toLowerCase().endsWith(".caf")) {
    return "voice";
  }
  if (mime.startsWith("image/")) return "image";
  if (mime.startsWith("video/")) return "video";
  if (mime.startsWith("audio/")) return "audio";
  return "file";
}

export class Relay {
  readonly epoch: string = randomBytes(6).toString("hex");
  status: Status = "starting";
  since: number;
  detail = "";
  private readonly now: () => number;
  private readonly keepEvents: number;
  private readonly keepMedia: number;
  private readonly ring: SidecarEvent[] = [];
  private readonly media = new Map<string, HeldMedia>();
  private readonly listeners = new Set<(event: SidecarEvent) => void>();
  private seq = 0;

  constructor(options: RelayOptions = {}) {
    this.now = options.now ?? Date.now;
    this.keepEvents = options.keepEvents ?? KEEP_EVENTS;
    this.keepMedia = options.keepMedia ?? KEEP_MEDIA;
    this.since = this.now();
  }

  get kept(): number {
    return this.ring.length;
  }

  setStatus(status: Status, detail = ""): boolean {
    if (status === this.status && detail === this.detail) return false;
    this.status = status;
    this.detail = detail;
    this.since = this.now();
    return true;
  }

  subscribe(listener: (event: SidecarEvent) => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /** Events after `after` in this run; every kept event when `epoch` names another run. */
  after(after: number, epoch: string): SidecarEvent[] {
    const from = epoch === this.epoch ? after : 0;
    return this.ring.filter((event) => event.seq > from);
  }

  private hold(file: RawFile): string {
    const ref = randomBytes(12).toString("hex");
    this.media.set(ref, { file, at: this.now() });
    while (this.media.size > this.keepMedia) {
      const oldest = this.media.keys().next();
      if (oldest.done) break;
      this.media.delete(oldest.value);
    }
    return ref;
  }

  private item(file: RawFile): MediaItem {
    return {
      ref: this.hold(file),
      kind: kindOf(file),
      mime: file.mime,
      name: file.name,
      size: file.size,
      preview: file.name.toLowerCase().includes(PREVIEW_MARK),
    };
  }

  /** Flatten `content` into text parts, media and the first link. */
  private flatten(
    content: RawContent,
    into: { text: string[]; media: MediaItem[]; link: SidecarEvent["link"] },
  ): void {
    switch (content.type) {
      case "text":
        if (content.text) into.text.push(content.text);
        return;
      case "file":
        into.media.push(this.item(content));
        return;
      case "link": {
        const link = {
          url: content.url,
          title: content.title ?? "",
          summary: content.summary ?? "",
        };
        if (into.link === null) into.link = link;
        else into.text.push(content.url);
        return;
      }
      case "group":
        for (const part of content.items) this.flatten(part, into);
        return;
      case "other":
        into.text.push(`[unsupported message: ${content.name}]`);
        return;
      default:
        return;
    }
  }

  /** Normalise one provider message; null when it is an echo or a receipt. */
  accept(raw: RawMessage): SidecarEvent | null {
    if (raw.fromUs || raw.content.type === "receipt") return null;
    const base = {
      id: raw.id,
      space: { id: raw.space.id, type: raw.space.type, name: raw.space.name ?? "" },
      sender: { id: raw.sender.id, name: raw.sender.name ?? "" },
      at: raw.at,
      reply:
        raw.replyTo === null
          ? null
          : {
              id: raw.replyTo.id,
              text: raw.replyTo.text ?? "",
              fromUs: raw.replyTo.fromUs === true,
            },
    };
    let event: Omit<SidecarEvent, "seq">;
    if (raw.content.type === "reaction") {
      const { target, emoji, targetFromUs, targetText } = raw.content;
      event = {
        ...base,
        kind: "reaction",
        text: "",
        media: [],
        link: null,
        reaction: { target, emoji, targetFromUs, targetText: targetText ?? "" },
      };
    } else {
      const parts: { text: string[]; media: MediaItem[]; link: SidecarEvent["link"] } = {
        text: [],
        media: [],
        link: null,
      };
      this.flatten(raw.content, parts);
      event = {
        ...base,
        kind: "message",
        text: parts.text.join("\n"),
        media: parts.media,
        link: parts.link,
        reaction: null,
      };
    }
    this.seq += 1;
    const numbered: SidecarEvent = { seq: this.seq, ...event };
    this.ring.push(numbered);
    if (this.ring.length > this.keepEvents) this.ring.splice(0, this.ring.length - this.keepEvents);
    for (const listener of this.listeners) listener(numbered);
    return numbered;
  }

  /** The bytes of one attachment handed out in an event. */
  async readMedia(ref: string): Promise<Uint8Array> {
    const held = this.media.get(ref);
    if (held === undefined || this.now() - held.at > MEDIA_TTL_MS) {
      this.media.delete(ref);
      throw new LinkError("message_not_found", "unknown or expired media ref");
    }
    if (held.file.size !== null && held.file.size > MAX_MEDIA_BYTES) {
      throw new LinkError("too_large");
    }
    const data = await held.file.read();
    if (data.byteLength > MAX_MEDIA_BYTES) throw new LinkError("too_large");
    return data;
  }
}
