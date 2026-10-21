// A small Markdown reader for model replies (P20-18). It turns text into blocks and inline runs
// that React renders as elements — never raw HTML. It is streaming-safe: an unclosed code fence
// or math block is reported as open (`closed: false`) instead of falling back to plain text, so
// the view does not flicker while a reply arrives.

export type Block =
  | { readonly type: "paragraph"; readonly text: string }
  | { readonly type: "heading"; readonly level: number; readonly text: string }
  | {
      readonly type: "code";
      readonly lang: string;
      readonly code: string;
      readonly closed: boolean;
    }
  | { readonly type: "math"; readonly tex: string; readonly closed: boolean }
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
  | { readonly type: "math"; readonly tex: string }
  | { readonly type: "strong"; readonly children: readonly Inline[] }
  | { readonly type: "em"; readonly children: readonly Inline[] }
  | { readonly type: "strike"; readonly children: readonly Inline[] }
  | { readonly type: "link"; readonly text: string; readonly href: string }
  | { readonly type: "break" };

const FENCE = /^(\s{0,3})(`{3,}|~{3,})\s*([\w+#.-]*)/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
const BULLET = /^\s{0,3}[-*+]\s+(.*)$/;
const ORDERED = /^\s{0,3}(\d{1,9})[.)]\s+(.*)$/;
const QUOTE = /^\s{0,3}>\s?(.*)$/;
const TABLE_RULE = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;

/** A line of only the opening fence's character, at least as many of them. */
function closesFence(line: string, marker: string): boolean {
  const trimmed = line.trim();
  const char = marker[0] ?? "`";
  return trimmed.length >= marker.length && [...trimmed].every((c) => c === char);
}

function cells(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split("|").map((cell) => cell.trim());
}

function startsBlock(line: string, next: string | undefined): boolean {
  return (
    FENCE.test(line) ||
    HEADING.test(line) ||
    RULE.test(line) ||
    BULLET.test(line) ||
    ORDERED.test(line) ||
    QUOTE.test(line) ||
    line.trim().startsWith("$$") ||
    (line.includes("|") && next !== undefined && TABLE_RULE.test(next))
  );
}

export function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i] ?? "";
    if (line.trim() === "") {
      i += 1;
      continue;
    }
    const fence = FENCE.exec(line);
    if (fence) {
      const marker = fence[2] ?? "```";
      const lang = (fence[3] ?? "").toLowerCase();
      const body: string[] = [];
      i += 1;
      let closed = false;
      while (i < lines.length) {
        const current = lines[i] ?? "";
        if (closesFence(current, marker)) {
          closed = true;
          i += 1;
          break;
        }
        body.push(current);
        i += 1;
      }
      blocks.push({ type: "code", lang, code: body.join("\n"), closed });
      continue;
    }
    if (line.trim().startsWith("$$")) {
      const first = line.trim().slice(2);
      if (first.endsWith("$$") && first.length >= 2) {
        blocks.push({ type: "math", tex: first.slice(0, -2).trim(), closed: true });
        i += 1;
        continue;
      }
      const body: string[] = first === "" ? [] : [first];
      i += 1;
      let closed = false;
      while (i < lines.length) {
        const current = lines[i] ?? "";
        if (current.trim().endsWith("$$")) {
          const rest = current.trim().slice(0, -2);
          if (rest !== "") body.push(rest);
          closed = true;
          i += 1;
          break;
        }
        body.push(current);
        i += 1;
      }
      blocks.push({ type: "math", tex: body.join("\n").trim(), closed });
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
      while (i < lines.length) {
        const current = lines[i] ?? "";
        const match = pattern.exec(current);
        if (match) {
          items.push((isOrdered ? match[2] : match[1]) ?? "");
          i += 1;
          continue;
        }
        // An indented continuation line belongs to the item above.
        if (current.trim() !== "" && /^\s{2,}/.test(current) && items.length > 0) {
          items[items.length - 1] = `${items[items.length - 1]}\n${current.trim()}`;
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

const SAFE_LINK = /^https?:\/\//i;

/** Inline runs of one block's text. */
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
      flush();
      out.push({ type: "break" });
      i += 1;
      continue;
    }
    if (ch === "`") {
      const ticks = /^`+/.exec(rest)?.[0] ?? "`";
      const end = source.indexOf(ticks, i + ticks.length);
      if (end > 0) {
        flush();
        out.push({ type: "code", text: source.slice(i + ticks.length, end).trim() });
        i = end + ticks.length;
        continue;
      }
    }
    if (ch === "$" && source[i + 1] !== "$" && source[i + 1] !== " ") {
      const end = source.indexOf("$", i + 1);
      if (end > i + 1 && source[end - 1] !== " " && !/\d/.test(source[end + 1] ?? "")) {
        flush();
        out.push({ type: "math", tex: source.slice(i + 1, end) });
        i = end + 1;
        continue;
      }
    }
    if (ch === "[") {
      const link = /^\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/.exec(rest);
      if (link) {
        flush();
        const href = link[2] ?? "";
        const label = link[1] || href;
        out.push(
          SAFE_LINK.test(href)
            ? { type: "link", text: label, href }
            : { type: "text", text: label },
        );
        i += link[0].length;
        continue;
      }
    }
    const strong = /^(\*\*|__)(?=\S)([\s\S]*?\S)\1/.exec(rest);
    if (strong) {
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
