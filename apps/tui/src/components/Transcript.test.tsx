import { renderToString } from "ink";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { lineText } from "../render/styled.tsx";
import { testTheme } from "../render/testTheme.ts";
import { turnStore } from "../state/turnStore.ts";
import { uiStore } from "../state/uiStore.ts";
import { type Theme, ThemeProvider } from "../theme/theme.ts";
import { AppLayout, queuedPreview } from "./AppLayout.tsx";
import { resolveSections } from "./details.ts";
import {
  formatStamp,
  isDiffText,
  LONG_USER_CHARS,
  layoutMessage,
  SYSTEM_COLLAPSE_CHARS,
  type TranscriptItem,
} from "./MessageRow.tsx";
import { Transcript, transcriptStats } from "./Transcript.tsx";
import { scrollBy, scrollToBottom, scrollToTop } from "./virtualRows.ts";

const theme: Theme = testTheme({ accent: "cyan" });
const sections = resolveSections("collapsed");
const ctx = { theme, sections };

const many = (n: number): TranscriptItem[] =>
  Array.from({ length: n }, (_, i) => ({
    id: `m${i}`,
    role: i % 2 === 0 ? "user" : "assistant",
    text: `message ${i} with some words that wrap when the pane is narrow enough`,
  }));

const draw = (items: readonly TranscriptItem[], width: number, height: number) =>
  renderToString(
    <ThemeProvider theme={theme}>
      <Transcript items={items} width={width} height={height} />
    </ThemeProvider>,
    { columns: width },
  ).split("\n");

const rowOf = (key: string) => key.slice(0, key.lastIndexOf(":"));

beforeEach(() => {
  scrollToBottom();
  turnStore.getState().reset();
  uiStore.getState().patch({ busy: false, timestamps: false, detailsMode: "collapsed" });
});
afterEach(() => scrollToBottom());

describe("virtual transcript", () => {
  it("renders only the visible rows of a 5,000-row transcript", () => {
    const items = many(5000);
    const out = draw(items, 80, 20);
    expect(out).toHaveLength(20);
    expect(transcriptStats.rows).toBe(5000);
    expect(transcriptStats.rendered).toBe(20);
    expect(transcriptStats.totalLines).toBeGreaterThan(10_000);
    const text = out.join("\n");
    expect(text).toContain("message 4999");
    expect(text).not.toContain("message 4980 ");
    expect(text).not.toContain("message 0 ");
  });

  it("stops following when scrolled up; new output does not move the view", () => {
    const items = many(200);
    draw(items, 80, 10);
    scrollBy(-25);
    draw(items, 80, 10);
    const top = transcriptStats.topKey;
    const offset = transcriptStats.offset;
    draw([...items, ...many(210).slice(200)], 80, 10);
    expect(transcriptStats.topKey).toBe(top);
    expect(transcriptStats.offset).toBe(offset);
  });

  it("keeps the anchor row on top after a width change", () => {
    const items = many(300);
    draw(items, 100, 12);
    scrollToTop();
    scrollBy(301);
    draw(items, 100, 12);
    const anchorRow = rowOf(transcriptStats.topKey);
    draw(items, 40, 12);
    expect(rowOf(transcriptStats.topKey)).toBe(anchorRow);
    draw(items, 100, 12);
    expect(rowOf(transcriptStats.topKey)).toBe(anchorRow);
  });

  it("follows new output while at the bottom", () => {
    draw(many(50), 80, 8);
    draw(many(60), 80, 8);
    expect(draw(many(60), 80, 8).join("\n")).toContain("message 59");
  });

  it("draws the banner first with the session facts", () => {
    uiStore.getState().patch({ sid: "s-123" });
    const banner: TranscriptItem = {
      id: "banner",
      role: "banner",
      text: "",
      info: { model: "m-large", provider: "acme", cwd: "/work", tools: ["a", "b"] },
    };
    const out = draw([banner, ...many(2)], 80, 30).join("\n");
    expect(out).toContain("indus agent");
    expect(out).toContain("m-large · acme");
    expect(out).toContain("2 available");
    expect(out).toContain("s-123");
    uiStore.getState().patch({ sid: null });
  });

  it("shows the live turn: settled segment, running tool and streaming text", () => {
    uiStore.getState().patch({ busy: true });
    turnStore.getState().patch({
      segments: [{ id: "s1", text: "Looked around first.", tools: [] }],
      activeTools: [
        {
          id: "t1",
          name: "read_file",
          args: "src/app.ts",
          status: "running",
          startedAt: Date.now() - 2500,
        },
      ],
      streamBuffer: "Now **writing** the answer",
    });
    const out = draw(many(2), 80, 20).join("\n");
    expect(out).toContain("Looked around first.");
    expect(out).toContain("▾ Tool calls (1)");
    expect(out).toContain("reading read_file(src/app.ts)");
    expect(out).toMatch(/2\.\ds/);
    expect(out).toContain("Now writing the answer");
  });
});

describe("message rows", () => {
  it("collapses a system line over 400 chars", () => {
    const long = `System prompt start\n${"x".repeat(SYSTEM_COLLAPSE_CHARS - 19)}`;
    expect(long.length).toBe(401);
    const lines = layoutMessage({ id: "s", role: "system", text: long }, 80, ctx).map(lineText);
    expect(lines).toEqual(["· ▸ System prompt start (401 chars)"]);
    const open = layoutMessage({ id: "s", role: "system", text: long, expanded: true }, 80, ctx);
    expect(open.length).toBeGreaterThan(1);
    const short = layoutMessage({ id: "s", role: "system", text: "y".repeat(400) }, 80, ctx);
    expect(short.length).toBeGreaterThan(1);
  });

  it("collapses a paste-backed user message over 300 chars", () => {
    const text = `please review this [paste #1 +40 lines]\n${"z".repeat(LONG_USER_CHARS)}`;
    const lines = layoutMessage({ id: "u", role: "user", text }, 80, ctx).map(lineText);
    expect(lines).toHaveLength(1);
    expect(lines[0]).toMatch(
      /^› please review this \[paste #1 \+40 lines\] ⋯ [\d,]+ more chars pasted$/,
    );
    const plain = layoutMessage({ id: "u", role: "user", text: "w ".repeat(200) }, 80, ctx);
    expect(plain.length).toBeGreaterThan(1);
  });

  it("stamps user and assistant rows when timestamps are on", () => {
    const at = new Date(2026, 0, 2, 9, 5).getTime();
    expect(formatStamp(at)).toBe("[09:05]");
    expect(formatStamp(undefined)).toBeNull();
    const lines = layoutMessage({ id: "a", role: "assistant", text: "hi", createdAt: at }, 40, {
      ...ctx,
      timestamps: true,
    }).map(lineText);
    expect(lines).toEqual(["  [09:05]", "◆ hi"]);
  });

  it("draws a turn rule above later user rows", () => {
    const out = draw(many(4), 60, 20).join("\n");
    expect(out.match(/^───$/gm)).toHaveLength(1);
  });

  it("colours diff rows and recognises diff text", () => {
    const diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new";
    expect(isDiffText(diff)).toBe(true);
    expect(isDiffText("just - a list")).toBe(false);
    const lines = layoutMessage({ id: "d", role: "diff", text: diff }, 40, ctx);
    expect(lines.map(lineText)).toEqual([
      "  --- a/x",
      "  +++ b/x",
      "  @@ -1 +1 @@",
      "  -old",
      "  +new",
    ]);
    expect(lines[4]?.at(-1)?.color).toBe(theme.color.diffAdded);
  });

  it("folds a finished reply's reasoning into one row, or hides it", () => {
    const item: TranscriptItem = {
      id: "r",
      role: "assistant",
      text: "Forty-two.",
      reasoning: "a long chain of thought",
    };
    const shown = layoutMessage(item, 40, { theme, sections: resolveSections("expanded") }).map(
      lineText,
    );
    expect(shown[0]).toContain("▸ thought");
    expect(shown.join("\n")).not.toContain("chain of thought");
    expect(shown.join("\n")).toContain("Forty-two.");
    const hidden = layoutMessage(item, 40, {
      theme,
      sections: resolveSections("collapsed", { thinking: "hidden" }),
    }).map(lineText);
    expect(hidden.join("\n")).not.toContain("thought");
  });

  it("shows tool rows as a one-line preview", () => {
    const lines = layoutMessage({ id: "t", role: "tool", text: "line one\nline two" }, 40, ctx).map(
      lineText,
    );
    expect(lines).toEqual(["  ⎿ line one line two"]);
  });
});

describe("layout", () => {
  it("stacks transcript, prompt zone and composer with the status rule on top", () => {
    uiStore.getState().patch({ statusBar: "top", info: { model: "m1" }, statusText: "ready" });
    const out = renderToString(
      <ThemeProvider theme={theme}>
        <AppLayout cols={70} rows={14} items={many(30)} queued={["first queued", "second"]} />
      </ThemeProvider>,
      { columns: 70 },
    ).split("\n");
    expect(out).toHaveLength(14);
    expect(out[10]).toMatch(/^queued 1\. first queued/);
    expect(out[12]).toMatch(/^─ ready │ m1 ─+$/);
    expect(out[13]).toMatch(/^›/);
  });

  it("puts the status rule at the bottom or nowhere", () => {
    uiStore.getState().patch({ statusBar: "bottom", info: { model: "m1" } });
    const bottom = renderToString(
      <ThemeProvider theme={theme}>
        <AppLayout cols={60} rows={8} items={many(10)} />
      </ThemeProvider>,
      { columns: 60 },
    ).split("\n");
    expect(bottom.at(-1)).toMatch(/^─ ready │ m1/);
    uiStore.getState().patch({ statusBar: "off" });
    const off = renderToString(
      <ThemeProvider theme={theme}>
        <AppLayout cols={60} rows={8} items={many(10)} />
      </ThemeProvider>,
      { columns: 60 },
    );
    expect(off).not.toContain("─ ready");
    uiStore.getState().patch({ statusBar: "top" });
  });

  it("previews at most three queued messages", () => {
    expect(queuedPreview(["a", "b", "c", "d", "e"], 60)).toEqual([
      "queued 1. a",
      "       2. b",
      "       3. c",
      "       +2 more",
    ]);
    expect(queuedPreview([], 60)).toEqual([]);
  });
});
