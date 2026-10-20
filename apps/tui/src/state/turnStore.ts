// What the running turn has produced so far. The turn controller writes it; the transcript's live
// rows read it. Cleared at every turn start and after the turn settles into the transcript.

import { useStore } from "zustand";
import { createStore, type StoreApi } from "zustand/vanilla";

export type ToolStatus = "pending" | "running" | "done" | "error" | "cancelled";

export interface ToolRow {
  id: string;
  name: string;
  /** Present-tense label for the running row ("reading", "searching", …). */
  verb?: string;
  /** One-line argument preview. */
  args?: string;
  status: ToolStatus;
  /** The latest progress line, then the result preview. */
  output?: string;
  startedAt?: number;
  endedAt?: number;
  /** Nesting level for subagent rows (0 or absent: the turn's own tools). */
  depth?: number;
}

export interface Segment {
  id: string;
  text: string;
  tools: ToolRow[];
  /** `diff`: an inline diff shown as its own block. */
  kind?: "text" | "diff";
}

export interface TodoItem {
  id: string;
  text: string;
  status: string;
}

export type ActivityLevel = "info" | "warn" | "error";

export interface ActivityItem {
  id: string;
  text: string;
  level: ActivityLevel;
}

export interface TurnFailureView {
  layer: string;
  code: string;
  retryable: boolean;
}

export interface TurnOutcome {
  reason: string;
  failure?: TurnFailureView;
}

/** At most this many finished tool rows and activity lines are kept per turn. */
export const TRAIL_LIMIT = 8;
export const ACTIVITY_LIMIT = 8;

export interface TurnState {
  /** Raw reply text not yet sealed into a segment (flushed copy; see the flush scheduler). */
  streamBuffer: string;
  segments: Segment[];
  /** Finished tools waiting for a segment to attach to. */
  pendingTools: ToolRow[];
  activeTools: ToolRow[];
  trail: ToolRow[];
  activity: ActivityItem[];
  reasoning: string;
  todos: TodoItem[];
  outcome: TurnOutcome | null;
  /** Additions to the wave-8 contract. */
  reasoningActive: boolean;
  turnId: string | null;
  generation: number | null;
  patch(p: Partial<TurnState>): void;
  reset(): void;
}

type TurnData = Omit<TurnState, "patch" | "reset">;

export function turnDefaults(): TurnData {
  return {
    streamBuffer: "",
    segments: [],
    pendingTools: [],
    activeTools: [],
    trail: [],
    activity: [],
    reasoning: "",
    todos: [],
    outcome: null,
    reasoningActive: false,
    turnId: null,
    generation: null,
  };
}

export const turnStore: StoreApi<TurnState> = createStore<TurnState>()((set) => ({
  ...turnDefaults(),
  patch: (p) => set(p),
  reset: () => set(turnDefaults()),
}));

export function useTurn<T>(sel: (s: TurnState) => T): T {
  return useStore(turnStore, sel);
}

/** Keep the newest `limit` entries. */
export function keepLast<T>(items: readonly T[], limit: number): T[] {
  return items.length <= limit ? [...items] : items.slice(items.length - limit);
}

/**
 * `session.steer` params for the running turn: the daemon applies a control only to the turn it
 * names (generation fences, P17-05), so the running turn's generation goes along when known.
 */
export function steerParams(sessionId: string, text: string): Record<string, unknown> {
  const generation = turnStore.getState().generation;
  return generation === null
    ? { session_id: sessionId, text }
    : { session_id: sessionId, text, generation };
}
