// Sending, and what the person can do while a turn runs (P20-21): Stop, Steer, Queue.
//
// The queue lives on the daemon: `prompt.submit { when_busy: "queue" }` puts an input in the
// session's durable FIFO, shared by every window and surface; `admission.state` events tell us
// where it stands. Stop interrupts exactly the running turn (its generation, never "the latest").
// With inputs waiting, Stop also pauses the queue: the daemon has no pause, so the inputs THIS
// window queued are withdrawn (`admission.cancel`) and held here, in order, until Resume. Resume
// (the drain) sends them back one at a time and gives up on an item after
// `MAX_AUTO_DRAIN_ATTEMPTS` failures. A turn whose outcome is unknown after a daemon restart
// blocks the queue and offers one Discard; nothing is ever resubmitted on its own (R03.21).
//
// Every input goes through the prepared-submission journal (P20-20): prepare → submit with the
// journal's request id → retire with the receipt. An uncertain answer keeps the entry; the person
// retries (same request id: the daemon runs it at most once) or dismisses it.
//
// Turn identity (W47-a): the submit answer and `admission.state` name an input by its admission id,
// `turn.started` / `turn.done` by the daemon's own turn id; only the generation is common to both,
// and the answer can arrive before `turn.started` or after `turn.done`. Busy state is therefore
// kept by generation: a turn whose end was seen never shows Stop again, and `turn.done` ends the
// busy state unless a later turn has started since.

import type { MethodName, MethodTypes, PromptSubmitResult } from "@indus/protocol";
import {
  type ChatConnection,
  REQUEST_TIMEOUTS,
  type RequestOptions,
  type SessionEvent,
} from "../gateway/connectionStore.ts";
import type { InflightJournal } from "./inflightJournal.ts";
import { errorMessage, errorName, isUncertain } from "./rpcErrors.ts";

export const MAX_AUTO_DRAIN_ATTEMPTS = 4;
/** Pause between drain attempts of the same item (times the attempt number). */
export const DRAIN_RETRY_MS = 500;

export type Request = <M extends MethodName>(
  method: M,
  params: MethodTypes[M]["params"],
  options?: RequestOptions,
) => Promise<MethodTypes[M]["result"]>;

/** An input waiting in the daemon's FIFO. */
export interface QueueEntry {
  readonly admissionId: string;
  readonly position: number | null;
  /** The text, when this window queued it. */
  readonly text: string | null;
  readonly mine: boolean;
}

/** An input held in this window while the queue is paused. */
export interface HeldItem {
  readonly requestId: string;
  readonly text: string;
  readonly failures: number;
  /** The drain gave up on it after `MAX_AUTO_DRAIN_ATTEMPTS`. */
  readonly failed: boolean;
}

/** A turn lost to a daemon restart. */
export interface LostTurn {
  readonly admissionId: string;
  readonly generation: number | null;
}

/** A journaled input whose last send had no definite answer. */
export interface UncertainSubmission {
  readonly requestId: string;
  readonly text: string;
}

export interface BusySnapshot {
  readonly running: boolean;
  /** The running (or just ended) turn: its own id once `turn.started` named it, else the
   * admission id from the submit answer. */
  readonly turnId: string | null;
  readonly generation: number | null;
  readonly queue: readonly QueueEntry[];
  readonly held: readonly HeldItem[];
  /** Held items wait for Resume. */
  readonly paused: boolean;
  readonly draining: boolean;
  readonly lost: LostTurn | null;
  readonly uncertain: readonly UncertainSubmission[];
  readonly error: string | null;
  /** The protocol name of the last failure (`SESSION_NOT_FOUND`, …), when it had one. */
  readonly errorName: string | null;
}

export interface PreparedJournalPort {
  prepare(entry: { requestId: string; sessionId: string; payload: unknown }): Promise<unknown>;
  retire(entry: {
    requestId: string;
    receipt: { outcome: "accepted" | "rejected" | "unknown"; admissionId?: string };
  }): Promise<unknown>;
  list(): Promise<{
    entries: Array<{ requestId: string; sessionId: string; state: string; payload: unknown }>;
  }>;
}

export interface SendOptions {
  /** Image references in the message: kept as an image draft until acknowledged. */
  readonly images?: readonly string[];
}

export type SendOutcome =
  | "started"
  | "queued"
  | "steered"
  | "uncertain"
  | "rejected"
  | "blocked"
  | "empty";

export interface ChatActionsOptions {
  sessionId: string;
  request: Request;
  /** Defaults to the desktop bridge's journal; null runs without one (tests, browsers). */
  journal?: PreparedJournalPort | null;
  inflight?: InflightJournal | null;
  newId?: () => string;
  /** Waits between drain attempts; tests pass an instant one. */
  sleep?: (ms: number) => Promise<void>;
}

interface Mine {
  readonly requestId: string;
  readonly text: string;
}

function bridgeJournal(): PreparedJournalPort | null {
  const bridge = (globalThis as { indusDesktop?: { journal?: PreparedJournalPort } }).indusDesktop;
  return bridge?.journal ?? null;
}

function textOf(payload: unknown): string {
  const text = (payload as { text?: unknown } | null)?.text;
  return typeof text === "string" ? text : "";
}

const realSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export class ChatActions {
  readonly sessionId: string;
  private readonly request: Request;
  private readonly journal: PreparedJournalPort | null;
  private readonly inflight: InflightJournal | null;
  private readonly newId: () => string;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly listeners = new Set<() => void>();
  /** Admission id → what this window sent under it. */
  private readonly mine = new Map<string, Mine>();
  /** Journaled inputs this instance is sending now, or has settled (never "recovered"). */
  private readonly sending = new Set<string>();
  private readonly settled = new Set<string>();
  private pendingSubmit: Promise<unknown> | null = null;
  /** The daemon's turn ids seen in `turn.started` → their generation (the latest few). */
  private readonly turns = new Map<string, number>();
  /** The newest generation whose end this window saw (-1: none). */
  private ended = -1;
  private state: BusySnapshot = {
    running: false,
    turnId: null,
    generation: null,
    queue: [],
    held: [],
    paused: false,
    draining: false,
    lost: null,
    uncertain: [],
    error: null,
    errorName: null,
  };

  constructor(options: ChatActionsOptions) {
    this.sessionId = options.sessionId;
    this.request = options.request;
    this.journal = options.journal === undefined ? bridgeJournal() : options.journal;
    this.inflight = options.inflight ?? null;
    this.newId = options.newId ?? (() => crypto.randomUUID());
    this.sleep = options.sleep ?? realSleep;
  }

  // -- state ---------------------------------------------------------------------------------------

  snapshot = (): BusySnapshot => this.state;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private fail(error: unknown): void {
    this.set({ error: errorMessage(error), errorName: errorName(error) ?? null });
  }

  private set(patch: Partial<BusySnapshot>): void {
    const next = { ...this.state, ...patch };
    next.paused = next.held.length > 0 && !next.draining;
    this.state = next;
    for (const listener of this.listeners) listener();
  }

  /** Feed the session's events (`ChatConnection.onSessionEvent`). */
  handleEvent(event: SessionEvent): void {
    if (event.session_id !== this.sessionId) return;
    switch (event.type) {
      case "turn.started":
        this.remember(event.turn_id, event.generation);
        // a renumbered ledger (a new daemon) starts below what ended before
        if (event.generation <= this.ended) this.ended = event.generation - 1;
        this.set({ running: true, turnId: event.turn_id, generation: event.generation });
        return;
      case "turn.done":
        this.onTurnDone(event.turn_id);
        return;
      case "session.info":
        if (event.running === false && this.state.running) {
          this.markEnded(this.state.generation);
          this.set({ running: false, turnId: null, generation: null });
        }
        return;
      case "admission.state":
        this.onAdmission(event.admission_id, event.state, event.generation, event.position);
        return;
      default:
        return;
    }
  }

  private remember(turnId: string, generation: number): void {
    this.turns.delete(turnId);
    this.turns.set(turnId, generation);
    while (this.turns.size > 16) {
      const [oldest] = this.turns.keys();
      if (oldest === undefined) break;
      this.turns.delete(oldest);
    }
  }

  private markEnded(generation: number | null): void {
    if (generation !== null && generation > this.ended) this.ended = generation;
  }

  /** The daemon's id of the turn running `generation`, when `turn.started` named it. */
  private turnOf(generation: number): string | null {
    let found: string | null = null;
    for (const [turnId, own] of this.turns) if (own === generation) found = turnId;
    return found;
  }

  private onTurnDone(turnId: string): void {
    const { running, generation, turnId: current } = this.state;
    const own = this.turns.get(turnId) ?? (current === turnId ? generation : null);
    this.markEnded(own);
    if (!running) return;
    // One turn runs at a time: only a later turn that already started keeps the busy state.
    const later =
      current !== turnId &&
      current !== null &&
      this.turns.has(current) &&
      (own === null || (generation !== null && generation > own));
    if (later) return;
    this.markEnded(generation);
    this.set({ running: false, turnId: null, generation: null });
  }

  /** A turn is claimed for `generation` (submit answer or `admission.state` started). */
  private claimed(admissionId: string, generation: number | null): Partial<BusySnapshot> {
    const turnId = generation === null ? null : this.turnOf(generation);
    if (generation !== null && generation <= this.ended) {
      // its end already arrived: nothing runs (the turn id still binds the optimistic row)
      return { running: false, turnId: turnId ?? admissionId, generation: null };
    }
    return { running: true, turnId: turnId ?? admissionId, generation };
  }

  private onAdmission(
    admissionId: string,
    state: string,
    generation: number | null | undefined,
    position: number | null | undefined,
  ): void {
    const others = this.state.queue.filter((entry) => entry.admissionId !== admissionId);
    if (state === "queued") {
      const known = this.mine.get(admissionId);
      const entry: QueueEntry = {
        admissionId,
        position: position ?? null,
        text: known?.text ?? null,
        mine: known !== undefined,
      };
      this.set({ queue: sortQueue([...others, entry]) });
      return;
    }
    if (state === "unknown") {
      this.set({
        queue: others,
        lost: { admissionId, generation: generation ?? null },
      });
      return;
    }
    // started or terminal: it left the queue
    let patch: Partial<BusySnapshot> = { queue: others };
    if (state === "terminal" && this.state.lost?.admissionId === admissionId) {
      patch = { ...patch, lost: null };
    }
    if (state === "started" && typeof generation === "number" && generation > this.ended) {
      patch = { ...patch, ...this.claimed(admissionId, generation) };
    }
    this.set(patch);
  }

  // -- sending -------------------------------------------------------------------------------------

  /** Enter in the composer: a new turn when idle, the queue while a turn runs. */
  send(text: string, options: SendOptions = {}): Promise<SendOutcome> {
    return this.submit(text, this.state.running ? "queue" : null, options);
  }

  /** Queue explicitly. */
  queue(text: string): Promise<SendOutcome> {
    return this.submit(text, "queue", {});
  }

  private async submit(
    text: string,
    whenBusy: "queue" | null,
    options: SendOptions,
    requestId: string = this.newId(),
  ): Promise<SendOutcome> {
    if (text.trim() === "") return "empty";
    const run = this.submitOnce(text, whenBusy, options, requestId);
    this.pendingSubmit = run;
    this.sending.add(requestId);
    try {
      return await run;
    } finally {
      this.sending.delete(requestId);
      if (this.pendingSubmit === run) this.pendingSubmit = null;
    }
  }

  private async submitOnce(
    text: string,
    whenBusy: "queue" | null,
    options: SendOptions,
    requestId: string,
    offerRetry = true,
  ): Promise<SendOutcome> {
    if (this.journal !== null) {
      try {
        await this.journal.prepare({ requestId, sessionId: this.sessionId, payload: { text } });
      } catch (error) {
        // Never send what could not be journaled: the text stays in the composer.
        this.fail(error);
        return "rejected";
      }
    }
    const images = options.images ?? [];
    if (images.length > 0) {
      this.inflight?.saveImageDraft(this.sessionId, { requestId, text, attachments: images });
    }
    let result: PromptSubmitResult;
    try {
      result = await this.request(
        "prompt.submit",
        {
          session_id: this.sessionId,
          text,
          request_id: requestId,
          ...(whenBusy === null ? {} : { when_busy: whenBusy }),
        },
        { timeoutMs: REQUEST_TIMEOUTS.promptSubmit },
      );
    } catch (error) {
      if (errorName(error) === "SESSION_BUSY" && whenBusy === null) {
        // The turn started elsewhere a moment ago: the same input goes to the queue.
        return this.submitOnce(text, "queue", options, requestId, offerRetry);
      }
      if (isUncertain(error)) {
        await this.retire(requestId, "unknown");
        if (offerRetry) this.addUncertain({ requestId, text });
        this.fail(error);
        return "uncertain";
      }
      await this.retire(requestId, "rejected");
      this.inflight?.clearImageDraft(this.sessionId, requestId);
      this.fail(error);
      return "rejected";
    }
    await this.retire(requestId, "accepted", result.turn_id);
    this.inflight?.clearImageDraft(this.sessionId, requestId);
    this.dropUncertain(requestId);
    this.set({ error: null, errorName: null });
    if (result.status === "queued") {
      this.mine.set(result.turn_id, { requestId, text });
      const listed = this.state.queue.find((entry) => entry.admissionId === result.turn_id);
      if (listed === undefined) {
        this.set({
          queue: sortQueue([
            ...this.state.queue,
            { admissionId: result.turn_id, position: null, text, mine: true },
          ]),
        });
      } else if (!listed.mine) {
        this.set({
          queue: this.state.queue.map((entry) =>
            entry.admissionId === result.turn_id ? { ...entry, text, mine: true } : entry,
          ),
        });
      }
      return "queued";
    }
    if (result.status === "steered" || result.status === "redirected") return "steered";
    const generation =
      typeof result.generation === "number" ? result.generation : this.state.generation;
    this.set(this.claimed(result.turn_id, generation));
    return "started";
  }

  private async retire(
    requestId: string,
    outcome: "accepted" | "rejected" | "unknown",
    admissionId?: string,
  ): Promise<void> {
    if (outcome !== "unknown") this.settled.add(requestId);
    if (this.journal === null) return;
    try {
      await this.journal.retire({
        requestId,
        receipt: admissionId === undefined ? { outcome } : { outcome, admissionId },
      });
    } catch {
      // an entry that could not be retired is offered again later; the request id dedupes it
    }
  }

  private addUncertain(item: UncertainSubmission): void {
    const rest = this.state.uncertain.filter((entry) => entry.requestId !== item.requestId);
    this.set({ uncertain: [...rest, item] });
  }

  private dropUncertain(requestId: string): void {
    if (!this.state.uncertain.some((entry) => entry.requestId === requestId)) return;
    this.set({ uncertain: this.state.uncertain.filter((entry) => entry.requestId !== requestId) });
  }

  /** Offer this session's journaled inputs left from an earlier run (or another window). */
  async recoverPrepared(): Promise<readonly UncertainSubmission[]> {
    if (this.journal === null) return [];
    let entries: Awaited<ReturnType<PreparedJournalPort["list"]>>["entries"];
    try {
      entries = (await this.journal.list()).entries;
    } catch {
      return [];
    }
    for (const entry of entries) {
      if (entry.sessionId !== this.sessionId) continue;
      // An input this instance is still sending (or has settled) is not left over: listing the
      // journal mid-submit would otherwise offer it again as "not confirmed".
      if (this.sending.has(entry.requestId) || this.settled.has(entry.requestId)) continue;
      this.addUncertain({ requestId: entry.requestId, text: textOf(entry.payload) });
    }
    return this.state.uncertain;
  }

  /** Send a journaled input again under its own request id (never runs twice). */
  retryUncertain(requestId: string): Promise<SendOutcome> {
    const item = this.state.uncertain.find((entry) => entry.requestId === requestId);
    if (item === undefined) return Promise.resolve("empty");
    return this.submit(item.text, this.state.running ? "queue" : null, {}, item.requestId);
  }

  /** The person gave up on a journaled input. */
  async dismissUncertain(requestId: string): Promise<void> {
    await this.retire(requestId, "rejected");
    this.dropUncertain(requestId);
  }

  // -- while a turn runs ---------------------------------------------------------------------------

  /** Guidance for the running turn; sent as a new prompt when no turn takes it. */
  async steer(text: string): Promise<SendOutcome> {
    if (text.trim() === "") return "empty";
    if (!this.state.running) return this.send(text);
    try {
      const answer = await this.request("session.steer", {
        session_id: this.sessionId,
        text,
        generation: this.state.generation,
      });
      if (answer.queued) return "steered";
    } catch (error) {
      if (errorName(error) !== "SESSION_NOT_FOUND" && errorName(error) !== "STALE_GENERATION") {
        this.fail(error);
        return "rejected";
      }
    }
    return this.submit(text, null, {});
  }

  /** Stop the running turn (Stop button, Esc); with inputs waiting, also pause the queue. */
  async stop(): Promise<boolean> {
    if (this.state.generation === null && this.pendingSubmit !== null) {
      // The submit's answer names the generation: wait for it, never interrupt blindly.
      await this.pendingSubmit.catch(() => {});
    }
    const generation = this.state.generation;
    let interrupted = false;
    if (generation !== null) {
      try {
        const answer = await this.request("session.interrupt", {
          session_id: this.sessionId,
          generation,
        });
        interrupted = answer.interrupted;
      } catch (error) {
        if (errorName(error) !== "STALE_GENERATION") this.fail(error);
      }
    }
    await this.pause();
    return interrupted;
  }

  /** Withdraw this window's queued inputs from the daemon and hold them until Resume. */
  private async pause(): Promise<void> {
    const own = this.state.queue.filter((entry) => entry.mine && entry.text !== null);
    if (own.length === 0) return;
    const held: HeldItem[] = [...this.state.held];
    for (const entry of own) {
      try {
        await this.request("admission.cancel", {
          session_id: this.sessionId,
          admission_id: entry.admissionId,
        });
      } catch {
        // it started meanwhile (or is gone): leave it where it is
        continue;
      }
      this.mine.delete(entry.admissionId);
      held.push({ requestId: this.newId(), text: entry.text ?? "", failures: 0, failed: false });
      this.set({
        queue: this.state.queue.filter((other) => other.admissionId !== entry.admissionId),
        held,
        draining: false,
      });
    }
  }

  /** Resume a paused queue: send the held inputs back in order. */
  async resume(): Promise<void> {
    if (this.state.lost !== null || this.state.draining) return;
    this.set({
      draining: true,
      held: this.state.held.map((item) =>
        item.failed ? { ...item, failed: false, failures: 0 } : item,
      ),
    });
    try {
      await this.drain();
    } finally {
      this.set({ draining: false });
    }
  }

  private async drain(): Promise<void> {
    while (this.state.held.length > 0 && this.state.lost === null) {
      const [head] = this.state.held;
      if (head === undefined || head.failed) return;
      const outcome = await this.submitOnce(head.text, "queue", {}, head.requestId, false);
      if (outcome === "queued" || outcome === "started" || outcome === "steered") {
        this.dropUncertain(head.requestId);
        this.set({ held: this.state.held.slice(1) });
        continue;
      }
      const failures = head.failures + 1;
      const failed = failures >= MAX_AUTO_DRAIN_ATTEMPTS;
      this.set({ held: [{ ...head, failures, failed }, ...this.state.held.slice(1)] });
      if (failed) return;
      await this.sleep(DRAIN_RETRY_MS * failures);
    }
  }

  /** Drop a held item. */
  removeHeld(requestId: string): void {
    this.set({ held: this.state.held.filter((item) => item.requestId !== requestId) });
  }

  /** Withdraw one of the daemon's queued inputs. */
  async cancelQueued(admissionId: string): Promise<boolean> {
    try {
      await this.request("admission.cancel", {
        session_id: this.sessionId,
        admission_id: admissionId,
      });
    } catch (error) {
      this.fail(error);
      return false;
    }
    this.mine.delete(admissionId);
    this.set({ queue: this.state.queue.filter((entry) => entry.admissionId !== admissionId) });
    return true;
  }

  /** Resolve a lost turn: its transcript closes it, the queue behind it goes on. */
  async discard(): Promise<boolean> {
    const lost = this.state.lost;
    if (lost === null) return false;
    let generation = lost.generation;
    if (generation === null) {
      try {
        const receipt = await this.request("admission.receipt", {
          session_id: this.sessionId,
          admission_id: lost.admissionId,
        });
        generation = receipt.generation ?? null;
      } catch (error) {
        this.fail(error);
        return false;
      }
    }
    if (generation === null) {
      this.set({ error: "the lost turn has no generation" });
      return false;
    }
    try {
      await this.request("admission.discard", {
        session_id: this.sessionId,
        admission_id: lost.admissionId,
        generation,
      });
    } catch (error) {
      this.fail(error);
      return false;
    }
    this.set({ lost: null, error: null, errorName: null });
    return true;
  }
}

function sortQueue(entries: QueueEntry[]): QueueEntry[] {
  return [...entries].sort(
    (a, b) => (a.position ?? Number.MAX_SAFE_INTEGER) - (b.position ?? Number.MAX_SAFE_INTEGER),
  );
}

const shared = new WeakMap<object, Map<string, ChatActions>>();

/**
 * The one `ChatActions` of a session on a connection, so the fresh draft's first send, the
 * composer that shows the session afterwards and the transcript's Retry share busy state, the queue
 * and the journal bookkeeping. `make` builds it on first use.
 */
export function sharedActions(
  connection: object,
  sessionId: string,
  make: () => ChatActions,
): ChatActions {
  let bySession = shared.get(connection);
  if (bySession === undefined) {
    bySession = new Map();
    shared.set(connection, bySession);
  }
  let actions = bySession.get(sessionId);
  if (actions === undefined) {
    const made = make();
    actions = made;
    bySession.set(sessionId, made);
    // W47-a: it hears the session's events for as long as it lives, not only while a composer
    // shows the session (a fresh draft's first send, quick entry and Retry run with none mounted,
    // and a turn that ends meanwhile would otherwise keep Stop/Steer up).
    const source = connection as Partial<Pick<ChatConnection, "onSessionEvent">>;
    if (typeof source.onSessionEvent === "function") {
      source.onSessionEvent.call(connection, sessionId, (event) => made.handleEvent(event));
    }
  }
  return actions;
}
