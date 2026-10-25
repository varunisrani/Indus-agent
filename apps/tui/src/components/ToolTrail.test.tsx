import { renderToString } from "ink";
import { beforeEach, describe, expect, it } from "vitest";
import { lineText } from "../render/styled.tsx";
import { testTheme } from "../render/testTheme.ts";
import { turnStore } from "../state/turnStore.ts";
import { uiStore } from "../state/uiStore.ts";
import { type Theme, ThemeProvider } from "../theme/theme.ts";
import { detailsStore, resolveSections } from "./details.ts";
import { layoutReasoning, thinkingPreview } from "./Reasoning.tsx";
import { layoutTodos, parseTodos, todoTree } from "./TodoPanel.tsx";
import {
  appendTrail,
  capVerbose,
  formatDuration,
  layoutToolTrail,
  readableOutput,
  ToolTrail,
  TRAIL_LIMIT,
  type TrailRow,
  type TrailView,
} from "./ToolTrail.tsx";
import { FALLBACK_VERB, toolVerb } from "./toolVerbs.ts";

const theme: Theme = testTheme({ accent: "cyan" });
const expanded = resolveSections("expanded", {}, true);

const done = (i: number, extra: Partial<TrailRow> = {}): TrailRow => ({
  id: `t${i}`,
  name: `tool_${i}`,
  status: "done",
  startedAt: 0,
  endedAt: 1200,
  ...extra,
});

const view = (over: Partial<TrailView> = {}): TrailView => ({
  active: [],
  pending: [],
  trail: [],
  activity: [],
  now: 10_000,
  frame: 0,
  ...over,
});

const text = (v: TrailView, sections = expanded, width = 80) =>
  layoutToolTrail(v, sections, width, theme).map(lineText);

describe("durations", () => {
  it.each([
    [0, "0.0s"],
    [-5, "0.0s"],
    [Number.NaN, "0.0s"],
    [420, "0.4s"],
    [4_250, "4.3s"],
    [9_940, "9.9s"],
    [9_960, "10s"],
    [12_400, "12s"],
    [59_400, "59s"],
    [59_600, "1m 0s"],
    [61_000, "1m 1s"],
    [3_725_000, "62m 5s"],
  ])("%d ms → %s", (ms, expected) => {
    expect(formatDuration(ms)).toBe(expected);
  });
});

describe("trail", () => {
  it("keeps the newest 8 entries", () => {
    let trail: TrailRow[] = [];
    for (let i = 0; i < 12; i++) trail = appendTrail(trail, done(i));
    expect(trail).toHaveLength(TRAIL_LIMIT);
    expect(trail[0]?.id).toBe("t4");
    const lines = text(view({ trail: Array.from({ length: 11 }, (_, i) => done(i)) }));
    expect(lines).toHaveLength(9);
    expect(lines[0]).toBe("▾ Tool calls (8)");
    expect(lines[1]).toBe("├ ✓ tool_3 (1.2s)");
    expect(lines[8]).toBe("└ ✓ tool_10 (1.2s)");
  });

  it("marks failures and cancellations", () => {
    const lines = text(
      view({ trail: [done(1, { status: "error" }), done(2, { status: "cancelled" })] }),
    );
    expect(lines).toEqual(["▾ Tool calls (2)", "├ ✗ tool_1 (1.2s)", "└ ⊘ tool_2 (1.2s)"]);
  });

  it("shows running tools with verb, argument preview and elapsed time", () => {
    const lines = text(
      view({
        active: [
          { id: "a", name: "terminal", args: "ls  -la\n/tmp", status: "running", startedAt: 7_500 },
        ],
      }),
    );
    expect(lines).toEqual(["▾ Tool calls (1)", "└ ◐ running terminal(ls -la /tmp) (2.5s)"]);
  });

  it("adds a drafting line for a tool the model is still writing", () => {
    expect(text(view({ pending: [{ id: "p", name: "write_file", status: "pending" }] }))).toEqual([
      "▾ Tool calls (1)",
      "└ ✎ drafting write_file…",
    ]);
  });

  it("draws a finished row waiting for its segment once, not as a drafting line", () => {
    const finished = done(1);
    expect(text(view({ trail: [finished], pending: [finished] }))).toEqual([
      "▾ Tool calls (1)",
      "└ ✓ tool_1 (1.2s)",
    ]);
  });

  it("indents subagent rows and hides them with their section", () => {
    const rows = [done(1), done(2, { depth: 1 })];
    expect(text(view({ trail: rows }))).toEqual([
      "▾ Tool calls (2)",
      "├ ✓ tool_1 (1.2s)",
      "└   ✓ tool_2 (1.2s)",
    ]);
    const noSubs = resolveSections("expanded", { subagents: "hidden" }, true);
    expect(text(view({ trail: rows }), noSubs)).toEqual(["▾ Tool calls (1)", "└ ✓ tool_1 (1.2s)"]);
  });

  it("caps verbose output to 800 chars / 12 lines", () => {
    const output = Array.from({ length: 40 }, (_, i) => `out ${i}`).join("\n");
    const capped = capVerbose(output);
    expect(capped.split("\n")).toHaveLength(13);
    expect(capped.startsWith("… 28 lines above not shown\n")).toBe(true);
    expect(capped.endsWith("out 39")).toBe(true);
    const wide = capVerbose("y".repeat(2000));
    expect(wide.length).toBeLessThanOrEqual(800 + 40);
    expect(capVerbose("small")).toBe("small");
    const lines = text(view({ trail: [done(1, { output, status: "error" })] }));
    expect(lines[2]).toBe("    ⎿ … 28 lines above not shown");
    expect(lines).toHaveLength(15);
  });

  it("shows a result only for a failed call, as its error words rather than raw JSON", () => {
    const ok = JSON.stringify({ output: "a.txt\nb.txt", exit_code: 0, error: null });
    expect(text(view({ trail: [done(1, { args: "ls", output: ok })] }))).toEqual([
      "▾ Tool calls (1)",
      "└ ✓ tool_1(ls) (1.2s)",
    ]);
    const bad = JSON.stringify({ output: "", exit_code: 2, error: "no such file" });
    expect(text(view({ trail: [done(2, { status: "error", output: bad })] }))).toEqual([
      "▾ Tool calls (1)",
      "└ ✗ tool_2 (1.2s)",
      "    ⎿ no such file",
    ]);
    expect(readableOutput(bad)).toBe("no such file");
    expect(readableOutput(ok)).toBe("a.txt\nb.txt");
    expect(readableOutput('{"exit_code": 0}')).toBe("");
    expect(readableOutput("{not json")).toBe("{not json");
    expect(readableOutput("plain words")).toBe("plain words");
  });

  it("collapses to a single summary line", () => {
    const collapsed = resolveSections("collapsed", { tools: "collapsed" });
    const lines = text(
      view({
        trail: [done(1)],
        active: [{ id: "a", name: "read_file", status: "running", startedAt: 9_000 }],
      }),
      collapsed,
    );
    expect(lines).toEqual(["▸ Tool calls (2) · reading read_file 1.0s"]);
  });
});

describe("detail sections", () => {
  const activity: TrailView["activity"] = [
    { id: "1", text: "fyi", level: "info" },
    { id: "2", text: "slow provider", level: "warn" },
    { id: "3", text: "tool crashed", level: "error" },
    { id: "4", text: "retrying", level: "warn" },
  ];

  it("still surfaces warnings and errors when every panel is hidden", () => {
    const hidden = resolveSections("hidden", {}, true);
    expect(text(view({ trail: [done(1)], activity }), hidden)).toEqual([
      "✗ tool crashed",
      "! retrying",
    ]);
    expect(
      text(
        view({ trail: [done(1)], activity: [activity[0] as TrailView["activity"][number]] }),
        hidden,
      ),
    ).toEqual([]);
  });

  it("lets a section override beat the global mode", () => {
    const sections = resolveSections("hidden", { tools: "expanded" }, true);
    expect(text(view({ trail: [done(1)], activity }), sections)).toEqual([
      "▾ Tool calls (1)",
      "└ ✓ tool_1 (1.2s)",
    ]);
  });

  it("lists the last activity entries when the lane is open", () => {
    const sections = resolveSections("expanded", { activity: "expanded" }, true);
    const five = [...activity, { id: "5", text: "fifth", level: "info" as const }];
    expect(text(view({ activity: five }), sections)).toEqual([
      "! slow provider",
      "✗ tool crashed",
      "! retrying",
      "· fifth",
    ]);
  });

  it("renders from the stores", () => {
    detailsStore.getState().set({ overrides: {}, globalFromCommand: false });
    uiStore.getState().patch({ detailsMode: "collapsed" });
    turnStore.getState().patch({ trail: [done(7)], activeTools: [], activity: [] });
    const out = renderToString(
      <ThemeProvider theme={theme}>
        <ToolTrail width={60} />
      </ThemeProvider>,
    );
    expect(out).toContain("▾ Tool calls (1)");
    expect(out).toContain("└ ✓ tool_7 (1.2s)");
    turnStore.getState().reset();
  });
});

describe("verbs", () => {
  it("maps tools to our verbs with fallbacks", () => {
    expect(toolVerb("read_file")).toBe("reading");
    expect(toolVerb("browser_click")).toBe("browsing");
    expect(toolVerb("mcp__github__search_files")).toBe("searching files");
    expect(toolVerb("mcp__x__unknown")).toBe("calling");
    expect(toolVerb("mystery")).toBe(FALLBACK_VERB);
  });
});

describe("reasoning", () => {
  it("previews the latest 160 chars while reasoning streams", () => {
    const long = `${"a ".repeat(200)}final words`;
    const preview = thinkingPreview(long);
    expect(preview).toHaveLength(160);
    expect(preview.startsWith("…")).toBe(true);
    expect(preview.endsWith("final words")).toBe(true);
    const collapsed = layoutReasoning("weighing options", "collapsed", true, 40, theme).map(
      lineText,
    );
    expect(collapsed).toEqual(["▸ thinking…", "  weighing options"]);
    expect(layoutReasoning("done", "collapsed", false, 40, theme).map(lineText)).toEqual([
      "▸ thought",
    ]);
  });

  it("shows the text when expanded and nothing when hidden", () => {
    expect(
      layoutReasoning("step one\nstep two", "expanded", false, 40, theme).map(lineText),
    ).toEqual(["▾ thought", "  step one", "  step two"]);
    expect(layoutReasoning("x", "hidden", true, 40, theme)).toEqual([]);
  });
});

describe("todo panel", () => {
  const raw = {
    todos: [
      { id: "1", content: "Read the code", status: "completed" },
      { id: "2", content: "Write the fix", status: "in_progress" },
      { id: "3", content: "Unit tests", status: "pending", parent: "2" },
      { id: "4", content: "Drop old path", status: "cancelled" },
      { id: "5", content: "", status: "pending" },
      { text: "Legacy shape", status: "weird" },
    ],
  };

  it("parses items and nests children under their parent", () => {
    const todos = parseTodos(raw);
    expect(todos.map((t) => [t.id, t.status])).toEqual([
      ["1", "completed"],
      ["2", "in_progress"],
      ["3", "pending"],
      ["4", "cancelled"],
      ["6", "pending"],
    ]);
    expect(todoTree(todos).map(([t, depth]) => `${t.id}:${depth}`)).toEqual([
      "1:0",
      "2:0",
      "3:1",
      "4:0",
      "6:0",
    ]);
    expect(parseTodos("nope")).toEqual([]);
  });

  it("shows a done count and folds", () => {
    const todos = parseTodos(raw);
    expect(layoutTodos(todos, false, 40, theme).map(lineText)).toEqual([
      "▾ plan 1/5 done",
      "  ✓ Read the code",
      "  ◉ Write the fix",
      "    ○ Unit tests",
      "  ✕ Drop old path",
      "  ○ Legacy shape",
    ]);
    expect(layoutTodos(todos, true, 40, theme).map(lineText)).toEqual(["▸ plan 1/5 done"]);
  });

  it("survives parent cycles", () => {
    const cyclic = parseTodos([
      { id: "a", content: "A", status: "pending", parent: "b" },
      { id: "b", content: "B", status: "pending", parent: "a" },
    ]);
    expect(todoTree(cyclic)).toHaveLength(2);
  });
});

beforeEach(() => {
  detailsStore.getState().set({ overrides: {}, globalFromCommand: false });
});
