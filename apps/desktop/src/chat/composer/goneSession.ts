// A session that vanished under the composer (P20-19). When the daemon confirms the session is
// gone (deleted, or continued elsewhere), the window drops to a fresh draft that adopts the unsent
// text ONCE and shows an inline "restored" strip with Undo. No toast, no navigation of its own, no
// focus change; a fresh draft that already has text is never overwritten (the old text is then
// only offered on the strip).

import type { MethodName, MethodTypes } from "@indus/protocol";
import type { RequestOptions } from "../../gateway/connectionStore.ts";
import { errorName } from "../rpcErrors.ts";

/** The draft key of the window's fresh (not yet created) conversation. */
export const FRESH = "";

export interface RestoredStrip {
  readonly fromSessionId: string;
  readonly text: string;
  /** True when the text went into the fresh draft (Undo puts the fresh draft back). */
  readonly adopted: boolean;
  /** The fresh draft before the adoption. */
  readonly previousFresh: string;
}

export interface DraftState {
  /** Unsent text per session id (`FRESH` for the new-chat draft). */
  readonly drafts: Readonly<Record<string, string>>;
  /** Sessions whose text was already handled. */
  readonly handled: readonly string[];
  readonly restored: RestoredStrip | null;
}

export const EMPTY_DRAFTS: DraftState = { drafts: {}, handled: [], restored: null };

export function draftOf(state: DraftState, key: string): string {
  return state.drafts[key] ?? "";
}

export function setDraft(state: DraftState, key: string, text: string): DraftState {
  if (draftOf(state, key) === text) return state;
  return { ...state, drafts: { ...state.drafts, [key]: text } };
}

/** Move the unsent text of a gone session to the fresh draft, once. */
export function adoptGoneDraft(state: DraftState, goneSessionId: string): DraftState {
  if (goneSessionId === FRESH || state.handled.includes(goneSessionId)) return state;
  const text = draftOf(state, goneSessionId);
  const drafts = { ...state.drafts };
  delete drafts[goneSessionId];
  const handled = [...state.handled, goneSessionId];
  if (text.trim() === "") return { ...state, drafts, handled };
  const fresh = draftOf(state, FRESH);
  const adopted = fresh.trim() === "";
  if (adopted) drafts[FRESH] = text;
  return {
    drafts,
    handled,
    restored: { fromSessionId: goneSessionId, text, adopted, previousFresh: fresh },
  };
}

/** Undo the strip: the fresh draft goes back to what it was before the adoption. */
export function undoRestore(state: DraftState): DraftState {
  const strip = state.restored;
  if (strip === null) return state;
  const drafts = { ...state.drafts };
  if (strip.adopted) drafts[FRESH] = strip.previousFresh;
  return { ...state, drafts, restored: null };
}

export function dismissRestore(state: DraftState): DraftState {
  return state.restored === null ? state : { ...state, restored: null };
}

/** A small store the composer and the route share. */
export class DraftStore {
  private state: DraftState = EMPTY_DRAFTS;
  private readonly listeners = new Set<() => void>();

  snapshot = (): DraftState => this.state;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  update(change: (state: DraftState) => DraftState): void {
    const next = change(this.state);
    if (next === this.state) return;
    this.state = next;
    for (const listener of this.listeners) listener();
  }
}

/** One store per window. */
export const windowDrafts = new DraftStore();

type Request = <M extends MethodName>(
  method: M,
  params: MethodTypes[M]["params"],
  options?: RequestOptions,
) => Promise<MethodTypes[M]["result"]>;

/**
 * True only when the daemon confirms the session is gone (`SESSION_NOT_FOUND`, or refused as
 * deleted / continued elsewhere). A timeout or a dropped link proves nothing.
 */
export async function verifySessionGone(request: Request, sessionId: string): Promise<boolean> {
  try {
    await request("session.history", { session_id: sessionId }, { timeoutMs: 15_000 });
    return false;
  } catch (error) {
    const name = errorName(error);
    return name === "SESSION_NOT_FOUND" || name === "SESSION_REFUSED";
  }
}
