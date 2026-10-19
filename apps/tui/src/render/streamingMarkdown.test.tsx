import { renderToString } from "ink";
import { describe, expect, it } from "vitest";
import { type Theme, ThemeProvider } from "../theme/theme.ts";
import {
  advanceScan,
  findStableBoundary,
  LIVE_MAX_CHARS,
  LIVE_MAX_LINES,
  layoutStream,
  liveTail,
  newScanState,
  StreamingMarkdown,
} from "./streamingMarkdown.tsx";
import { lineText } from "./styled.tsx";
import { testTheme } from "./testTheme.ts";

const theme: Theme = testTheme({ accent: "cyan" });
const plain = (text: string, width = 60) => layoutStream(text, width, theme).lines.map(lineText);

describe("stable boundary", () => {
  it("is just past an empty line outside a fence", () => {
    expect(findStableBoundary("intro\n\nbody")).toBe(7);
    expect(findStableBoundary("one\n\ntwo\n\nthree")).toBe(10);
  });

  it("does not exist inside an open fence", () => {
    expect(findStableBoundary("```py\nx = 1\n\ny = 2\n")).toBe(-1);
    expect(findStableBoundary("a\n\n```\ncode\n\nmore")).toBe(3);
  });

  it("returns after a fence closes", () => {
    const text = "```\ncode\n\nstill\n```\n\nafter";
    expect(findStableBoundary(text)).toBe(text.indexOf("after"));
  });

  it("ignores a partial last line that may still open a fence", () => {
    expect(findStableBoundary("text\n\n``")).toBe(6);
    expect(findStableBoundary("no newline yet")).toBe(-1);
  });

  it("matches a fresh scan when fed delta by delta", () => {
    const text =
      "# T\n\npara one\n\n```js\nlet a\n\nlet b\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nend";
    let state = newScanState();
    for (let i = 1; i <= text.length; i += 3) state = advanceScan(state, text.slice(0, i));
    state = advanceScan(state, text);
    const fresh = advanceScan(newScanState(), text);
    expect(state.blocks).toEqual(fresh.blocks);
    expect(state.settled).toBe(fresh.settled);
  });

  it("starts over when the text no longer extends what was scanned", () => {
    const state = advanceScan(newScanState(), "```\nopen\n\n");
    expect(state.fence).not.toBeNull();
    const next = advanceScan(state, "fresh start\n\nnext");
    expect(next.fence).toBeNull();
    expect(next.blocks).toEqual(["fresh start\n\n"]);
  });
});

describe("live layout", () => {
  it("keeps an open fence's tail as plain text", () => {
    const lines = plain("Intro **bold**\n\n```ts\nconst a = 1\n\nconst b = 2");
    expect(lines[0]).toBe("Intro bold");
    expect(lines).toContain("```ts");
    expect(lines).toContain("const b = 2");
  });

  it("turns the fence into a code block once it closes and a boundary passes", () => {
    const lines = plain("Intro\n\n```ts\nconst a = 1\n```\n\nafter");
    expect(lines).toEqual(["Intro", "", "  ts", "  const a = 1", "", "after"]);
    expect(lines.some((l) => l.includes("```"))).toBe(false);
  });

  it("lays out a prose tail as markdown so emphasis does not flash raw", () => {
    expect(plain("Now **writing** the answer")).toEqual(["Now writing the answer"]);
    expect(plain("partial **bol")).toEqual(["partial **bol"]);
  });

  it("shows a half table plainly until it settles", () => {
    expect(plain("| a | b |\n|---|")).toEqual(["| a | b |", "|---|"]);
    expect(plain("| a | b |\n|---|---|\n| 1 | 2 |\n\n")).toEqual([
      "  a    b",
      "  ───  ───",
      "  1    2",
    ]);
  });

  it("renders a 20,000-char stream from its last 16,000 chars only", () => {
    const rows = Array.from(
      { length: 200 },
      (_, i) => `row-${String(i).padStart(3, "0")} ${"x".repeat(92)}`,
    );
    const text = rows.join("\n");
    expect(text.length).toBeGreaterThan(20_000 - 200);
    const tail = liveTail(text);
    expect(tail.text.length).toBeLessThanOrEqual(LIVE_MAX_CHARS);
    expect(tail.text.startsWith("row-")).toBe(true);
    const out = renderToString(
      <ThemeProvider theme={theme}>
        <StreamingMarkdown text={text} width={120} />
      </ThemeProvider>,
      { columns: 120 },
    );
    expect(out).not.toContain("row-000");
    expect(out).toContain("row-199");
    expect(out.split("\n")[0]).toMatch(
      /^… \d+ earlier lines \([\d,]+ chars\) not shown while streaming/,
    );
    expect(out.length).toBeLessThan(LIVE_MAX_CHARS + 600);
  });

  it("keeps at most 240 lines of a long stream", () => {
    const text = Array.from({ length: 300 }, (_, i) => `line ${i}`).join("\n");
    const tail = liveTail(text);
    expect(tail.text.split("\n")).toHaveLength(LIVE_MAX_LINES);
    expect(tail.omittedLines).toBe(60);
    expect(tail.text.startsWith("line 60\n")).toBe(true);
  });

  it("leaves short text untouched", () => {
    expect(liveTail("short")).toEqual({ text: "short", omittedChars: 0, omittedLines: 0 });
  });

  it("passes escape sequences through", () => {
    const lines = layoutStream("\u001b[32mok\u001b[0m\n\nnext", 40, theme).lines;
    expect(lines.every((l) => l[0]?.raw)).toBe(true);
  });
});
