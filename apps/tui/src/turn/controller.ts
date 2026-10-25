// The turn state machine on screen (research 05 §3.28):
//   idle → busy (turn.started) → [interrupted-sealed (local stop)] → idle (message.complete).
// Raw reply text collects in a buffer; a tool boundary seals it into a segment; finished tools
// head the next segment (the transcript draws a message's tools above its text, so the screen
// reads in the order things happened); completion shows only the part of the final text not
// already on screen. A local stop seals at once and reconciles with the stored partial later (R05.4).

import { type Clock, systemClock } from "@indus/client";
import type { Rpc } from "../app/rpc.ts";
import { FlushScheduler } from "../events/flushScheduler.ts";
import {
  FINISHED_TURN_MEMORY,
  INTERRUPT_COOLDOWN_MS,
  LIVE_TAIL_MAX_CHARS,
  LIVE_TAIL_MAX_LINES,
  NOTICE_TTL_MS,
  REASONING_KEEP_CHARS,
  REASONING_PULSE_MS,
  REASONING_TRIM_AT_CHARS,
  THINKING_PREVIEW_CHARS,
} from "../events/timing.ts";
import { captureDestination, isCurrent } from "../state/destination.ts";
import { overlayStore } from "../state/overlayStore.ts";
import { type NewMessage, transcriptStore } from "../state/transcriptStore.ts";
import {
  ACTIVITY_LIMIT,
  type ActivityLevel,
  keepLast,
  type Segment,
  type TodoItem,
  type ToolRow,
  TRAIL_LIMIT,
  type TurnFailureView,
  type TurnOutcome,
  turnStore,
} from "../state/turnStore.ts";
import { type Notice, STATUS_READY, STATUS_WORKING, uiStore } from "../state/uiStore.ts";
import { argsPreview, looksLikeDiff, verbFor } from "./toolVerbs.ts";

type TimerHandle = ReturnType<Clock["setTimeout"]>;

export const INTERRUPTED_MARK = "_interrupted_";
export const STATUS_INTERRUPTED = "interrupted";
export const STATUS_INTERRUPTING = "interrupting…";
export const NOTHING_STREAMED_NOTE = "Stopped before any reply arrived.";

export interface ControllerOptions {
  rpc?: Pick<Rpc, "call">;
  clock?: Clock;
  /** Where system lines go (default: the transcript store). */
  sys?: (text: string) => void;
}

export interface ToolEnd {
  callId: string;
  name: string;
  ok: boolean;
  durationMs?: number;
  result?: string;
}

interface SealedStop {
  /** The bubble written at the stop, or null when only a note was written. */
  messageId: string | null;
  partial: string;
  /** Busy is held until the real end because queued input waits. */
  holdBusy: boolean;
}

const FAILURE_LAYERS: Readonly<Record<string, string>> = {
  provider_failed: "provider",
  persistence_failed: "storage",
  compression_exhausted: "context",
  budget_exhausted: "agent",
  max_iterations: "agent",
  invalid_tool_strikes: "agent",
  guardrail_halt: "agent",
  interrupted_by_user: "user",
  interrupted_by_system: "system",
};

/** The structured failure for a turn that ended with `reason` (and the server's failure text). */
export function failureView(reason: string, retryable: boolean): TurnFailureView {
  return { layer: FAILURE_LAYERS[reason] ?? "agent", code: reason, retryable };
}

/** The last `THINKING_PREVIEW_CHARS` of reasoning on one line, for the status area. */
export function thinkingPreview(reasoning: string): string {
  const line = reasoning.replace(/\s+/g, " ").trim();
  return line.length > THINKING_PREVIEW_CHARS
    ? `…${line.slice(line.length - THINKING_PREVIEW_CHARS + 1)}`
    : line;
}

/** The tail of the reply the live row renders (char and line caps). */
export function liveTail(text: string): string {
  let tail =
    text.length > LIVE_TAIL_MAX_CHARS ? text.slice(text.length - LIVE_TAIL_MAX_CHARS) : text;
  const lines = tail.split("\n");
  if (lines.length > LIVE_TAIL_MAX_LINES) tail = lines.slice(-LIVE_TAIL_MAX_LINES).join("\n");
  return tail;
}

/** Remove from the start of `final` every sealed text it repeats, in order. */
export function unseenTail(final: string, sealedTexts: readonly string[]): string {
  let tail = final.trimStart();
  for (const text of sealedTexts) {
    const piece = text.trim();
    if (piece !== "" && tail.startsWith(piece)) tail = tail.slice(piece.length).trimStart();
  }
  return tail;
}

function withMark(text: string): string {
  return text === "" ? INTERRUPTED_MARK : `${text}\n\n${INTERRUPTED_MARK}`;
}

const BARE_ERROR = /^\s*error\b/i;

export class TurnController {
  readonly flusher: FlushScheduler;
  private readonly clock: Clock;
  private readonly rpc: Pick<Rpc, "call"> | undefined;
  private readonly sys: (text: string) => void;

  private turnId: string | null = null;
  private generation: number | null = null;
  private buffer = "";
  private segments: Segment[] = [];
  private pendingTools: ToolRow[] = [];
  private activeTools: ToolRow[] = [];
  private trail: ToolRow[] = [];
  private reasoningText = "";
  private outcome: TurnOutcome | null = null;
  private sealed: SealedStop | null = null;
  private readonly finished: string[] = [];
  private segmentSeq = 0;
  private activitySeq = 0;
  private noticeSeq = 0;
  private statusTimer: TimerHandle | null = null;
  private pulseTimer: TimerHandle | null = null;
  private noticeTimer: TimerHandle | null = null;
  private heldNotice: Notice | null = null;

  constructor(options: ControllerOptions = {}) {
    this.clock = options.clock ?? systemClock;
    this.rpc = options.rpc;
    this.sys =
      options.sys ?? ((text) => transcriptStore.getState().append({ role: "system", text }));
    this.flusher = new FlushScheduler({ clock: this.clock, flush: () => this.flush() });
  }

  // -- state queries ----------------------------------------------------------------------------

  get currentTurn(): string | null {
    return this.turnId;
  }

  /** True between a local stop and the turn's real end. */
  get interrupted(): boolean {
    return this.sealed !== null;
  }

  /** Events of this turn are late: it already settled. */
  isFinished(turnId: string): boolean {
    return this.finished.includes(turnId);
  }

  /** Whether an event for `turnId` should be applied; starts an unseen turn (resume mid-turn). */
  accepts(turnId: string): boolean {
    if (this.isFinished(turnId)) return false;
    if (this.turnId !== turnId) this.start(turnId, null);
    return true;
  }

  // -- turn lifecycle ---------------------------------------------------------------------------

  start(turnId: string, generation: number | null): void {
    if (this.turnId === turnId) {
      if (generation !== null) this.generation = generation;
      turnStore.getState().patch({ generation: this.generation });
      return;
    }
    if (this.isFinished(turnId)) return;
    // A new turn while an old one never got its final: settle what is on screen first.
    if (this.turnId !== null) this.complete(this.turnId, "", "interrupted_by_system");
    this.clearTurn();
    this.turnId = turnId;
    this.generation = generation;
    const { todos } = turnStore.getState();
    turnStore.getState().reset();
    turnStore.getState().patch({ turnId, generation, todos });
    uiStore.getState().patch({ busy: true });
    this.setStatus(STATUS_WORKING);
  }

  delta(turnId: string, text: string): void {
    if (text === "" || !this.accepts(turnId) || this.sealed !== null) return;
    this.buffer += text;
    this.endReasoningPulse();
    if (uiStore.getState().streaming) this.flusher.request();
  }

  reasoning(turnId: string, text: string): void {
    if (text === "" || !this.accepts(turnId) || this.sealed !== null) return;
    this.reasoningText += text;
    if (this.reasoningText.length > REASONING_TRIM_AT_CHARS) {
      this.reasoningText = this.reasoningText.slice(-REASONING_KEEP_CHARS);
    }
    this.pulseReasoning();
    this.flusher.request();
  }

  /** Authoritative commentary between tool calls: seal it as its own segment. */
  interim(turnId: string, text: string): void {
    if (!this.accepts(turnId) || this.sealed !== null) return;
    const authoritative = text.trimStart();
    if (authoritative === "") return;
    this.buffer = authoritative;
    this.seal();
  }

  toolStart(turnId: string, callId: string, name: string, args?: Record<string, unknown>): void {
    if (!this.accepts(turnId) || this.sealed !== null) return;
    this.seal();
    this.endReasoningPulse();
    const row: ToolRow = {
      id: callId,
      name,
      verb: verbFor(name),
      args: argsPreview(args),
      status: "running",
      startedAt: this.clock.now(),
    };
    this.activeTools = [...this.activeTools.filter((t) => t.id !== callId), row];
    this.publish();
  }

  toolProgress(turnId: string, callId: string, message: string): void {
    if (!this.accepts(turnId) || this.sealed !== null) return;
    this.activeTools = this.activeTools.map((t) =>
      t.id === callId ? { ...t, output: message } : t,
    );
    this.publish();
  }

  toolComplete(turnId: string, end: ToolEnd): void {
    if (!this.accepts(turnId) || this.sealed !== null) return;
    const started = this.activeTools.find((t) => t.id === end.callId);
    this.activeTools = this.activeTools.filter((t) => t.id !== end.callId);
    const endedAt = this.clock.now();
    const result = end.result ?? "";
    const row: ToolRow = {
      id: end.callId,
      name: started?.name ?? end.name,
      verb: started?.verb ?? verbFor(end.name),
      args: started?.args ?? "",
      status: end.ok ? "done" : "error",
      output: result.split("\n", 1)[0] ?? "",
      startedAt: started?.startedAt ?? endedAt - (end.durationMs ?? 0),
      endedAt,
    };
    this.trail = keepLast([...this.trail, row], TRAIL_LIMIT);
    this.pendingTools = [...this.pendingTools, row];
    if (end.ok && uiStore.getState().inlineDiffs && looksLikeDiff(result)) {
      this.seal();
      this.attachPendingTools();
      this.pushSegment(result.trim(), [], "diff");
    }
    this.publish();
  }

  todos(items: readonly { id: string; content: string; status: string }[]): void {
    if (this.sealed !== null) return;
    const todos: TodoItem[] = items.map((item) => ({
      id: item.id,
      text: item.content,
      status: item.status,
    }));
    turnStore.getState().patch({ todos });
  }

  /** `turn.done`: record why the turn ended; the transcript settles on `message.complete`. */
  done(
    turnId: string,
    reason: string,
    failure?: { reason: string; retryable: boolean } | null,
  ): void {
    if (this.isFinished(turnId)) return;
    const failed = failure !== undefined && failure !== null;
    this.outcome = failed
      ? { reason: failure.reason, failure: failureView(reason, failure.retryable) }
      : { reason };
    if (this.turnId === turnId) turnStore.getState().patch({ outcome: this.outcome });
  }

  /** `message.complete`: settle the turn into the transcript and go idle. */
  complete(turnId: string, text: string, reason: string): void {
    if (this.isFinished(turnId)) return;
    if (this.turnId !== turnId && this.turnId !== null && this.sealed === null) {
      // The final of a turn we never saw start; settle ours as it stands, then record this one.
      this.complete(this.turnId, "", "interrupted_by_system");
    }
    if (this.sealed !== null) {
      this.reconcileStop(text, reason);
    } else {
      this.settle(text, reason);
    }
    this.remember(turnId);
    this.idle();
  }

  /**
   * Stop the running turn: seal now, ask the server to interrupt. `keepBusy` holds the busy state
   * until the real end because queued input must wait for it. Resolves to the server's answer
   * (false: nothing was running any more, or the call failed).
   */
  async interrupt(options: { keepBusy?: boolean } = {}): Promise<boolean> {
    const ui = uiStore.getState();
    const turnId = this.turnId;
    if (!ui.busy || turnId === null || this.sealed !== null || ui.sid === null) return false;
    const dest = captureDestination();
    const params: Record<string, unknown> = { session_id: ui.sid };
    if (this.generation !== null) params.generation = this.generation;
    this.sealStop(options.keepBusy ?? false);
    if (this.rpc === undefined) return false;
    try {
      const answer = (await this.rpc.call("session.interrupt", params)) as {
        interrupted?: boolean;
      };
      return answer?.interrupted === true;
    } catch (error) {
      if (isCurrent(dest) && !this.isFinished(turnId) && this.turnId === turnId) {
        this.sys(
          `Could not stop the turn: ${error instanceof Error ? error.message : String(error)}`,
        );
      }
      return false;
    }
  }

  /** Drop everything (bridge exit, session switch): idle, nothing written to the transcript. */
  reset(): void {
    if (this.turnId !== null) this.remember(this.turnId);
    this.clearTurn();
    this.idle();
    turnStore.getState().patch({ todos: [] });
    this.clearNoticeState();
    this.setStatus(STATUS_READY);
  }

  // -- status, activity, notices ----------------------------------------------------------------

  /** Show `text`; with `restoreMs`, go back to "working…"/"ready" after that long. */
  setStatus(text: string, restoreMs?: number): void {
    this.clearTimer("status");
    uiStore.getState().patch({ statusText: text });
    if (restoreMs === undefined) return;
    this.statusTimer = this.clock.setTimeout(() => {
      this.statusTimer = null;
      const { busy } = uiStore.getState();
      uiStore.getState().patch({ statusText: busy ? this.busyStatus() : STATUS_READY });
    }, restoreMs);
  }

  /** Add an activity line; repeats of the newest line are dropped; `group` replaces in place. */
  pushActivity(text: string, level: ActivityLevel = "info", group?: string): void {
    const { activity } = turnStore.getState();
    const base =
      group === undefined ? activity : activity.filter((a) => !a.id.startsWith(`${group}:`));
    const last = base.at(-1);
    if (last !== undefined && last.text === text && last.level === level) return;
    this.activitySeq += 1;
    const id = group === undefined ? `a${this.activitySeq}` : `${group}:${this.activitySeq}`;
    turnStore
      .getState()
      .patch({ activity: keepLast([...base, { id, text, level }], ACTIVITY_LIMIT) });
  }

  /**
   * A notice: shown now when idle, held until the turn ends while busy. Empty text with a key
   * clears that key (visible or held).
   */
  showNotice(input: { key: string | null; level: Notice["level"]; text: string }): void {
    if (input.text.trim() === "") {
      if (input.key !== null) this.clearNotice(input.key);
      return;
    }
    this.noticeSeq += 1;
    const notice: Notice = { id: this.noticeSeq, ...input };
    if (uiStore.getState().busy) {
      this.heldNotice = notice;
      return;
    }
    this.applyNotice(notice);
  }

  clearNotice(key: string): void {
    if (this.heldNotice?.key === key) this.heldNotice = null;
    if (uiStore.getState().notice?.key === key) {
      this.clearTimer("notice");
      uiStore.getState().patch({ notice: null });
    }
  }

  // -- internals --------------------------------------------------------------------------------

  private busyStatus(): string {
    return this.sealed?.holdBusy ? STATUS_INTERRUPTING : STATUS_WORKING;
  }

  private applyNotice(notice: Notice): void {
    this.clearTimer("notice");
    uiStore.getState().patch({ notice });
    if (notice.level === "error") return;
    this.noticeTimer = this.clock.setTimeout(() => {
      this.noticeTimer = null;
      if (uiStore.getState().notice?.id === notice.id) uiStore.getState().patch({ notice: null });
    }, NOTICE_TTL_MS);
  }

  private flushHeldNotice(): void {
    const held = this.heldNotice;
    this.heldNotice = null;
    if (held !== null) this.applyNotice(held);
  }

  private clearNoticeState(): void {
    this.heldNotice = null;
    this.clearTimer("notice");
    if (uiStore.getState().notice !== null) uiStore.getState().patch({ notice: null });
  }

  private clearTimer(which: "status" | "pulse" | "notice"): void {
    const handle =
      which === "status"
        ? this.statusTimer
        : which === "pulse"
          ? this.pulseTimer
          : this.noticeTimer;
    if (handle !== null) this.clock.clearTimeout(handle);
    if (which === "status") this.statusTimer = null;
    else if (which === "pulse") this.pulseTimer = null;
    else this.noticeTimer = null;
  }

  private pulseReasoning(): void {
    this.clearTimer("pulse");
    if (!turnStore.getState().reasoningActive)
      turnStore.getState().patch({ reasoningActive: true });
    this.pulseTimer = this.clock.setTimeout(() => {
      this.pulseTimer = null;
      turnStore.getState().patch({ reasoningActive: false });
    }, REASONING_PULSE_MS);
  }

  private endReasoningPulse(): void {
    this.clearTimer("pulse");
    if (turnStore.getState().reasoningActive)
      turnStore.getState().patch({ reasoningActive: false });
  }

  /** Copy the buffered text and reasoning to the store (called by the flush scheduler). */
  private flush(): void {
    const patch: { streamBuffer: string; reasoning?: string } = {
      streamBuffer: liveTail(this.buffer.trimStart()),
    };
    if (turnStore.getState().reasoning !== this.reasoningText) patch.reasoning = this.reasoningText;
    turnStore.getState().patch(patch);
  }

  private publish(): void {
    turnStore.getState().patch({
      segments: this.segments,
      pendingTools: this.pendingTools,
      activeTools: this.activeTools,
      trail: this.trail,
      streamBuffer: liveTail(this.buffer.trimStart()),
    });
  }

  private pushSegment(text: string, tools: ToolRow[], kind: Segment["kind"] = "text"): void {
    this.segmentSeq += 1;
    // Diff segments carry a `diff` id prefix: the transcript renders them as diffs by it.
    const prefix = kind === "diff" ? "diff:" : "";
    const id = `${prefix}${this.turnId ?? "turn"}:s${this.segmentSeq}`;
    this.segments = [...this.segments, { id, text, tools, kind }];
  }

  /** Tools finished since the last seal, with no text after them yet: a tools-only segment. */
  private attachPendingTools(): void {
    if (this.pendingTools.length === 0) return;
    this.pushSegment("", this.pendingTools);
    this.pendingTools = [];
  }

  /** Seal the buffer: tools that finished before this text head its segment. */
  private seal(): void {
    const text = this.buffer.trim();
    if (text === "") return;
    this.buffer = "";
    this.pushSegment(text, this.pendingTools);
    this.pendingTools = [];
    this.flusher.cancel();
    this.publish();
  }

  private sealedTexts(): string[] {
    return this.segments.filter((s) => s.kind !== "diff").map((s) => s.text);
  }

  private settle(text: string, reason: string): void {
    this.flusher.cancel();
    const tail = this.buffer.trim();
    if (tail !== "" && !text.includes(tail)) this.seal();
    this.buffer = "";
    const finalText = unseenTail(text, this.sealedTexts());
    const ownFence = /```(diff|patch)\b/i.test(finalText);
    const segments = this.segments.filter(
      (s) => s.kind !== "diff" || (!ownFence && !finalText.includes(s.text.trim())),
    );
    const streamed = this.segments.some((s) => s.kind !== "diff" && s.text.trim() !== "");
    const failure = this.outcome?.failure;
    const failed =
      failure !== undefined &&
      !streamed &&
      tail === "" &&
      (finalText === "" || BARE_ERROR.test(finalText));

    const writes: NewMessage[] = [];
    for (const segment of segments) {
      writes.push({
        role: "assistant",
        text: segment.kind === "diff" ? segment.text : segment.text,
        kind: segment.kind === "diff" ? "diff" : "text",
        ...(segment.tools.length > 0 ? { tools: segment.tools } : {}),
      });
    }
    // Tools after the last sealed text ran before the final text: they head the final reply
    // (or stand alone when there is none).
    const tools = this.pendingTools;
    if (failed) {
      if (tools.length > 0) writes.push({ role: "assistant", text: "", tools });
      writes.push({
        role: "assistant",
        kind: "failure",
        text: this.outcome?.reason ?? reason,
        failure,
      });
    } else if (finalText !== "" || tools.length > 0) {
      writes.push({ role: "assistant", text: finalText, ...(tools.length > 0 ? { tools } : {}) });
    }
    // The turn's reasoning stays with its first reply, behind the thinking section's mode.
    const reasoning = this.reasoningText.trim();
    const first = writes.find((m) => m.kind !== "failure");
    if (reasoning !== "" && first !== undefined) first.reasoning = reasoning;
    const append = transcriptStore.getState().append;
    for (const message of writes) append(message);
    this.pendingTools = [];
  }

  private sealStop(holdBusy: boolean): void {
    this.flusher.cancel();
    this.endReasoningPulse();
    const partial = this.buffer.trimStart();
    this.buffer = "";
    this.attachPendingTools();
    const append = transcriptStore.getState().append;
    for (const segment of this.segments) {
      append({
        role: "assistant",
        text: segment.text,
        kind: segment.kind === "diff" ? "diff" : "text",
        ...(segment.tools.length > 0 ? { tools: segment.tools } : {}),
      });
    }
    const running = this.activeTools.map((t) => ({
      ...t,
      status: "cancelled" as const,
      endedAt: this.clock.now(),
    }));
    let messageId: string | null = null;
    if (partial.trim() !== "" || running.length > 0) {
      messageId = append({
        role: "assistant",
        text: withMark(partial.trimEnd()),
        ...(running.length > 0 ? { tools: running } : {}),
      });
    } else {
      this.sys(NOTHING_STREAMED_NOTE);
    }
    this.sealed = { messageId, partial, holdBusy };
    this.segments = [];
    this.activeTools = [];
    this.pendingTools = [];
    turnStore.getState().patch({
      streamBuffer: "",
      segments: [],
      activeTools: [],
      pendingTools: [],
      activity: [],
      reasoningActive: false,
    });
    overlayStore.getState().closeFlowCards(this.turnId ?? undefined);
    if (holdBusy) {
      uiStore.getState().patch({ busy: true });
      this.setStatus(STATUS_INTERRUPTING);
      return;
    }
    uiStore.getState().patch({ busy: false });
    this.setStatus(STATUS_INTERRUPTED, INTERRUPT_COOLDOWN_MS);
    this.flushHeldNotice();
  }

  /** The end of a turn stopped locally: take the stored partial when it says more (R05.4). */
  private reconcileStop(text: string, reason: string): void {
    const sealed = this.sealed;
    if (sealed === null) return;
    const stored = text.trim();
    const shown = sealed.partial.trim();
    if (stored.length > shown.length && stored.startsWith(shown)) {
      const full = reason.startsWith("interrupted") ? withMark(stored) : stored;
      if (sealed.messageId !== null)
        transcriptStore.getState().update(sealed.messageId, { text: full });
      else transcriptStore.getState().append({ role: "assistant", text: full });
    }
  }

  private remember(turnId: string): void {
    if (!this.finished.includes(turnId)) {
      this.finished.push(turnId);
      if (this.finished.length > FINISHED_TURN_MEMORY) this.finished.shift();
    }
  }

  private clearTurn(): void {
    this.flusher.cancel();
    this.clearTimer("pulse");
    this.turnId = null;
    this.generation = null;
    this.buffer = "";
    this.segments = [];
    this.pendingTools = [];
    this.activeTools = [];
    this.trail = [];
    this.reasoningText = "";
    this.outcome = null;
    this.sealed = null;
  }

  /** The settle edge: per-turn state cleared, busy off, the finished turn's flow cards closed. */
  private idle(): void {
    const turnId = this.turnId;
    const held = this.sealed;
    this.clearTurn();
    // The task list is the session's, not the turn's: it stays until the next update.
    const { todos } = turnStore.getState();
    turnStore.getState().reset();
    if (todos.length > 0) turnStore.getState().patch({ todos });
    overlayStore.getState().closeFlowCards(turnId ?? undefined);
    const wasBusy = uiStore.getState().busy;
    uiStore.getState().patch({ busy: false, compacting: false });
    // After a plain stop the "interrupted" status runs its own cooldown; otherwise say ready.
    if (held === null || held.holdBusy || wasBusy) this.setStatus(STATUS_READY);
    this.flushHeldNotice();
  }
}
