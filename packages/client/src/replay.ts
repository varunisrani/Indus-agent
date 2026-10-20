// Replay cursors (P17-20): per session, the newest event `seq` applied and the daemon replay epoch
// it belongs to. A reconnecting client asks the daemon for what came after its cursor; events at or
// below the cursor of the same epoch are duplicates and never reach the surface twice.

/** Sessions with a cursor at most (research 08 §3.9); the least recently touched goes first. */
export const REPLAY_CURSOR_CAPACITY = 256;

/** The event a daemon sends when it cannot replay from a cursor (another run, evicted, ahead). */
export const REPLAY_GAP_EVENT = "session.replay_gap";

export interface Cursor {
  /** The replay epoch the seq belongs to; null for a server without epochs (the stdio bridge). */
  readonly epoch: string | null;
  /** The newest seq applied. */
  readonly seq: number;
}

/**
 * `apply`: new for its session (the cursor moved); `duplicate`: already applied, drop it;
 * `gap`: a replay gap (the cursor was reset to the gap's `last_seq`; reload a snapshot);
 * `untracked`: no session or seq (transport events, ready, server requests).
 */
export type Admission = "apply" | "duplicate" | "gap" | "untracked";

export interface ReplayOptions {
  capacity?: number;
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const isSeq = (value: unknown): value is number =>
  typeof value === "number" && Number.isInteger(value) && value >= 0;

export class ReplayCursors {
  private readonly capacity: number;
  private readonly cursors = new Map<string, Cursor>();
  private currentEpoch: string | null = null;
  /** Cursors dropped by the capacity rule, for diagnostics. */
  evicted = 0;

  constructor(options: ReplayOptions = {}) {
    this.capacity = Math.max(1, options.capacity ?? REPLAY_CURSOR_CAPACITY);
  }

  /** The epoch of the connection's ready frame (events without their own epoch use it). */
  get epoch(): string | null {
    return this.currentEpoch;
  }

  get size(): number {
    return this.cursors.size;
  }

  setEpoch(epoch: string | null | undefined): void {
    this.currentEpoch = typeof epoch === "string" && epoch !== "" ? epoch : null;
  }

  cursor(sessionId: string): Cursor | undefined {
    return this.cursors.get(sessionId);
  }

  /** Sessions with a cursor, least recently touched first. */
  sessions(): string[] {
    return [...this.cursors.keys()];
  }

  /** Decide what to do with one inbound event body (see `Admission`). */
  admit(event: unknown): Admission {
    if (!isObject(event) || typeof event.session_id !== "string" || event.session_id === "") {
      return "untracked";
    }
    const sessionId = event.session_id;
    if (event.type === REPLAY_GAP_EVENT) {
      const epoch = typeof event.replay_epoch === "string" ? event.replay_epoch : this.currentEpoch;
      this.reset(sessionId, epoch, isSeq(event.last_seq) ? event.last_seq : 0);
      return "gap";
    }
    if (!isSeq(event.seq)) return "untracked";
    const epoch = typeof event.replay_epoch === "string" ? event.replay_epoch : this.currentEpoch;
    const known = this.cursors.get(sessionId);
    if (known !== undefined && known.epoch === epoch && event.seq <= known.seq) return "duplicate";
    this.put(sessionId, { epoch, seq: event.seq });
    return "apply";
  }

  /** Move the cursor forward (or into another epoch); never backwards within one epoch. */
  advance(sessionId: string, epoch: string | null, seq: number): void {
    if (!isSeq(seq)) return;
    const known = this.cursors.get(sessionId);
    if (known !== undefined && known.epoch === epoch && seq <= known.seq) {
      this.touch(sessionId, known);
      return;
    }
    this.put(sessionId, { epoch, seq });
  }

  /** Set the cursor whatever it was (after a gap the snapshot is the new base). */
  reset(sessionId: string, epoch: string | null, seq: number): void {
    this.put(sessionId, { epoch, seq: isSeq(seq) ? seq : 0 });
  }

  forget(sessionId: string): void {
    this.cursors.delete(sessionId);
  }

  /** Drop every cursor (a server without replay epochs numbers each process from 1 again). */
  clear(): void {
    this.cursors.clear();
  }

  /** `session.subscribe` / `events.since` params that resume `sessionId` from its cursor. */
  resumeParams(sessionId: string): { session_id: string; epoch?: string; cursor?: number } {
    const known = this.cursors.get(sessionId);
    if (known === undefined) return { session_id: sessionId };
    return known.epoch === null
      ? { session_id: sessionId, cursor: known.seq }
      : { session_id: sessionId, epoch: known.epoch, cursor: known.seq };
  }

  private touch(sessionId: string, cursor: Cursor): void {
    this.cursors.delete(sessionId);
    this.cursors.set(sessionId, cursor);
  }

  private put(sessionId: string, cursor: Cursor): void {
    this.touch(sessionId, cursor);
    while (this.cursors.size > this.capacity) {
      const oldest = this.cursors.keys().next().value;
      if (oldest === undefined) break;
      this.cursors.delete(oldest);
      this.evicted += 1;
    }
  }
}
