import { renderToString } from "ink";
import { beforeEach, describe, expect, it } from "vitest";
import { type Theme, ThemeProvider } from "../theme/theme.ts";
import {
  fitColumns,
  inlinePlain,
  inlineSpans,
  layoutMarkdown,
  MARKDOWN_CACHE_LIMIT,
  Markdown,
  markdownCacheStats,
  parseBlocks,
  resetMarkdownCache,
} from "./markdown.tsx";
import { cellWidth, lineText, lineWidth, type StyledLine } from "./styled.tsx";
import { testTheme } from "./testTheme.ts";

const theme: Theme = testTheme({
  accent: "#3a8fb7",
  "fg-muted": "#888888",
  success: "#33aa55",
  danger: "#cc3333",
});

const texts = (lines: StyledLine[]) => lines.map(lineText);
const frame = (text: string, width: number) =>
  renderToString(
    <ThemeProvider theme={theme}>
      <Markdown text={text} width={width} />
    </ThemeProvider>,
    { columns: width },
  )
    .split("\n")
    .map((l) => l.trimEnd());

const TABLE = [
  "| Tool | What it does | Cost |",
  "|------|:-------------|-----:|",
  "| read_file | reads a file and returns numbered lines | low |",
  "| terminal | runs a shell command | varies |",
].join("\n");

beforeEach(() => resetMarkdownCache());

describe("tables", () => {
  it("fits at 80 columns with ideal widths and a two-space gap", () => {
    const lines = frame(TABLE, 80);
    expect(lines).toEqual([
      "  Tool       What it does                             Cost",
      "  ─────────  ───────────────────────────────────────  ──────",
      "  read_file  reads a file and returns numbered lines  low",
      "  terminal   runs a shell command                     varies",
    ]);
  });

  it("shrinks and wraps cells at 40 columns without passing the width", () => {
    const lines = layoutMarkdown(TABLE, 40, theme);
    for (const line of lines) expect(lineWidth(line)).toBeLessThanOrEqual(40);
    const plain = texts(lines);
    expect(plain[0]).toMatch(/^ {2}Tool {7}What it does/);
    expect(plain[1]).toMatch(/^ {2}─{9} {2}─+ {2}─{6}$/);
    // the long cell wrapped onto several lines, words intact
    expect(plain.join(" ")).toContain("numbered");
    expect(plain.length).toBeGreaterThan(4);
    const gapColumn = plain[1]?.indexOf("  ", 2) ?? -1;
    for (const row of plain.slice(2)) expect(row.slice(gapColumn, gapColumn + 2)).toBe("  ");
  });

  it("keeps every column at least three cells", () => {
    expect(fitColumns([30, 30, 30], [12, 14, 10], 20).widths.every((w) => w >= 3)).toBe(true);
    expect(fitColumns([5, 6], [3, 3], 40)).toEqual({ widths: [5, 6], hard: false });
    const shrunk = fitColumns([20, 40], [5, 5], 30);
    expect(shrunk.widths.reduce((a, b) => a + b, 0)).toBe(30);
    expect(shrunk.hard).toBe(false);
  });

  it("falls back to header: value records when columns cannot fit", () => {
    const wide = "| a | b | c | d | e | f |\n|---|---|---|---|---|---|\n| 1 | 2 | 3 | 4 | 5 | 6 |";
    const plain = texts(layoutMarkdown(wide, 16, theme));
    expect(plain).toContain("  a: 1");
    expect(plain).toContain("  f: 6");
  });

  it("reads pipe rows without a delimiter row as a table", () => {
    expect(texts(layoutMarkdown("| k | v |\n| a | 1 |", 40, theme))).toEqual([
      "  k    v",
      "  ───  ───",
      "  a    1",
    ]);
  });

  it("pads ragged rows and renders a header-only table", () => {
    const ragged = "| x | y |\n|---|---|\n| only |";
    expect(texts(layoutMarkdown(ragged, 40, theme))[2]).toBe("  only");
    const header = texts(layoutMarkdown("| x | y |\n|---|---|", 40, theme));
    expect(header[0]).toBe("  x    y");
  });
});

describe("code fences", () => {
  it("renders a closed fence as an indented code block with its language", () => {
    const plain = texts(layoutMarkdown("```ts\nconst a = 1; // note\n```", 40, theme));
    expect(plain).toEqual(["  ts", "  const a = 1; // note"]);
  });

  it("colours keywords, strings and comments from theme tokens", () => {
    const [, code] = layoutMarkdown('```py\ndef f(): return "x"  # c\n```', 60, theme);
    const keyword = code?.find((s) => s.text.startsWith("def"));
    const text = code?.find((s) => s.text === '"x"');
    const comment = code?.find((s) => s.text.startsWith("#"));
    expect(keyword?.color).toBe(theme.color.syntaxKeyword);
    expect(text?.color).toBe(theme.color.syntaxString);
    expect(comment?.color).toBe(theme.color.syntaxComment);
  });

  it("colours diff lines", () => {
    const lines = layoutMarkdown("```diff\n@@ -1 +1 @@\n-old\n+new\n same\n```", 40, theme);
    expect(lines[1]?.at(-1)?.color).toBe(theme.color.diffRemoved);
    expect(lines[2]?.at(-1)?.color).toBe(theme.color.diffAdded);
    expect(texts(lines)).toEqual(["  @@ -1 +1 @@", "  -old", "  +new", "   same"]);
  });

  it("hard-wraps long code lines inside the indent", () => {
    const lines = texts(layoutMarkdown(`\`\`\`\n${"x".repeat(50)}\n\`\`\``, 20, theme));
    expect(lines.every((l) => l.startsWith("  ") && cellWidth(l) <= 20)).toBe(true);
    expect(lines.join("").replace(/ /g, "")).toBe("x".repeat(50));
  });

  it("runs an unclosed fence to the end in a final render", () => {
    expect(texts(layoutMarkdown("```\nopen\nstill code", 40, theme))).toEqual([
      "  open",
      "  still code",
    ]);
  });

  it("renders md fences as markdown", () => {
    expect(texts(layoutMarkdown("```md\n# Inner\n```", 40, theme))).toEqual(["Inner"]);
  });
});

describe("inline", () => {
  const ctx = { theme, hyperlinks: false };

  it("leaves __dunder__ names alone", () => {
    const spans = inlineSpans("call __init__ then x.__name__ and __main__.py", {}, ctx);
    expect(spans.map((s) => s.text).join("")).toBe("call __init__ then x.__name__ and __main__.py");
    expect(spans.some((s) => s.italic || s.bold)).toBe(false);
  });

  it("keeps snake_case words literal but italicises _word_", () => {
    const literal = inlineSpans("use snake_case_name here", {}, ctx);
    expect(literal.some((s) => s.italic)).toBe(false);
    const italic = inlineSpans("this is _gentle_ text", {}, ctx);
    expect(italic.find((s) => s.text === "gentle")?.italic).toBe(true);
  });

  it("handles bold, italic, strike and nesting", () => {
    const spans = inlineSpans("**bold _both_** and ~~gone~~ and __strong words__", {}, ctx);
    expect(spans.find((s) => s.text === "bold ")?.bold).toBe(true);
    const both = spans.find((s) => s.text === "both");
    expect(both?.bold && both.italic).toBe(true);
    expect(spans.find((s) => s.text === "gone")?.strike).toBe(true);
    expect(spans.find((s) => s.text === "strong words")?.bold).toBe(true);
  });

  it("keeps inline code verbatim", () => {
    const spans = inlineSpans("run `**not bold** _x_`", {}, ctx);
    const code = spans.find((s) => s.text === "**not bold** _x_");
    expect(code?.bold).toBeUndefined();
    expect(code?.color).toBe("#3a8fb7");
  });

  it("shows link targets in text unless hyperlinks are on", () => {
    expect(inlinePlain("[docs](https://example.org/a)")).toBe("docs");
    const off = inlineSpans("[docs](https://example.org/a)", {}, ctx);
    expect(off.map((s) => s.text).join("")).toBe("docs (https://example.org/a)");
    const on = inlineSpans("[docs](https://example.org/a)", {}, { theme, hyperlinks: true });
    expect(on).toHaveLength(1);
    expect(on[0]?.link).toBe("https://example.org/a");
    expect(on[0]?.underline).toBe(true);
  });

  it("keeps trailing punctuation out of bare URLs", () => {
    const spans = inlineSpans("see https://example.org/x_(y), then", {}, ctx);
    expect(spans.find((s) => s.link)?.text).toBe("https://example.org/x_(y)");
    expect(spans.map((s) => s.text).join("")).toBe("see https://example.org/x_(y), then");
  });

  it("renders footnote refs, images and autolinks", () => {
    expect(inlinePlain("fact[^1]")).toBe("fact[1]");
    expect(inlinePlain("![chart](https://e.org/c.png)")).toBe("image: chart");
    expect(inlinePlain("<https://e.org>")).toBe("https://e.org");
    const mail = inlineSpans("<team@example.org>", {}, ctx);
    expect(mail[0]).toMatchObject({ text: "team@example.org", link: "mailto:team@example.org" });
  });
});

describe("blocks", () => {
  it("parses headings, lists, quotes, rules, footnotes and media rows", () => {
    const source = [
      "# Top",
      "Second",
      "------",
      "- one",
      "  - nested",
      "- [x] done",
      "- [ ] open",
      "3. third",
      "> quoted",
      ">> deeper",
      "***",
      "[^1]: the note",
      "  continues",
      "MEDIA: /tmp/out.png",
    ].join("\n");
    expect(parseBlocks(source).map((b) => b.kind)).toEqual([
      "heading",
      "heading",
      "item",
      "item",
      "item",
      "item",
      "item",
      "quote",
      "rule",
      "footnote",
      "media",
    ]);
    expect(texts(layoutMarkdown(source, 40, theme))).toEqual([
      "Top",
      "Second",
      "• one",
      "  ◦ nested",
      "▣ done",
      "□ open",
      "3. third",
      "▎ quoted",
      "▎ ▎ deeper",
      "─".repeat(40),
      "[1] the note",
      "    continues",
      "⇲ attachment /tmp/out.png",
    ]);
  });

  it("links a MEDIA path as a file", () => {
    const [line] = layoutMarkdown("MEDIA: /tmp/a.wav", 60, theme);
    expect(line?.find((s) => s.link)?.link).toBe("file:///tmp/a.wav");
  });

  it("hangs wrapped list text under the first word", () => {
    const plain = texts(layoutMarkdown("- alpha beta gamma delta epsilon", 16, theme));
    expect(plain).toEqual(["• alpha beta", "  gamma delta", "  epsilon"]);
  });

  it("collapses runs of blank lines into one gap", () => {
    expect(texts(layoutMarkdown("a\n\n\n\nb\n\n", 20, theme))).toEqual(["a", "", "b"]);
  });

  it("prints text that already holds escape sequences as it is", () => {
    const ansi = "\u001b[31mred\u001b[0m plain\n**not markdown**";
    const lines = layoutMarkdown(ansi, 40, theme);
    expect(lines.every((l) => l.length === 1 && l[0]?.raw)).toBe(true);
    const out = frame(ansi, 40).join("\n");
    expect(out).toContain("\u001b[31mred");
    expect(out).toContain("**not markdown**");
  });

  it("renders nothing for empty text", () => {
    expect(layoutMarkdown("  \n", 40, theme)).toEqual([]);
  });
});

describe("render cache", () => {
  it("hits on a re-render with the same text, width and theme", () => {
    const first = layoutMarkdown("# cached", 40, theme);
    const again = layoutMarkdown("# cached", 40, theme);
    expect(again).toBe(first);
    expect(markdownCacheStats()).toMatchObject({ hits: 1, misses: 1, size: 1 });
    layoutMarkdown("# cached", 41, theme);
    layoutMarkdown("# cached", 40, { ...theme });
    expect(markdownCacheStats()).toMatchObject({ hits: 1, misses: 3 });
  });

  it("serves a re-rendered component from the cache", () => {
    frame("**x** and y", 30);
    frame("**x** and y", 30);
    expect(markdownCacheStats().hits).toBeGreaterThanOrEqual(1);
    expect(markdownCacheStats().misses).toBe(1);
  });

  it("holds at most 512 entries, dropping the oldest", () => {
    for (let i = 0; i < MARKDOWN_CACHE_LIMIT + 40; i++) layoutMarkdown(`row ${i}`, 30, theme);
    expect(markdownCacheStats().size).toBe(MARKDOWN_CACHE_LIMIT);
    layoutMarkdown("row 0", 30, theme);
    expect(markdownCacheStats().hits).toBe(0);
  });
});
