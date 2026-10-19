// Fixed-capacity FIFO that forgets its oldest entries.

export class Ring<T> {
  private readonly slots: Array<T | undefined>;
  private start = 0;
  private count = 0;
  /** Entries pushed out by newer ones since the ring was created or cleared. */
  dropped = 0;

  readonly capacity: number;

  constructor(capacity: number) {
    if (!Number.isInteger(capacity) || capacity < 1) {
      throw new RangeError(`ring capacity must be a positive integer, got ${capacity}`);
    }
    this.capacity = capacity;
    this.slots = new Array<T | undefined>(capacity);
  }

  get size(): number {
    return this.count;
  }

  push(item: T): void {
    const end = (this.start + this.count) % this.capacity;
    this.slots[end] = item;
    if (this.count < this.capacity) {
      this.count += 1;
    } else {
      this.start = (this.start + 1) % this.capacity;
      this.dropped += 1;
    }
  }

  /** Oldest first. */
  toArray(): T[] {
    const out: T[] = [];
    for (let i = 0; i < this.count; i += 1) {
      out.push(this.slots[(this.start + i) % this.capacity] as T);
    }
    return out;
  }

  /** The newest `n` entries, oldest first. */
  last(n: number): T[] {
    const all = this.toArray();
    return n >= all.length ? all : all.slice(all.length - Math.max(0, n));
  }

  /** Remove and return everything, oldest first. */
  take(): T[] {
    const out = this.toArray();
    this.clear();
    return out;
  }

  clear(): void {
    this.slots.fill(undefined);
    this.start = 0;
    this.count = 0;
    this.dropped = 0;
  }
}
