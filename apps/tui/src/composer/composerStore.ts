// The composer as a whole (P12-16/17/19): the editor model, the continuation lines finished with
// a trailing backslash, the tokens standing behind labels in the text, history browsing and the
// completion list. One instance per app (`composer`); tests make
// their own with `new Composer()`.

import { useSyncExternalStore } from "react";
import type { KeyPress } from "../input/keymap.ts";
import { applyKey, EditorModel, type KeyOutcome } from "./editorModel.ts";

export interface PasteToken {
  readonly kind: "paste";
  readonly label: string;
  readonly text: string;
  /** Where `paste.collapse` kept the full text, once it answered. */
  path?: string;
}

export interface ImageToken {
  readonly kind: "image";
  readonly label: string;
  readonly path: string;
  readonly name: string;
}

export type ComposerToken = PasteToken | ImageToken;

export interface CompletionRow {
  readonly text: string;
  readonly display: string;
  readonly meta?: string;
  /** Set on the "unavailable" row: shown, never applied. */
  readonly disabled?: boolean;
}

export interface CompletionState {
  readonly items: readonly CompletionRow[];
  readonly index: number;
  readonly replaceFrom: number;
}

export const MAX_TOKENS = 32;

const NO_COMPLETION: CompletionState = { items: [], index: 0, replaceFrom: 0 };

export class Composer {
  readonly editor = new EditorModel();
  continuation: string[] = [];
  tokens: ComposerToken[] = [];
  /** Bumped whenever the content (text, continuation, tokens) changes: stale-result guard. */
  revision = 0;
  /** Bumped on every change at all, for React. */
  version = 0;
  historyIndex: number | null = null;
  historyDraft = "";
  completion: CompletionState = NO_COMPLETION;
  private pasteCount = 0;
  private imageCount = 0;
  private readonly listeners = new Set<() => void>();

  get text(): string {
    return this.editor.text;
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getVersion = (): number => this.version;

  /** Text, continuation lines or tokens present: Ctrl+C clears, Ctrl+D refuses to quit. */
  hasDraft(): boolean {
    return this.text !== "" || this.continuation.length > 0 || this.tokens.length > 0;
  }

  /** Continuation lines and the live line, joined the way they are sent. */
  fullDraft(): string {
    return [...this.continuation, this.text].join("\n");
  }

  /** Announce a change. Content changes bump the revision and drop tokens whose label is gone. */
  notify(content = true): void {
    if (content) {
      this.revision += 1;
      const draft = this.fullDraft();
      const kept = this.tokens.filter((token) => draft.includes(token.label));
      if (kept.length !== this.tokens.length) this.tokens = kept;
    }
    this.version += 1;
    for (const listener of this.listeners) listener();
  }

  /** Feed one key to the editor; content changes are announced. */
  key(k: KeyPress): KeyOutcome {
    const before = this.editor.text;
    const outcome = applyKey(this.editor, k);
    this.notify(this.editor.text !== before);
    return outcome;
  }

  /** Replace the live line from outside (history, queue edit, completion); undo starts afresh. */
  setText(text: string, cursor: number = text.length): void {
    this.editor.load(text, cursor);
    this.notify();
  }

  /** Insert typed or pasted text at the cursor as one undoable edit. */
  insertText(text: string): void {
    if (this.editor.insert(text)) this.notify();
  }

  /** Insert a token label, padded with a space where it would touch other text. */
  insertLabel(label: string): void {
    const { text, cursor } = this.editor;
    const before = cursor > 0 && !/\s/.test(text[cursor - 1] ?? "") ? " " : "";
    const after = cursor < text.length && !/\s/.test(text[cursor] ?? "") ? " " : "";
    this.editor.insert(`${before}${label}${after}`);
  }

  /** Add a token and its label at the cursor. The oldest tokens give way past `MAX_TOKENS`. */
  addToken(token: ComposerToken): void {
    this.insertLabel(token.label);
    this.tokens = [...this.tokens, token].slice(-MAX_TOKENS);
    this.notify();
  }

  nextPasteNumber(): number {
    this.pasteCount += 1;
    return this.pasteCount;
  }

  nextImageNumber(): number {
    this.imageCount += 1;
    return this.imageCount;
  }

  /** The live line moves up into the continuation lines (trailing backslash + Enter). */
  pushContinuation(line: string): void {
    this.continuation = [...this.continuation, line];
    this.editor.load("");
    this.notify();
  }

  /** Empty everything: text, continuation, tokens, history browsing, completion list. */
  clear(): void {
    this.editor.load("");
    this.continuation = [];
    this.tokens = [];
    this.historyIndex = null;
    this.historyDraft = "";
    this.completion = NO_COMPLETION;
    this.notify();
  }

  setCompletion(next: CompletionState): void {
    this.completion = next;
    this.notify(false);
  }

  clearCompletion(): void {
    if (this.completion.items.length === 0 && this.completion.index === 0) return;
    this.setCompletion(NO_COMPLETION);
  }

  /** Move the highlighted completion, wrapping at both ends. */
  cycleCompletion(step: 1 | -1): void {
    const count = this.completion.items.length;
    if (count === 0) return;
    this.setCompletion({
      ...this.completion,
      index: (this.completion.index + step + count) % count,
    });
  }

  /** Up past the first line: older history entry; the draft is kept to come back to. */
  historyBack(entries: readonly string[]): boolean {
    if (entries.length === 0) return false;
    if (this.historyIndex === null) this.historyDraft = this.text;
    const index =
      this.historyIndex === null ? entries.length - 1 : Math.max(0, this.historyIndex - 1);
    this.historyIndex = index;
    this.setText(entries[index] ?? "");
    return true;
  }

  /** Down past the last line: newer entry, then the saved draft. */
  historyForward(entries: readonly string[]): boolean {
    if (this.historyIndex === null) return false;
    const index = this.historyIndex + 1;
    if (index >= entries.length) {
      this.historyIndex = null;
      this.setText(this.historyDraft);
    } else {
      this.historyIndex = index;
      this.setText(entries[index] ?? "");
    }
    return true;
  }
}

export const composer = new Composer();

/** Re-render on any composer change; returns the composer for reading. */
export function useComposer(target: Composer = composer): Composer {
  useSyncExternalStore(target.subscribe, target.getVersion, target.getVersion);
  return target;
}
