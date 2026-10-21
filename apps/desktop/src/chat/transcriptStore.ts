// Per-session transcripts of one connection (P20-18). Events are applied as they arrive;
// listeners hear about them in batches (every 33 ms) so a fast stream does not render per delta.
// Local actions (a submitted row) notify at once. History loads are generation-fenced: a load
// that was asked earlier but answers later than a newer one is ignored (R08.19).

import { type ClientEvent, type Clock, systemClock } from "@indus/client";
import type { GatewayConnection } from "../gateway/connectionStore.ts";
import {
  addLocalUser,
  applyEvent,
  applyHistory,
  bindLocalUser,
  dropLocalUser,
  emptyTranscript,
  type SessionTranscript,
} from "./messageStream.ts";

export const TRANSCRIPT_FLUSH_MS = 33;

interface Entry {
  state: SessionTranscript;
  listeners: Set<() => void>;
  opened: number;
  flushTimer: unknown;
  hydrating: boolean;
}

export interface TranscriptStoreOptions {
  clock?: Clock;
  flushMs?: number;
}

/** The part of a connection the store uses (tests pass a fake). */
export type TranscriptSource = Pick<
  GatewayConnection,
  "onEvent" | "request" | "watchSession" | "unwatchSession"
>;

export class TranscriptStore {
  private readonly connection: TranscriptSource;
  private readonly clock: Clock;
  private readonly flushMs: number;
  private readonly entries = new Map<string, Entry>();
  private readonly settingsListeners = new Set<() => void>();
  private historySerial = 0;
  private localSerial = 0;
  private reasoning = false;
  private readonly unsubscribe: () => void;

  constructor(connection: TranscriptSource, options: TranscriptStoreOptions = {}) {
    this.connection = connection;
    this.clock = options.clock ?? systemClock;
    this.flushMs = options.flushMs ?? TRANSCRIPT_FLUSH_MS;
    this.unsubscribe = connection.onEvent((event) => this.onEvent(event));
  }

  get(sessionId: string): SessionTranscript {
    return this.entry(sessionId).state;
  }

  subscribe(sessionId: string, listener: () => void): () => void {
    const entry = this.entry(sessionId);
    entry.listeners.add(listener);
    return () => {
      entry.listeners.delete(listener);
    };
  }

  /** Show a session: watch its events (kept across reconnects) and load its history. */
  async open(sessionId: string): Promise<void> {
    const entry = this.entry(sessionId);
    entry.opened += 1;
    if (entry.opened > 1) return;
    await Promise.allSettled([this.connection.watchSession(sessionId), this.hydrate(sessionId)]);
  }

  close(sessionId: string): void {
    const entry = this.entries.get(sessionId);
    if (entry === undefined || entry.opened === 0) return;
    entry.opened -= 1;
    if (entry.opened === 0) this.connection.unwatchSession(sessionId);
  }

  /** Load the stored conversation; older loads that answer late change nothing. */
  async hydrate(sessionId: string): Promise<void> {
    const entry = this.entry(sessionId);
    this.historySerial += 1;
    const generation = this.historySerial;
    entry.hydrating = true;
    try {
      const result = await this.connection.request("session.history", { session_id: sessionId });
      this.set(sessionId, applyHistory(entry.state, result.messages, generation), true);
    } finally {
      if (generation === this.historySerial) entry.hydrating = false;
    }
  }

  /** The optimistic row for a prompt being submitted; returns its local id. */
  addLocalUser(sessionId: string, text: string): string {
    this.localSerial += 1;
    const localId = `l${this.localSerial}`;
    this.set(sessionId, addLocalUser(this.entry(sessionId).state, localId, text), true);
    return localId;
  }

  bindLocalUser(sessionId: string, localId: string, turnId: string): void {
    this.set(sessionId, bindLocalUser(this.entry(sessionId).state, localId, turnId), true);
  }

  dropLocalUser(sessionId: string, localId: string): void {
    this.set(sessionId, dropLocalUser(this.entry(sessionId).state, localId), true);
  }

  get showReasoning(): boolean {
    return this.reasoning;
  }

  /** `display.show_reasoning`, toggled live. */
  setShowReasoning(show: boolean): void {
    if (this.reasoning === show) return;
    this.reasoning = show;
    for (const listener of [...this.settingsListeners]) listener();
  }

  subscribeSettings(listener: () => void): () => void {
    this.settingsListeners.add(listener);
    return () => {
      this.settingsListeners.delete(listener);
    };
  }

  dispose(): void {
    this.unsubscribe();
    for (const entry of this.entries.values()) {
      if (entry.flushTimer !== undefined) this.clock.clearTimeout(entry.flushTimer);
    }
  }

  private onEvent(event: ClientEvent): void {
    const sessionId = (event as { session_id?: unknown }).session_id;
    if (typeof sessionId !== "string") return;
    const entry = this.entries.get(sessionId);
    if (entry === undefined) return;
    const next = applyEvent(entry.state, event);
    if (next === entry.state) return;
    this.set(sessionId, next, false);
    if (next.needsSnapshot && !entry.hydrating) void this.hydrate(sessionId).catch(() => undefined);
  }

  private entry(sessionId: string): Entry {
    let entry = this.entries.get(sessionId);
    if (entry === undefined) {
      entry = {
        state: emptyTranscript(sessionId),
        listeners: new Set(),
        opened: 0,
        flushTimer: undefined,
        hydrating: false,
      };
      this.entries.set(sessionId, entry);
    }
    return entry;
  }

  private set(sessionId: string, state: SessionTranscript, now: boolean): void {
    const entry = this.entry(sessionId);
    if (state === entry.state) return;
    entry.state = state;
    if (now) {
      this.flush(entry);
      return;
    }
    if (entry.flushTimer === undefined) {
      entry.flushTimer = this.clock.setTimeout(() => this.flush(entry), this.flushMs);
    }
  }

  private flush(entry: Entry): void {
    if (entry.flushTimer !== undefined) this.clock.clearTimeout(entry.flushTimer);
    entry.flushTimer = undefined;
    for (const listener of [...entry.listeners]) listener();
  }
}
