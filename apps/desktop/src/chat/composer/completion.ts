// Composer completion (P20-19): which trigger the caret is in, asking the daemon for candidates,
// dropping answers that arrive after a newer question, and applying a picked candidate.
//
// Triggers: `/` at the very start of the text (slash commands, curated by P20-24) and `@word`
// anywhere after whitespace (paths, `complete.path`).

import type { CompletionItem, MethodName, MethodTypes } from "@indus/protocol";
import type { RequestOptions } from "../../gateway/connectionStore.ts";
import { completeSlash } from "../slash.ts";

export type TriggerKind = "slash" | "mention";

export interface Trigger {
  readonly kind: TriggerKind;
  /** Where the replaced word starts (the `/` or `@`). */
  readonly start: number;
  /** Where it ends (the caret). */
  readonly end: number;
  /** The word typed so far, trigger character included. */
  readonly word: string;
}

/** The trigger under the caret, or null. */
export function findTrigger(text: string, caret: number): Trigger | null {
  const before = text.slice(0, caret);
  if (before.startsWith("/") && !/\s/.test(before)) {
    return { kind: "slash", start: 0, end: caret, word: before };
  }
  const match = /(^|\s)(@[^\s@]*)$/.exec(before);
  if (match?.[2] !== undefined) {
    const word = match[2];
    return { kind: "mention", start: caret - word.length, end: caret, word };
  }
  return null;
}

/** The text and caret after taking `item` for `trigger`. */
export function applyCompletion(
  text: string,
  trigger: Trigger,
  item: CompletionItem,
): { text: string; caret: number } {
  let insert = item.text;
  if (trigger.kind === "mention" && !insert.startsWith("@")) insert = `@${insert}`;
  const after = text.slice(trigger.end);
  // A finished command or file reference gets a space; a folder keeps the caret inside it.
  const space = !insert.endsWith("/") && !after.startsWith(" ") ? " " : "";
  const next = `${text.slice(0, trigger.start)}${insert}${space}${after}`;
  return { text: next, caret: trigger.start + insert.length + space.length };
}

type Request = <M extends MethodName>(
  method: M,
  params: MethodTypes[M]["params"],
  options?: RequestOptions,
) => Promise<MethodTypes[M]["result"]>;

export interface CompletionState {
  readonly trigger: Trigger | null;
  readonly items: readonly CompletionItem[];
  readonly selected: number;
}

export const EMPTY_COMPLETION: CompletionState = { trigger: null, items: [], selected: 0 };

/** Debounce for asking the daemon while the person types. */
export const COMPLETION_DEBOUNCE_MS = 60;

export interface CompletionSourceOptions {
  request: Request | null;
  sessionId: string | null;
  onChange: (state: CompletionState) => void;
  debounceMs?: number;
}

/**
 * Asks for candidates as the text changes. Each question gets a generation; an answer for an
 * older generation (the person kept typing) is dropped, so a slow answer never replaces a newer
 * list.
 */
export class CompletionSource {
  private readonly options: CompletionSourceOptions;
  private generation = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private controller: AbortController | null = null;
  private state: CompletionState = EMPTY_COMPLETION;

  constructor(options: CompletionSourceOptions) {
    this.options = options;
  }

  get current(): CompletionState {
    return this.state;
  }

  private publish(state: CompletionState): void {
    this.state = state;
    this.options.onChange(state);
  }

  /** The text or caret changed. */
  update(text: string, caret: number): void {
    const trigger = findTrigger(text, caret);
    this.generation += 1;
    const mine = this.generation;
    if (this.timer !== null) clearTimeout(this.timer);
    this.controller?.abort();
    this.controller = null;
    if (trigger === null) {
      if (this.state.trigger !== null) this.publish(EMPTY_COMPLETION);
      return;
    }
    const ask = () => {
      this.timer = null;
      void this.ask(trigger, mine);
    };
    const wait = this.options.debounceMs ?? COMPLETION_DEBOUNCE_MS;
    if (wait <= 0) ask();
    else this.timer = setTimeout(ask, wait);
  }

  private async ask(trigger: Trigger, mine: number): Promise<void> {
    const controller = new AbortController();
    this.controller = controller;
    let items: CompletionItem[];
    try {
      items = await this.fetch(trigger, controller.signal);
    } catch {
      items = [];
    }
    if (mine !== this.generation) return;
    this.publish({ trigger, items, selected: 0 });
  }

  private async fetch(trigger: Trigger, signal: AbortSignal): Promise<CompletionItem[]> {
    const { request, sessionId } = this.options;
    if (trigger.kind === "slash") return completeSlash(trigger.word, request, sessionId, signal);
    if (request === null) return [];
    const answer = await request(
      "complete.path",
      { word: trigger.word.slice(1), session_id: sessionId },
      { timeoutMs: 10_000, signal },
    );
    return answer.items;
  }

  /** Move the highlight (wraps). */
  move(delta: number): void {
    const count = this.state.items.length;
    if (count === 0) return;
    const selected = (this.state.selected + delta + count) % count;
    this.publish({ ...this.state, selected });
  }

  close(): void {
    this.generation += 1;
    if (this.timer !== null) clearTimeout(this.timer);
    this.controller?.abort();
    this.publish(EMPTY_COMPLETION);
  }

  dispose(): void {
    this.generation += 1;
    if (this.timer !== null) clearTimeout(this.timer);
    this.controller?.abort();
  }
}

/**
 * Recall of earlier prompts with Up/Down. It only moves while the composer is empty or still shows
 * the entry it recalled, so arrows inside typed text are left to the editor.
 */
export class PromptHistory {
  private readonly entries: string[] = [];
  private cursor: number | null = null;
  private readonly limit: number;

  constructor(limit = 200) {
    this.limit = limit;
  }

  push(text: string): void {
    const trimmed = text.trim();
    if (trimmed === "") return;
    if (this.entries.at(-1) !== trimmed) this.entries.push(trimmed);
    if (this.entries.length > this.limit) this.entries.shift();
    this.cursor = null;
  }

  /** True when an arrow should recall instead of moving the caret. */
  canRecall(current: string): boolean {
    if (current === "") return true;
    return this.cursor !== null && this.entries[this.cursor] === current;
  }

  /** The entry one step older (`-1`) or newer (`+1`); null when nothing changes. */
  step(direction: -1 | 1, current: string): string | null {
    if (!this.canRecall(current) || this.entries.length === 0) return null;
    if (this.cursor === null) {
      if (direction === 1) return null;
      this.cursor = this.entries.length - 1;
      return this.entries[this.cursor] ?? null;
    }
    const next = this.cursor + direction;
    if (next < 0) return null;
    if (next >= this.entries.length) {
      this.cursor = null;
      return "";
    }
    this.cursor = next;
    return this.entries[next] ?? null;
  }

  reset(): void {
    this.cursor = null;
  }
}
