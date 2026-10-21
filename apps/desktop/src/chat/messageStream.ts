// The transcript model (P20-18): a pure reducer over two sources.
//
//   history  `session.history` rows — the persisted base (`applyHistory`)
//   events   the session's live stream — turns, deltas, tools, completions (`applyEvent`)
//   local    rows the composer submitted and the daemon has not stored yet (`addLocalUser`)
//
// Invariants (the oracle in testing/oracle.ts checks them): every persisted user/assistant
// message appears exactly once and in order; deltas are appended raw, so the reply on screen is
// the concatenation of what was streamed; a repeated event (replay) changes nothing; a snapshot
// replaces only the base and never drops live content newer than itself; a snapshot older than
// one already applied is ignored.

import type { ClientEvent } from "@indus/client";
import type { HistoryMessage } from "@indus/protocol";
import { classifyFailure, FAILED_EXITS, type FailureLayer } from "./failure.ts";
import { toolOutcome } from "./toolSummary.ts";

export type ToolState = "running" | "ok" | "failed" | "neutral";

export interface TextPart {
  readonly type: "text";
  readonly text: string;
}

export interface ReasoningPart {
  readonly type: "reasoning";
  readonly text: string;
}

export interface ToolPart {
  readonly type: "tool";
  readonly callId: string;
  readonly name: string;
  readonly args: Readonly<Record<string, unknown>> | null;
  readonly state: ToolState;
  readonly progress: string | null;
  readonly fraction: number | null;
  readonly result: string | null;
  readonly durationMs: number | null;
}

export type AssistantPart = TextPart | ReasoningPart | ToolPart;

export interface UserItem {
  readonly kind: "user";
  readonly key: string;
  readonly text: string;
  readonly turnId: string | null;
  readonly localId: string | null;
  readonly persisted: boolean;
  readonly rowId: number | null;
}

export interface AssistantItem {
  readonly kind: "assistant";
  readonly key: string;
  readonly turnId: string | null;
  readonly parts: readonly AssistantPart[];
  readonly streaming: boolean;
  readonly persisted: boolean;
  readonly interrupted: boolean;
  /** This window saw the turn start (it holds the whole turn, not a tail of it). */
  readonly sawStart: boolean;
}

export interface ErrorItem {
  readonly kind: "error";
  readonly key: string;
  readonly turnId: string;
  readonly layer: FailureLayer;
  /** The failure sentence, or empty (then the exit reason speaks). */
  readonly message: string;
  /** The turn's exit reason. */
  readonly reason: string;
  readonly retryable: boolean;
  readonly resetsAt: string | null;
}

export interface AsyncItem {
  readonly kind: "async";
  readonly key: string;
  readonly title: string;
  readonly lines: readonly string[];
}

export interface NoticeItem {
  readonly kind: "notice";
  readonly key: string;
  readonly level: "info" | "warning" | "error";
  readonly text: string;
}

export type TranscriptItem = UserItem | AssistantItem | ErrorItem | AsyncItem | NoticeItem;

export interface SessionTranscript {
  readonly sessionId: string;
  readonly items: readonly TranscriptItem[];
  /** A turn is in flight. */
  readonly running: boolean;
  /** The running (or last) turn. */
  readonly turnId: string | null;
  /** The running turn's generation (for interrupts). */
  readonly generation: number | null;
  /** History was loaded at least once. */
  readonly hydrated: boolean;
  /** The newest event seq applied (within `epoch`). */
  readonly lastSeq: number;
  readonly epoch: string | null;
  /** The generation of the snapshot in `items` (older snapshots are ignored). */
  readonly historyGeneration: number;
  /** A replay gap: the base must be loaded again. */
  readonly needsSnapshot: boolean;
}

export function emptyTranscript(sessionId: string): SessionTranscript {
  return {
    sessionId,
    items: [],
    running: false,
    turnId: null,
    generation: null,
    hydrated: false,
    lastSeq: 0,
    epoch: null,
    historyGeneration: 0,
    needsSnapshot: false,
  };
}

// -- small helpers --------------------------------------------------------------------------------

const rec = (value: unknown): Record<string, unknown> =>
  typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
const str = (value: unknown): string => (typeof value === "string" ? value : "");
const num = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

/** Whitespace-insensitive comparison form. */
export function normalizeText(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

/** The visible reply text of an assistant item (text parts, in order). */
export function assistantText(item: AssistantItem): string {
  return item.parts
    .filter((part): part is TextPart => part.type === "text")
    .map((part) => part.text)
    .join("");
}

function replaceAt<T>(list: readonly T[], index: number, value: T): T[] {
  const next = [...list];
  next[index] = value;
  return next;
}

function findAssistant(items: readonly TranscriptItem[], turnId: string): number {
  for (let i = items.length - 1; i >= 0; i -= 1) {
    const item = items[i];
    if (item?.kind === "assistant" && item.turnId === turnId) return i;
  }
  return -1;
}

/** The assistant item of `turnId`, created (after its user row, if any) when missing. */
function ensureAssistant(
  t: SessionTranscript,
  turnId: string,
  sawStart: boolean,
): { items: TranscriptItem[]; index: number } {
  const items = [...t.items];
  const found = findAssistant(items, turnId);
  if (found >= 0) {
    const item = items[found] as AssistantItem;
    if (sawStart && !item.sawStart) items[found] = { ...item, sawStart: true };
    return { items, index: found };
  }
  let userIndex = -1;
  for (let i = items.length - 1; i >= 0; i -= 1) {
    const item = items[i];
    if (item?.kind === "user" && item.turnId === turnId) {
      userIndex = i;
      break;
    }
  }
  if (userIndex < 0 && sawStart) {
    // A local row the composer has not bound yet (the turn started before the submit answer).
    const last = items.findLastIndex((item) => item.kind === "user");
    const candidate = items[last];
    if (candidate?.kind === "user" && !candidate.persisted && candidate.turnId === null) {
      items[last] = { ...candidate, turnId };
      userIndex = last;
    }
  }
  const created: AssistantItem = {
    kind: "assistant",
    key: `turn:${turnId}`,
    turnId,
    parts: [],
    streaming: true,
    persisted: false,
    interrupted: false,
    sawStart,
  };
  let at = items.length;
  if (userIndex >= 0) {
    at = userIndex + 1;
    // Keep other users bound to the same turn (queued, steered) above the reply.
    while (at < items.length) {
      const next = items[at];
      if (next?.kind === "user" && next.turnId === turnId) at += 1;
      else break;
    }
  }
  items.splice(at, 0, created);
  return { items, index: at };
}

function withParts(
  t: SessionTranscript,
  turnId: string,
  change: (item: AssistantItem) => AssistantItem,
  sawStart = false,
): SessionTranscript {
  const { items, index } = ensureAssistant(t, turnId, sawStart);
  const item = items[index] as AssistantItem;
  return { ...t, items: replaceAt(items, index, change(item)) };
}

function appendText(item: AssistantItem, type: "text" | "reasoning", text: string): AssistantItem {
  if (text === "") return item;
  const parts = [...item.parts];
  const last = parts.at(-1);
  if (last !== undefined && last.type === type) {
    parts[parts.length - 1] = { type, text: last.text + text };
  } else {
    parts.push({ type, text });
  }
  return { ...item, parts };
}

function updateTool(
  item: AssistantItem,
  callId: string,
  change: (part: ToolPart | null) => ToolPart,
): AssistantItem {
  const index = item.parts.findIndex((part) => part.type === "tool" && part.callId === callId);
  if (index < 0) return { ...item, parts: [...item.parts, change(null)] };
  return { ...item, parts: replaceAt(item.parts, index, change(item.parts[index] as ToolPart)) };
}

function newTool(callId: string, name: string): ToolPart {
  return {
    type: "tool",
    callId,
    name,
    args: null,
    state: "running",
    progress: null,
    fraction: null,
    result: null,
    durationMs: null,
  };
}

/**
 * Settle the final reply against what streamed: only what is not on screen is added. The final
 * text is matched against the text after the last tool call first (the closing message), then
 * against the whole reply; when the stream lost characters inside the closing message, that
 * message becomes the final text (it is what gets stored).
 */
export function reconcileFinal(item: AssistantItem, finalText: string): AssistantItem {
  if (normalizeText(finalText) === "") return item;
  const parts = [...item.parts];
  let lastTool = -1;
  parts.forEach((part, index) => {
    if (part.type === "tool") lastTool = index;
  });
  const tailIndexes: number[] = [];
  for (let i = lastTool + 1; i < parts.length; i += 1) {
    if (parts[i]?.type === "text") tailIndexes.push(i);
  }
  const tail = tailIndexes.map((i) => (parts[i] as TextPart).text).join("");
  const whole = assistantText(item);
  if (normalizeText(tail) === normalizeText(finalText)) return item;
  if (normalizeText(whole) === normalizeText(finalText)) return item;
  if (finalText.startsWith(tail) && tail !== "") {
    return appendText(item, "text", finalText.slice(tail.length));
  }
  if (whole !== "" && finalText.startsWith(whole)) {
    return appendText(item, "text", finalText.slice(whole.length));
  }
  if (tail === "") return { ...item, parts: [...parts, { type: "text", text: finalText }] };
  // The stream lost text inside the closing message: the stored final replaces it.
  const kept = parts.filter((_, index) => !tailIndexes.includes(index));
  const at = tailIndexes[0] ?? kept.length;
  kept.splice(at, 0, { type: "text", text: finalText });
  return { ...item, parts: kept };
}

function upsertKeyed(items: readonly TranscriptItem[], item: NoticeItem): TranscriptItem[] {
  const index = items.findIndex((other) => other.key === item.key);
  return index < 0 ? [...items, item] : replaceAt(items, index, item);
}

// -- events -----------------------------------------------------------------------------------------

/** Apply one event of this session. Repeats (seq at or below the applied one) change nothing. */
export function applyEvent(t: SessionTranscript, event: ClientEvent): SessionTranscript {
  const body = rec(event);
  if (body.session_id !== undefined && body.session_id !== t.sessionId) return t;
  const type = str(body.type);
  if (type === "session.replay_gap") {
    return { ...t, needsSnapshot: true, lastSeq: num(body.last_seq) ?? t.lastSeq };
  }
  const seq = num(body.seq);
  let next = t;
  if (seq !== null) {
    const epoch = typeof body.replay_epoch === "string" ? body.replay_epoch : t.epoch;
    if (epoch === t.epoch && seq <= t.lastSeq) return t;
    next = { ...t, lastSeq: seq, epoch };
  }
  const turnId = str(body.turn_id);
  switch (type) {
    case "turn.started": {
      const started = withParts(next, turnId, (item) => item, true);
      return {
        ...started,
        running: true,
        turnId,
        generation: num(body.generation),
      };
    }
    case "message.delta":
      return withParts(next, turnId, (item) => appendText(item, "text", str(body.text)));
    case "reasoning.delta":
      return withParts(next, turnId, (item) => appendText(item, "reasoning", str(body.text)));
    case "tool.started": {
      const callId = str(body.call_id);
      const args = body.arguments === undefined ? null : rec(body.arguments);
      return withParts(next, turnId, (item) =>
        updateTool(item, callId, (part) => ({
          ...(part ?? newTool(callId, str(body.name))),
          name: str(body.name) || part?.name || "",
          args,
        })),
      );
    }
    case "tool.progress": {
      const callId = str(body.call_id);
      return withParts(next, turnId, (item) =>
        updateTool(item, callId, (part) => ({
          ...(part ?? newTool(callId, "")),
          progress: str(body.message) || null,
          fraction: num(body.fraction),
        })),
      );
    }
    case "tool.completed": {
      const callId = str(body.call_id);
      const result = str(body.result);
      return withParts(next, turnId, (item) =>
        updateTool(item, callId, (part) => ({
          ...(part ?? newTool(callId, str(body.name))),
          name: str(body.name) || part?.name || "",
          state: toolOutcome(body.ok === true, result),
          result,
          durationMs: num(body.duration_ms),
          progress: null,
        })),
      );
    }
    case "message.complete":
      return withParts(next, turnId, (item) => ({
        ...reconcileFinal(item, str(body.text)),
        streaming: false,
      }));
    case "turn.done":
      return finishTurn(next, turnId, str(body.reason), body.failure);
    case "notice": {
      const level = body.level === "error" ? "error" : body.level === "warning" ? "warning" : null;
      if (level === null) return next;
      const key = `notice:${str(body.key) || String(seq ?? next.items.length)}`;
      return {
        ...next,
        items: upsertKeyed(next.items, { kind: "notice", key, level, text: str(body.text) }),
      };
    }
    case "learning.review.done": {
      const lines = Array.isArray(body.summary_lines) ? body.summary_lines.map(String) : [];
      if (lines.length === 0) return next;
      const item: AsyncItem = {
        kind: "async",
        key: `async:review:${String(seq)}`,
        title: "review",
        lines,
      };
      return { ...next, items: [...next.items, item] };
    }
    case "heartbeat.fired": {
      const item: AsyncItem = {
        kind: "async",
        key: `async:heartbeat:${str(body.heartbeat_id)}:${String(num(body.fire_count) ?? seq)}`,
        title: "heartbeat",
        lines: [str(body.prompt)],
      };
      return { ...next, items: [...next.items, item] };
    }
    default:
      return next;
  }
}

function finishTurn(
  t: SessionTranscript,
  turnId: string,
  reason: string,
  failure: unknown,
): SessionTranscript {
  const sealed = withParts(t, turnId, (item) => ({
    ...item,
    streaming: false,
    interrupted: reason === "interrupted_by_user" || reason === "interrupted_by_system",
    parts: item.parts.map((part) =>
      part.type === "tool" && part.state === "running" ? { ...part, state: "neutral" } : part,
    ),
  }));
  const running = sealed.turnId === turnId ? false : sealed.running;
  const failed = failure !== null && failure !== undefined;
  if (!failed && !FAILED_EXITS.has(reason)) return { ...sealed, running };
  const info = rec(failure);
  const key = `error:${turnId}`;
  if (sealed.items.some((item) => item.key === key)) return { ...sealed, running };
  const message = str(info.reason);
  const error: ErrorItem = {
    kind: "error",
    key,
    turnId,
    layer: classifyFailure(reason, message),
    message,
    reason,
    retryable: info.retryable === true,
    resetsAt: typeof info.resets_at === "string" ? info.resets_at : null,
  };
  const index = findAssistant(sealed.items, turnId);
  const items = [...sealed.items];
  items.splice(index + 1, 0, error);
  return { ...sealed, items, running };
}

// -- local rows -------------------------------------------------------------------------------------

export function addLocalUser(
  t: SessionTranscript,
  localId: string,
  text: string,
): SessionTranscript {
  const item: UserItem = {
    kind: "user",
    key: `local:${localId}`,
    text,
    turnId: null,
    localId,
    persisted: false,
    rowId: null,
  };
  return { ...t, items: [...t.items, item] };
}

export function bindLocalUser(
  t: SessionTranscript,
  localId: string,
  turnId: string,
): SessionTranscript {
  const index = t.items.findIndex((item) => item.kind === "user" && item.localId === localId);
  if (index < 0) return t;
  const item = t.items[index] as UserItem;
  if (item.turnId === turnId) return t;
  return { ...t, items: replaceAt(t.items, index, { ...item, turnId }) };
}

export function dropLocalUser(t: SessionTranscript, localId: string): SessionTranscript {
  const items = t.items.filter((item) => !(item.kind === "user" && item.localId === localId));
  return items.length === t.items.length ? t : { ...t, items };
}

// -- history ----------------------------------------------------------------------------------------

/** History rows → persisted items: one assistant item per answer, tool rows folded into it. */
export function historyItems(rows: readonly HistoryMessage[]): TranscriptItem[] {
  const items: TranscriptItem[] = [];
  rows.forEach((row, index) => {
    const key = `row:${row.row_id ?? `i${index}`}`;
    if (row.role === "user") {
      items.push({
        kind: "user",
        key,
        text: row.text,
        turnId: null,
        localId: null,
        persisted: true,
        rowId: row.row_id ?? null,
      });
      return;
    }
    let last = items.at(-1);
    if (last?.kind !== "assistant") {
      last = {
        kind: "assistant",
        key,
        turnId: null,
        parts: [],
        streaming: false,
        persisted: true,
        interrupted: false,
        sawStart: true,
      };
      items.push(last);
    }
    const parts = [...last.parts];
    if (row.role === "assistant") {
      if (row.text !== "") parts.push({ type: "text", text: row.text });
    } else {
      parts.push({
        ...newTool(`row:${row.row_id ?? index}`, row.name ?? "tool"),
        state: /^(error|failed)\b/i.test(row.text.trim()) ? "failed" : "ok",
        result: row.text,
      });
    }
    items[items.length - 1] = { ...last, parts };
  });
  return items;
}

interface LiveGroup {
  user: UserItem | null;
  assistant: AssistantItem | null;
  /** Errors, notices and async rows that came with this group. */
  extras: TranscriptItem[];
  /** The persisted row this group followed (its match is searched after it). */
  afterKey: string | null;
}

function liveGroups(items: readonly TranscriptItem[]): LiveGroup[] {
  const groups: LiveGroup[] = [];
  let current: LiveGroup | null = null;
  let afterKey: string | null = null;
  for (const item of items) {
    if ((item.kind === "user" || item.kind === "assistant") && item.persisted) {
      afterKey = item.key;
      continue;
    }
    if (item.kind === "user") {
      current = { user: item, assistant: null, extras: [], afterKey };
      groups.push(current);
      continue;
    }
    if (item.kind === "assistant") {
      const belongs =
        current !== null &&
        current.assistant === null &&
        (current.user === null ||
          current.user.turnId === null ||
          current.user.turnId === item.turnId);
      if (belongs && current !== null) {
        current.assistant = item;
      } else {
        current = { user: null, assistant: item, extras: [], afterKey };
        groups.push(current);
      }
      continue;
    }
    if (current === null) {
      current = { user: null, assistant: null, extras: [], afterKey };
      groups.push(current);
    }
    current.extras.push(item);
  }
  return groups;
}

function segmentEnd(base: readonly TranscriptItem[], from: number): number {
  let end = from;
  while (end < base.length && base[end]?.kind !== "user") end += 1;
  return end;
}

function segmentText(base: readonly TranscriptItem[], from: number, to: number): string {
  return base
    .slice(from, to)
    .filter((item): item is AssistantItem => item.kind === "assistant")
    .map((item) => assistantText(item))
    .join(" ");
}

function sameReply(stored: string, live: AssistantItem): boolean {
  const a = normalizeText(stored);
  const whole = normalizeText(assistantText(live));
  if (a === "" || whole === "") return a === whole && live.parts.length === 0;
  if (a === whole) return true;
  const lastText = [...live.parts].reverse().find((part) => part.type === "text");
  const closing = lastText === undefined ? "" : normalizeText(lastText.text);
  return closing !== "" && a.endsWith(closing);
}

/**
 * Replace the persisted base with `rows` (snapshot `generation`) and keep every live row the
 * snapshot does not hold yet. A snapshot older than the one applied is ignored.
 */
export function applyHistory(
  t: SessionTranscript,
  rows: readonly HistoryMessage[],
  generation: number,
): SessionTranscript {
  if (generation < t.historyGeneration) return t;
  // Stored user rows keep the turn they were bound to by an earlier snapshot.
  const boundTurns = new Map<string, string>();
  for (const item of t.items) {
    if (item.kind === "user" && item.persisted && item.turnId !== null) {
      boundTurns.set(item.key, item.turnId);
    }
  }
  const base = historyItems(rows).map((item) => {
    const turnId = item.kind === "user" ? boundTurns.get(item.key) : undefined;
    return turnId === undefined ? item : { ...item, turnId };
  });
  const out: TranscriptItem[] = [];
  let cursor = 0;
  let behind = false;
  const flushBase = (to: number): void => {
    while (cursor < to) {
      const item = base[cursor];
      if (item !== undefined) out.push(item);
      cursor += 1;
    }
  };
  const baseIndex = new Map(base.map((item, index) => [item.key, index]));
  let floor = 0;
  const findUser = (match: (item: UserItem) => boolean): number => {
    for (let i = Math.max(cursor, floor); i < base.length; i += 1) {
      const item = base[i];
      if (item?.kind === "user" && match(item)) return i;
    }
    return -1;
  };
  const tail: TranscriptItem[] = [];
  // After a replay gap the live stream is broken: the snapshot is the truth for every turn;
  // only rows the composer submitted and the daemon has not stored stay.
  const groups = t.needsSnapshot
    ? liveGroups(t.items).map((group) => ({ ...group, assistant: null, extras: [] }))
    : liveGroups(t.items);
  for (const group of groups) {
    if (behind) {
      tail.push(...groupItems(group));
      continue;
    }
    const live = group.assistant;
    const after = group.afterKey === null ? undefined : baseIndex.get(group.afterKey);
    floor = after === undefined ? floor : Math.max(floor, after + 1);
    let at = -1;
    if (group.user !== null) {
      const want = normalizeText(group.user.text);
      at = findUser((item) => normalizeText(item.text) === want);
    } else if (live?.turnId) {
      at = findUser((item) => item.turnId === live.turnId);
    }
    if (at >= 0) {
      const end = segmentEnd(base, at + 1);
      flushBase(at + 1);
      const turnId = group.user?.turnId ?? live?.turnId ?? null;
      const user = out.at(-1);
      if (user?.kind === "user" && turnId !== null) out[out.length - 1] = { ...user, turnId };
      if (live === null) {
        flushBase(end);
      } else if (!live.streaming && sameReply(segmentText(base, at + 1, end), live)) {
        flushBase(end);
      } else if (live.sawStart || end === at + 1) {
        // The snapshot holds less of this turn than the window: the live reply is the latest.
        cursor = end;
        out.push(live);
      } else {
        flushBase(end);
        out.push(live);
      }
      out.push(...group.extras);
      continue;
    }
    if (group.user === null && live !== null && !live.streaming) {
      // The reply may already be on screen from the snapshot: its user's group (a different
      // turn binding, e.g. an optimistic row) flushed the whole stored segment just before.
      let shown = false;
      for (let i = out.length - 1; i >= 0; i -= 1) {
        const item = out[i];
        if (item === undefined || item.kind === "user") break;
        if (item.kind === "assistant" && item.persisted && sameReply(assistantText(item), live)) {
          shown = true;
          break;
        }
      }
      if (shown) {
        out.push(...group.extras);
        continue;
      }
      let found = -1;
      for (let i = Math.max(cursor, floor); i < base.length; i += 1) {
        const item = base[i];
        if (item?.kind === "assistant" && sameReply(assistantText(item), live)) {
          found = i;
          break;
        }
      }
      if (found >= 0) {
        flushBase(found + 1);
        out.push(...group.extras);
        continue;
      }
    }
    if (group.user === null && live === null) {
      tail.push(...group.extras);
      continue;
    }
    behind = true;
    tail.push(...groupItems(group));
  }
  flushBase(base.length);
  return {
    ...t,
    items: [...out, ...tail],
    hydrated: true,
    historyGeneration: generation,
    needsSnapshot: false,
    ...(t.needsSnapshot ? { running: false } : {}),
  };
}

function groupItems(group: LiveGroup): TranscriptItem[] {
  const items: TranscriptItem[] = [];
  if (group.user !== null) items.push(group.user);
  if (group.assistant !== null) items.push(group.assistant);
  items.push(...group.extras);
  return items;
}
