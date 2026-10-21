// Find in the rendered transcript (P20-39). Matches are DOM ranges over the transcript's text
// nodes, painted with the CSS Custom Highlight API (`::highlight(indus-find)` and
// `::highlight(indus-find-active)`), so React's streaming rows are never mutated and the find
// input's own text is never a match. Hidden screen-reader labels and anything marked
// `data-find-skip` are not searched. A match must sit inside one text node.

export const HIGHLIGHT_ALL = "indus-find";
export const HIGHLIGHT_ACTIVE = "indus-find-active";
const SKIP = ".sr-only, [data-find-skip], script, style, [hidden], [aria-hidden='true']";

export interface FindOptions {
  matchCase?: boolean;
}

/** Ranges of every occurrence of `query` in `root`'s visible text, in document order. */
export function findRanges(root: Node, query: string, options: FindOptions = {}): Range[] {
  if (query.trim() === "") return [];
  const doc = root.ownerDocument ?? (root as Document);
  const needle = options.matchCase === true ? query : query.toLowerCase();
  const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) => {
      const parent = node.parentElement;
      if (parent !== null && parent.closest(SKIP) !== null) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const ranges: Range[] = [];
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
    const text = node.nodeValue ?? "";
    const lowered = options.matchCase === true ? text : text.toLowerCase();
    // Case folding that changes the length would misplace offsets; search as written then.
    const haystack = lowered.length === text.length ? lowered : text;
    let at = haystack.indexOf(needle);
    while (at !== -1) {
      const range = doc.createRange();
      range.setStart(node, at);
      range.setEnd(node, at + needle.length);
      ranges.push(range);
      at = haystack.indexOf(needle, at + Math.max(needle.length, 1));
    }
  }
  return ranges;
}

export interface HighlightRegistry {
  set(name: string, ranges: readonly Range[]): void;
  delete(name: string): void;
}

/** The engine's highlight registry, or null where the API is missing (jsdom, old engines). */
export function cssHighlights(): HighlightRegistry | null {
  const host = globalThis as {
    CSS?: { highlights?: Map<string, unknown> };
    Highlight?: new (...ranges: Range[]) => unknown;
  };
  const registry = host.CSS?.highlights;
  const Highlight = host.Highlight;
  if (registry === undefined || Highlight === undefined) return null;
  return {
    set: (name, ranges) => {
      registry.set(name, new Highlight(...ranges));
    },
    delete: (name) => {
      registry.delete(name);
    },
  };
}

export interface FindState {
  query: string;
  total: number;
  /** 1-based index of the active match, 0 when there is none. */
  current: number;
}

export const EMPTY_FIND: FindState = { query: "", total: 0, current: 0 };

/** Search, step and paint over one root. */
export class TranscriptFinder {
  private readonly root: () => Node | null;
  private readonly highlights: HighlightRegistry | null;
  private ranges: Range[] = [];
  private state: FindState = EMPTY_FIND;
  private readonly options: FindOptions;

  constructor(options: {
    root: () => Node | null;
    highlights: HighlightRegistry | null;
    find?: FindOptions;
  }) {
    this.root = options.root;
    this.highlights = options.highlights;
    this.options = options.find ?? {};
  }

  get current(): FindState {
    return this.state;
  }

  /** A new query: the first match becomes active. */
  search(query: string): FindState {
    this.collect(query);
    this.state = { query, total: this.ranges.length, current: this.ranges.length > 0 ? 1 : 0 };
    this.paint(true);
    return this.state;
  }

  /** Re-run the same query after the transcript changed; keep the active index where it can be. */
  refresh(): FindState {
    if (this.state.query === "") return this.state;
    this.collect(this.state.query);
    const total = this.ranges.length;
    const current = total === 0 ? 0 : Math.min(Math.max(this.state.current, 1), total);
    this.state = { ...this.state, total, current };
    this.paint(false);
    return this.state;
  }

  next(): FindState {
    return this.step(1);
  }

  previous(): FindState {
    return this.step(-1);
  }

  clear(): FindState {
    this.ranges = [];
    this.state = EMPTY_FIND;
    this.highlights?.delete(HIGHLIGHT_ALL);
    this.highlights?.delete(HIGHLIGHT_ACTIVE);
    return this.state;
  }

  private step(direction: 1 | -1): FindState {
    const total = this.ranges.length;
    if (total === 0) return this.state;
    const current = ((this.state.current - 1 + direction + total) % total) + 1;
    this.state = { ...this.state, current };
    this.paint(true);
    return this.state;
  }

  private collect(query: string): void {
    const root = this.root();
    this.ranges = root === null ? [] : findRanges(root, query, this.options);
  }

  private paint(scroll: boolean): void {
    if (this.ranges.length === 0) {
      this.highlights?.delete(HIGHLIGHT_ALL);
      this.highlights?.delete(HIGHLIGHT_ACTIVE);
      return;
    }
    const active = this.ranges[this.state.current - 1];
    this.highlights?.set(HIGHLIGHT_ALL, this.ranges);
    this.highlights?.set(HIGHLIGHT_ACTIVE, active === undefined ? [] : [active]);
    if (scroll && active !== undefined) {
      const host = active.startContainer.parentElement;
      host?.scrollIntoView?.({ block: "center", inline: "nearest" });
    }
  }
}
