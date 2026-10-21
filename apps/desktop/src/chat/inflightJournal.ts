// The renderer's in-flight turn journal and image drafts (P20-20).
//
// While a turn streams, the visible tail of that turn (the person's message and the rows after it)
// is copied to localStorage, so a renderer or app crash does not lose streamed progress; the
// transcript (P20-18) folds a surviving entry back in on resume and clears it once the stored
// history covers it. Best effort: storage that throws or is full never disturbs the chat.
//
// Sizes are counted in UTF-16 code units, the unit browsers charge localStorage in.
//
// Image drafts: a message with images is saved when sent and cleared on its acknowledgement; one
// that survives a restart is offered back ("Restore image draft") and never sent on its own.

export const JOURNAL_VERSION = 1;
export const STORE_KEY = "indus.desktop.inflight.v1";
export const IMAGE_DRAFTS_KEY = "indus.desktop.image-drafts.v1";

export const MAX_STORE_CHARS = 4 * 1024 * 1024;
export const MAX_ENTRY_CHARS = 160 * 1024;
export const MAX_MESSAGES = 24;
export const MAX_TEXT_PART_CHARS = 64 * 1024;
export const MAX_METADATA_CHARS = 2 * 1024;
export const MAX_ATTACHMENT_REFS = 256;
export const MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
export const WRITE_THROTTLE_MS = 400;

export interface JournalPart {
  readonly type: "text" | "reasoning" | "tool";
  readonly text: string;
  /** Set when the text was cut to `MAX_TEXT_PART_CHARS`. */
  readonly clipped?: true;
}

export interface JournalMessage {
  readonly id: string;
  readonly role: "user" | "assistant" | "tool";
  readonly parts: readonly JournalPart[];
  /** Attachment references (paths or upload ids), at most `MAX_ATTACHMENT_REFS`. */
  readonly attachments?: readonly string[];
  /** Small extra facts (tool name, status); dropped when over `MAX_METADATA_CHARS`. */
  readonly metadata?: Readonly<Record<string, unknown>>;
}

export interface TurnSnapshot {
  readonly turnId: string | null;
  readonly startedAt: number | null;
  readonly messages: readonly JournalMessage[];
}

export interface InflightEntry extends TurnSnapshot {
  readonly sessionId: string;
  readonly updatedAt: number;
}

interface Store {
  version: typeof JOURNAL_VERSION;
  entries: Record<string, InflightEntry>;
}

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export type ShapeResult =
  | { readonly ok: true; readonly entry: InflightEntry; readonly chars: number }
  | { readonly ok: false; readonly reason: "too-large" };

function clipPart(part: JournalPart): JournalPart {
  if (part.text.length <= MAX_TEXT_PART_CHARS) return part;
  return { type: part.type, text: part.text.slice(0, MAX_TEXT_PART_CHARS), clipped: true };
}

function shapeMessage(message: JournalMessage): JournalMessage {
  const shaped: {
    id: string;
    role: JournalMessage["role"];
    parts: JournalPart[];
    attachments?: string[];
    metadata?: Record<string, unknown>;
  } = { id: message.id, role: message.role, parts: message.parts.map(clipPart) };
  if (message.attachments && message.attachments.length > 0) {
    shaped.attachments = message.attachments.slice(0, MAX_ATTACHMENT_REFS);
  }
  if (message.metadata !== undefined) {
    let size = Number.POSITIVE_INFINITY;
    try {
      size = JSON.stringify(message.metadata).length;
    } catch {
      // not serializable: dropped
    }
    if (size <= MAX_METADATA_CHARS) shaped.metadata = { ...message.metadata };
  }
  return shaped;
}

/** Apply the per-entry limits; an entry still over `MAX_ENTRY_CHARS` is refused. */
export function shapeEntry(sessionId: string, snapshot: TurnSnapshot, now: number): ShapeResult {
  const messages = snapshot.messages.slice(-MAX_MESSAGES).map(shapeMessage);
  const entry: InflightEntry = {
    sessionId,
    turnId: snapshot.turnId,
    startedAt: snapshot.startedAt,
    updatedAt: now,
    messages,
  };
  const chars = JSON.stringify(entry).length;
  if (chars > MAX_ENTRY_CHARS) return { ok: false, reason: "too-large" };
  return { ok: true, entry, chars };
}

function defaultStorage(): StorageLike | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

export interface InflightJournalOptions {
  storage?: StorageLike | null;
  now?: () => number;
}

export interface ImageDraft {
  readonly requestId: string;
  readonly text: string;
  readonly attachments: readonly string[];
  readonly savedAt: number;
}

export class InflightJournal {
  private readonly storage: StorageLike | null;
  private readonly now: () => number;
  private readonly pending = new Map<string, TurnSnapshot>();
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(options: InflightJournalOptions = {}) {
    this.storage = options.storage === undefined ? defaultStorage() : options.storage;
    this.now = options.now ?? Date.now;
  }

  // -- store -------------------------------------------------------------------------------------

  private load(): Store {
    const empty: Store = { version: JOURNAL_VERSION, entries: {} };
    if (this.storage === null) return empty;
    try {
      const raw = this.storage.getItem(STORE_KEY);
      if (raw === null) return empty;
      const parsed = JSON.parse(raw) as Partial<Store>;
      if (parsed.version !== JOURNAL_VERSION || typeof parsed.entries !== "object") return empty;
      return { version: JOURNAL_VERSION, entries: parsed.entries ?? {} };
    } catch {
      return empty;
    }
  }

  private save(store: Store): void {
    if (this.storage === null) return;
    try {
      if (Object.keys(store.entries).length === 0) this.storage.removeItem(STORE_KEY);
      else this.storage.setItem(STORE_KEY, JSON.stringify(store));
    } catch {
      // full or blocked storage: the journal is best effort
    }
  }

  // -- in-flight turns ---------------------------------------------------------------------------

  /** Note the latest tail of a running turn; written at most every `WRITE_THROTTLE_MS`. */
  record(sessionId: string, snapshot: TurnSnapshot): void {
    this.pending.set(sessionId, snapshot);
    if (this.timer !== null) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.flush();
    }, WRITE_THROTTLE_MS);
  }

  /** Write every pending snapshot now (also used on unload). */
  flush(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.pending.size === 0) return;
    const store = this.load();
    const now = this.now();
    for (const [sessionId, snapshot] of this.pending) {
      const shaped = shapeEntry(sessionId, snapshot, now);
      // A refused entry leaves the session's previous entry as it was.
      if (shaped.ok) store.entries[sessionId] = shaped.entry;
    }
    this.pending.clear();
    this.dropExpired(store, now);
    this.fit(store);
    this.save(store);
  }

  /** The surviving entry of a session (null when none or older than `MAX_AGE_MS`). */
  read(sessionId: string): InflightEntry | null {
    const entry = this.load().entries[sessionId];
    if (entry === undefined || this.now() - entry.updatedAt > MAX_AGE_MS) return null;
    return entry;
  }

  /** The turn ended or the stored history covers it. */
  clear(sessionId: string): void {
    this.pending.delete(sessionId);
    const store = this.load();
    if (!(sessionId in store.entries)) return;
    delete store.entries[sessionId];
    this.save(store);
  }

  /** Drop entries older than `MAX_AGE_MS`; returns how many went. */
  prune(): number {
    const store = this.load();
    const removed = this.dropExpired(store, this.now());
    if (removed > 0) this.save(store);
    return removed;
  }

  private dropExpired(store: Store, now: number): number {
    let removed = 0;
    for (const [sessionId, entry] of Object.entries(store.entries)) {
      if (now - entry.updatedAt > MAX_AGE_MS) {
        delete store.entries[sessionId];
        removed += 1;
      }
    }
    return removed;
  }

  /** Evict the oldest entries until the store fits `MAX_STORE_CHARS`. */
  private fit(store: Store): void {
    const byAge = Object.values(store.entries).sort((a, b) => a.updatedAt - b.updatedAt);
    let size = JSON.stringify(store).length;
    for (const entry of byAge) {
      if (size <= MAX_STORE_CHARS) return;
      delete store.entries[entry.sessionId];
      size = JSON.stringify(store).length;
    }
  }

  // -- image drafts ------------------------------------------------------------------------------

  private loadDrafts(): Record<string, ImageDraft> {
    if (this.storage === null) return {};
    try {
      const raw = this.storage.getItem(IMAGE_DRAFTS_KEY);
      const parsed: unknown = raw === null ? {} : JSON.parse(raw);
      return typeof parsed === "object" && parsed !== null
        ? (parsed as Record<string, ImageDraft>)
        : {};
    } catch {
      return {};
    }
  }

  private saveDrafts(drafts: Record<string, ImageDraft>): void {
    if (this.storage === null) return;
    try {
      if (Object.keys(drafts).length === 0) this.storage.removeItem(IMAGE_DRAFTS_KEY);
      else this.storage.setItem(IMAGE_DRAFTS_KEY, JSON.stringify(drafts));
    } catch {
      // best effort
    }
  }

  /** Remember an image message until its acknowledgement. */
  saveImageDraft(sessionId: string, draft: Omit<ImageDraft, "savedAt">): void {
    const drafts = this.loadDrafts();
    drafts[sessionId] = {
      requestId: draft.requestId,
      text: draft.text.slice(0, MAX_TEXT_PART_CHARS),
      attachments: draft.attachments.slice(0, MAX_ATTACHMENT_REFS),
      savedAt: this.now(),
    };
    this.saveDrafts(drafts);
  }

  /** The image draft to offer for a session, if one survived (never sent from here). */
  imageDraft(sessionId: string): ImageDraft | null {
    const draft = this.loadDrafts()[sessionId];
    if (draft === undefined || this.now() - draft.savedAt > MAX_AGE_MS) return null;
    return draft;
  }

  /** The message was acknowledged (or the person dismissed or restored the draft). */
  clearImageDraft(sessionId: string, requestId?: string): void {
    const drafts = this.loadDrafts();
    const draft = drafts[sessionId];
    if (draft === undefined) return;
    if (requestId !== undefined && draft.requestId !== requestId) return;
    delete drafts[sessionId];
    this.saveDrafts(drafts);
  }
}
