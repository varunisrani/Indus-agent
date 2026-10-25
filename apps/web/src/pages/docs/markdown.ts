// A small Markdown reader for our user guides (W58-J2-1), adapted from the desktop's reply reader
// (apps/desktop/src/chat/markdown.ts, our own code): text becomes blocks and inline runs that React
// renders as elements, never raw HTML. Differences for docs: no math, links keep any href (the page
// decides what a link may do), table cells honour `\|`, HTML comment lines are skipped, and
// headings get the anchor ids our docs site gives them (`slugify`, Python-Markdown's toc rule).

export type Block =
  | { readonly type: "paragraph"; readonly text: string }
  | { readonly type: "heading"; readonly level: number; readonly text: string }
  | { readonly type: "code"; readonly lang: string; readonly code: string }
  | {
      readonly type: "list";
      readonly ordered: boolean;
      readonly start: number;
      readonly items: readonly string[];
    }
  | { readonly type: "quote"; readonly text: string }
  | { readonly type: "rule" }
  | {
      readonly type: "table";
      readonly header: readonly string[];
      readonly rows: readonly (readonly string[])[];
    };

export type Inline =
  | { readonly type: "text"; readonly text: string }
  | { readonly type: "code"; readonly text: string }
  | { readonly type: "strong"; readonly children: readonly Inline[] }
  | { readonly type: "em"; readonly children: readonly Inline[] }
  | { readonly type: "strike"; readonly children: readonly Inline[] }
  | { readonly type: "link"; readonly children: readonly Inline[]; readonly href: string }
  | { readonly type: "break" };

const FENCE = /^(\s{0,3})(`{3,}|~{3,})\s*([\w+#.-]*)/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
const BULLET = /^\s{0,3}[-*+]\s+(.*)$/;
const ORDERED = /^\s{0,3}(\d{1,9})[.)]\s+(.*)$/;
const QUOTE = /^\s{0,3}>\s?(.*)$/;
const TABLE_RULE = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;
const COMMENT = /^\s*<!--.*-->\s*$/;

function closesFence(line: string, marker: string): boolean {
  const trimmed = line.trim();
  const char = marker[0] ?? "`";
  return trimmed.length >= marker.length && [...trimmed].every((c) => c === char);
}

/** A table row's cells: split on `|` that is not escaped (`\|` stays in the cell as written). */
export function cells(line: string): string[] {
  const trimmed = line
    .trim()
    .replace(/^\|/, "")
    .replace(/(?<!\\)\|$/, "");
  const out: string[] = [];
  let cell = "";
  for (let i = 0; i < trimmed.length; i += 1) {
    const ch = trimmed[i] ?? "";
    if (ch === "\\" && trimmed[i + 1] === "|") {
      cell += "\\|";
      i += 1;
    } else if (ch === "|") {
      out.push(cell.trim());
      cell = "";
    } else {
      cell += ch;
    }
  }
  out.push(cell.trim());
  return out;
}

function startsBlock(line: string, next: string | undefined): boolean {
  return (
    FENCE.test(line) ||
    HEADING.test(line) ||
    RULE.test(line) ||
    BULLET.test(line) ||
    ORDERED.test(line) ||
    QUOTE.test(line) ||
    (line.includes("|") && next !== undefined && TABLE_RULE.test(next))
  );
}

/** A list item's text split into its leading inline text (the first line and the lines that
 * continue it) and the blocks after it (a nested list, code, a later paragraph). */
export function splitItem(text: string): { lead: string; rest: string } {
  const lines = text.split("\n");
  let end = 1;
  while (end < lines.length) {
    const line = lines[end] ?? "";
    if (line.trim() === "" || startsBlock(line, lines[end + 1])) break;
    end += 1;
  }
  return { lead: lines.slice(0, end).join("\n"), rest: lines.slice(end).join("\n") };
}

export function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i] ?? "";
    if (line.trim() === "" || COMMENT.test(line)) {
      i += 1;
      continue;
    }
    const fence = FENCE.exec(line);
    if (fence) {
      const indent = fence[1]?.length ?? 0;
      const marker = fence[2] ?? "```";
      const lang = (fence[3] ?? "").toLowerCase();
      const body: string[] = [];
      i += 1;
      while (i < lines.length) {
        const current = lines[i] ?? "";
        i += 1;
        if (closesFence(current, marker)) break;
        body.push(current.slice(Math.min(indent, current.length - current.trimStart().length)));
      }
      blocks.push({ type: "code", lang, code: body.join("\n") });
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      blocks.push({ type: "heading", level: heading[1]?.length ?? 1, text: heading[2] ?? "" });
      i += 1;
      continue;
    }
    if (RULE.test(line)) {
      blocks.push({ type: "rule" });
      i += 1;
      continue;
    }
    if (line.includes("|") && TABLE_RULE.test(lines[i + 1] ?? "")) {
      const header = cells(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && (lines[i] ?? "").includes("|") && (lines[i] ?? "").trim() !== "") {
        rows.push(cells(lines[i] ?? ""));
        i += 1;
      }
      blocks.push({ type: "table", header, rows });
      continue;
    }
    if (QUOTE.test(line)) {
      const body: string[] = [];
      while (i < lines.length) {
        const match = QUOTE.exec(lines[i] ?? "");
        if (!match) break;
        body.push(match[1] ?? "");
        i += 1;
      }
      blocks.push({ type: "quote", text: body.join("\n") });
      continue;
    }
    const bullet = BULLET.exec(line);
    const ordered = ORDERED.exec(line);
    if (bullet || ordered) {
      const isOrdered = ordered !== null;
      const pattern = isOrdered ? ORDERED : BULLET;
      const items: string[] = [];
      const start = isOrdered ? Number(ordered?.[1] ?? 1) : 1;
      const base = line.length - line.trimStart().length;
      while (i < lines.length) {
        const current = lines[i] ?? "";
        const match = pattern.exec(current);
        // A marker indented past this list's own is a nested list: it belongs to the item.
        const deeper = current.length - current.trimStart().length >= base + 2;
        if (match && !deeper) {
          items.push((isOrdered ? match[2] : match[1]) ?? "");
          i += 1;
          continue;
        }
        // An indented line (a continuation, a nested list or code) belongs to the item above; a
        // blank line inside the item is kept when indented text follows it.
        const following = lines[i + 1] ?? "";
        const continues =
          (current.trim() !== "" && (deeper || /^\s{2,}/.test(current))) ||
          (current.trim() === "" && /^\s{2,}\S/.test(following));
        if (continues && items.length > 0) {
          items[items.length - 1] = `${items[items.length - 1]}\n${current.replace(/^ {2,4}/, "")}`;
          i += 1;
          continue;
        }
        break;
      }
      blocks.push({ type: "list", ordered: isOrdered, start, items });
      continue;
    }
    const body: string[] = [line];
    i += 1;
    while (i < lines.length) {
      const current = lines[i] ?? "";
      if (current.trim() === "" || startsBlock(current, lines[i + 1])) break;
      body.push(current);
      i += 1;
    }
    blocks.push({ type: "paragraph", text: body.join("\n") });
  }
  return blocks;
}

/** Inline runs of one block's text. A soft line break inside a paragraph is a space. */
export function parseInline(source: string): Inline[] {
  const out: Inline[] = [];
  let text = "";
  const flush = (): void => {
    if (text !== "") out.push({ type: "text", text });
    text = "";
  };
  let i = 0;
  while (i < source.length) {
    const rest = source.slice(i);
    const ch = source[i] ?? "";
    if (
      ch === "\\" &&
      i + 1 < source.length &&
      /[\\`*_{}[\]()#+\-.!$~|>]/.test(source[i + 1] ?? "")
    ) {
      text += source[i + 1];
      i += 2;
      continue;
    }
    if (ch === "\n") {
      text += " ";
      i += 1;
      continue;
    }
    if (ch === "`") {
      const ticks = /^`+/.exec(rest)?.[0] ?? "`";
      const end = source.indexOf(ticks, i + ticks.length);
      if (end > 0) {
        flush();
        const code = source.slice(i + ticks.length, end).replace(/\\\|/g, "|");
        out.push({ type: "code", text: code.trim() });
        i = end + ticks.length;
        continue;
      }
    }
    if (ch === "[") {
      const link = /^\[((?:[^\]`]|`[^`]*`)*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/.exec(rest);
      if (link) {
        flush();
        const href = link[2] ?? "";
        out.push({ type: "link", children: parseInline(link[1] || href), href });
        i += link[0].length;
        continue;
      }
    }
    const strong = /^(\*\*|__)(?=\S)([\s\S]*?\S)\1/.exec(rest);
    if (strong && (ch === "*" || i === 0 || !/\w/.test(source[i - 1] ?? ""))) {
      flush();
      out.push({ type: "strong", children: parseInline(strong[2] ?? "") });
      i += strong[0].length;
      continue;
    }
    const strike = /^~~(?=\S)([\s\S]*?\S)~~/.exec(rest);
    if (strike) {
      flush();
      out.push({ type: "strike", children: parseInline(strike[1] ?? "") });
      i += strike[0].length;
      continue;
    }
    const em = /^(\*|_)(?=\S)([\s\S]*?\S)\1(?![*_\w])/.exec(rest);
    if (em && (ch === "*" || i === 0 || !/\w/.test(source[i - 1] ?? ""))) {
      flush();
      out.push({ type: "em", children: parseInline(em[2] ?? "") });
      i += em[0].length;
      continue;
    }
    text += ch;
    i += 1;
  }
  flush();
  return out;
}

/** The plain text of inline runs (what a heading's anchor is made from). */
export function plainText(runs: readonly Inline[]): string {
  return runs
    .map((run) => {
      switch (run.type) {
        case "text":
        case "code":
          return run.text;
        case "break":
          return " ";
        default:
          return plainText(run.children);
      }
    })
    .join("");
}

/** A heading's anchor as our docs site makes it (Python-Markdown's toc): ASCII only, word
 * characters, spaces and dashes kept, lower case, runs of spaces and dashes become one dash. */
export function slugify(text: string): string {
  return text
    .normalize("NFKD")
    .replace(/[^\x20-\x7e]/g, "")
    .replace(/[^\w\s-]/g, "")
    .trim()
    .toLowerCase()
    .replace(/[-\s]+/g, "-");
}
