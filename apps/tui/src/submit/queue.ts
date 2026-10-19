// The renderer's prompt queue (P12-19): prompts typed while a turn runs (busy mode `queue`, a
// refused steer, any prompt with an image) wait here and leave one at a time when the session
// settles. An item can be opened for editing; while one is open nothing drains. An item leaves
// the queue the moment it is handed to the bridge, so nothing is ever re-sent after a reconnect.

import { useSyncExternalStore } from "react";
import type { ComposerToken } from "../composer/composerStore.ts";

export interface QueueItem {
  readonly id: number;
  /** What is sent (paste labels already expanded; `{!cmd}` still raw). */
  readonly text: string;
  /** What the user sees in the queue and the transcript. */
  readonly display: string;
  /** Carries an image token: must go as a fresh turn, never as a steer. */
  readonly hasImage: boolean;
  /** The tokens behind the labels in `display`, restored when the item is edited. */
  readonly tokens: readonly ComposerToken[];
}

export class PromptQueue {
  private list: QueueItem[] = [];
  private nextId = 1;
  /** Index of the item open in the composer, or null. */
  editing: number | null = null;
  version = 0;
  private readonly listeners = new Set<() => void>();

  get items(): readonly QueueItem[] {
    return this.list;
  }

  get length(): number {
    return this.list.length;
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getVersion = (): number => this.version;

  private changed(): void {
    this.version += 1;
    for (const listener of this.listeners) listener();
  }

  make(text: string, display: string = text, tokens: readonly ComposerToken[] = []): QueueItem {
    const id = this.nextId;
    this.nextId += 1;
    const hasImage = tokens.some(
      (token) => token.kind === "image" && display.includes(token.label),
    );
    return {
      id,
      text,
      display,
      hasImage,
      tokens: tokens.filter((token) => display.includes(token.label)),
    };
  }

  /** Append at the back. */
  add(text: string, display: string = text, tokens: readonly ComposerToken[] = []): QueueItem {
    return this.push(this.make(text, display, tokens));
  }

  /** Append a made item at the back. */
  push(item: QueueItem): QueueItem {
    this.list = [...this.list, item];
    this.changed();
    return item;
  }

  /** Put an item at the front (an edited item, or one the bridge refused just now). */
  addFront(item: QueueItem): void {
    this.list = [item, ...this.list.filter((other) => other.id !== item.id)];
    if (this.editing !== null) this.editing += 1;
    this.changed();
  }

  /** Take the front item (it leaves the queue for good). */
  shift(): QueueItem | undefined {
    const [first, ...rest] = this.list;
    if (first === undefined) return undefined;
    this.list = rest;
    if (this.editing !== null) this.editing = this.editing === 0 ? null : this.editing - 1;
    this.changed();
    return first;
  }

  /** Remove the item at `index`; returns it. */
  removeAt(index: number): QueueItem | undefined {
    const item = this.list[index];
    if (item === undefined) return undefined;
    this.list = this.list.filter((_, at) => at !== index);
    if (this.editing === index) this.editing = null;
    else if (this.editing !== null && this.editing > index) this.editing -= 1;
    this.changed();
    return item;
  }

  /**
   * The edited version of item `index`: it leaves its slot; the caller sends it or puts it at the
   * front. Unchanged text keeps the item as it was.
   */
  takeEdited(
    index: number,
    text: string,
    display: string = text,
    tokens: readonly ComposerToken[] = [],
  ): QueueItem | undefined {
    const old = this.list[index];
    if (old === undefined) return undefined;
    this.removeAt(index);
    if (display === old.display) return old;
    return this.make(text, display, tokens);
  }

  setEditing(index: number | null): void {
    this.editing = index !== null && index >= 0 && index < this.list.length ? index : null;
    this.changed();
  }

  clear(): void {
    this.list = [];
    this.editing = null;
    this.changed();
  }
}

export const promptQueue = new PromptQueue();

/** Re-render on queue changes; returns the items. */
export function useQueueItems(queue: PromptQueue = promptQueue): readonly QueueItem[] {
  useSyncExternalStore(queue.subscribe, queue.getVersion, queue.getVersion);
  return queue.items;
}
