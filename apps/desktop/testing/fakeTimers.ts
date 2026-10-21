// A deterministic clock + timer queue for main-process tests (no real sleeps).

export class FakeTimers {
  now = 0;
  private queue: Array<{ at: number; fn: () => void; id: number }> = [];
  private ids = 0;

  setTimeout = (fn: () => void, ms: number): unknown => {
    const id = ++this.ids;
    this.queue.push({ at: this.now + Math.max(0, ms), fn, id });
    return id;
  };

  clearTimeout = (handle: unknown): void => {
    this.queue = this.queue.filter((t) => t.id !== handle);
  };

  clock = (): number => this.now;

  pending(): number {
    return this.queue.length;
  }

  /** Move time forward, firing due timers in order (timers they schedule fire too, if due). */
  advance(ms: number): void {
    const until = this.now + ms;
    for (;;) {
      this.queue.sort((a, b) => a.at - b.at || a.id - b.id);
      const next = this.queue[0];
      if (next === undefined || next.at > until) break;
      this.queue.shift();
      this.now = next.at;
      next.fn();
    }
    this.now = until;
  }
}
