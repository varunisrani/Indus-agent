// Events that arrive before anyone listens are kept and handed over in order (R05.2).

import { EVENT_BUFFER_CAPACITY } from "./limits.ts";
import { Ring } from "./ring.ts";

export type EventListener<E> = (event: E) => void;

export interface EventBufferOptions {
  capacity?: number;
  /** A listener threw; the others still got the event. */
  onListenerError?: (error: unknown) => void;
}

export class EventBuffer<E> {
  private readonly early: Ring<E>;
  private readonly listeners = new Set<EventListener<E>>();
  private readonly onListenerError: ((error: unknown) => void) | undefined;
  private everSubscribed = false;

  constructor(options: EventBufferOptions = {}) {
    this.early = new Ring<E>(options.capacity ?? EVENT_BUFFER_CAPACITY);
    this.onListenerError = options.onListenerError;
  }

  /** Set once by the first subscribe and never cleared, not even by a reconnect. */
  get subscribed(): boolean {
    return this.everSubscribed;
  }

  /** Events waiting for the first subscriber. */
  get waiting(): number {
    return this.early.size;
  }

  /** Events pushed out of the full buffer before anyone subscribed. */
  get dropped(): number {
    return this.early.dropped;
  }

  push(event: E): void {
    if (!this.everSubscribed) {
      this.early.push(event);
      return;
    }
    for (const listener of [...this.listeners]) this.deliver(listener, event);
  }

  /**
   * Add a listener. The first one ever also receives everything held so far, oldest first,
   * before this call returns. JS runs one thing at a time, so no live event can overtake them.
   */
  subscribe(listener: EventListener<E>): () => void {
    this.listeners.add(listener);
    if (!this.everSubscribed) {
      this.everSubscribed = true;
      for (const event of this.early.take()) {
        for (const current of [...this.listeners]) this.deliver(current, event);
      }
    }
    return () => {
      this.listeners.delete(listener);
    };
  }

  private deliver(listener: EventListener<E>, event: E): void {
    try {
      listener(event);
    } catch (error) {
      this.onListenerError?.(error);
    }
  }
}
