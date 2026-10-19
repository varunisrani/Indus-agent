import { describe, expect, it } from "vitest";
import { cellWidth, lineText } from "../render/styled.tsx";
import { testTheme } from "../render/testTheme.ts";
import type { Theme } from "../theme/theme.ts";
import { statusLine } from "./StatusRule.tsx";
import {
  BREAKPOINTS,
  busyIndicator,
  compactNumber,
  contextBar,
  cwdLabel,
  formatClock,
  INDICATOR_STYLES,
  readStatusFields,
  type StatusInput,
  statusFromStores,
  statusSegments,
} from "./statusSegments.ts";

const full: Omit<StatusInput, "cols"> = {
  busy: false,
  statusText: "ready",
  model: "m1",
  contextUsed: 50_000,
  contextMax: 200_000,
  durationMs: 5_000,
  compressions: 2,
  bgTasks: 1,
  subagents: 2,
  cacheHitPct: 80,
  latencyS: 1.2,
  tps: 42,
};

const ids = (input: StatusInput) => statusSegments(input).left.map((s) => s.id);

describe("breakpoints", () => {
  it.each([
    [60, ["status", "model", "context"]],
    [72, ["status", "model", "context", "bar"]],
    [80, ["status", "model", "context", "bar", "duration", "compressions"]],
    [
      96,
      [
        "status",
        "model",
        "context",
        "bar",
        "duration",
        "compressions",
        "bg_tasks",
        "subagents",
        "cache_hit",
      ],
    ],
    [
      104,
      [
        "status",
        "model",
        "context",
        "bar",
        "duration",
        "compressions",
        "bg_tasks",
        "subagents",
        "cache_hit",
        "latency",
      ],
    ],
    [
      110,
      [
        "status",
        "model",
        "context",
        "bar",
        "duration",
        "compressions",
        "bg_tasks",
        "subagents",
        "cache_hit",
        "latency",
        "tps",
      ],
    ],
    [
      140,
      [
        "status",
        "model",
        "context",
        "bar",
        "duration",
        "compressions",
        "bg_tasks",
        "subagents",
        "cache_hit",
        "latency",
        "tps",
      ],
    ],
  ])("at %d cols shows exactly %j", (cols, expected) => {
    expect(ids({ ...full, cols })).toEqual(expected);
  });

  it("uses the documented breakpoints", () => {
    expect(Object.values(BREAKPOINTS)).toEqual([72, 76, 80, 84, 88, 92, 96, 104, 110]);
    expect(ids({ ...full, cols: 75 })).not.toContain("duration");
    expect(ids({ ...full, cols: 76 })).toContain("duration");
    expect(ids({ ...full, cols: 91 })).not.toContain("subagents");
    expect(ids({ ...full, cols: 92 })).toContain("subagents");
  });

  it("shows voice only once a voice label exists", () => {
    expect(ids({ ...full, cols: 140 })).not.toContain("voice");
    expect(ids({ ...full, cols: 140, voice: "● rec" })).toContain("voice");
    expect(ids({ ...full, cols: 83, voice: "● rec" })).not.toContain("voice");
  });

  it("collapses the context read-out below 72 cols", () => {
    const narrow = statusSegments({ ...full, cols: 60 }).left.find((s) => s.id === "context");
    const wide = statusSegments({ ...full, cols: 72 }).left.find((s) => s.id === "context");
    expect(narrow?.text).toBe("25%");
    expect(wide?.text).toBe("50k/200k 25%");
  });

  it("keeps model and context even when nothing else fits", () => {
    expect(ids({ ...full, cols: 20, cwdLabel: "~/long/project/path", branch: "main" })).toEqual([
      "status",
      "model",
      "context",
    ]);
  });
});

describe("hidden fields and right edge", () => {
  it("shows only the segments listed in display.status_bar.fields", () => {
    const fields = readStatusFields(["context_detail", "TPS", "latency", " title "]);
    const shown = ids({ ...full, cols: 140, fields });
    expect(shown).toEqual(["status", "model", "context", "latency", "tps"]);
    const context = statusSegments({ ...full, cols: 140, fields }).left.find(
      (s) => s.id === "context",
    );
    expect(context?.text).toBe("50k/200k");
    expect(readStatusFields([])).toBeNull();
    expect(readStatusFields("tps")).toBeNull();
    expect(ids({ ...full, cols: 140, fields: null })).toContain("cache_hit");
  });

  it("shows the title badge, else folder and branch", () => {
    expect(
      statusSegments({ ...full, cols: 140, title: "Fix login", cwdLabel: "~/p", branch: "main" })
        .right,
    ).toEqual({
      text: " Fix login ",
      tone: "accent",
      bold: true,
    });
    expect(
      statusSegments({ ...full, cols: 140, cwdLabel: "~/p", branch: "main" }).right?.text,
    ).toBe("~/p ⎇ main");
    const noTitle = statusSegments({
      ...full,
      cols: 140,
      title: "T",
      cwdLabel: "~/p",
      fields: new Set(["tps"]),
    });
    expect(noTitle.right?.text).toBe("~/p");
  });

  it("drops tail segments whole before cutting anything", () => {
    const layout = statusSegments({
      ...full,
      cols: 96,
      cwdLabel: "~/projects/indus",
      branch: "feature/x",
    });
    expect(layout.left.map((s) => s.id)).not.toContain("cache_hit");
    for (const seg of layout.left) expect(seg.text.endsWith("…")).toBe(false);
  });

  it("pins a YOLO warning badge", () => {
    const layout = statusSegments({ ...full, cols: 40, yolo: true });
    expect(layout.left.find((s) => s.id === "yolo")).toEqual({
      id: "yolo",
      text: "⚠ yolo",
      tone: "warn",
    });
  });

  it("paints exactly the terminal width", () => {
    const theme: Theme = testTheme();
    for (const cols of [60, 80, 140]) {
      const line = statusLine(
        statusSegments({ ...full, cols, cwdLabel: "~/p", branch: "main" }),
        cols,
        theme,
      );
      expect(cellWidth(lineText(line))).toBe(cols);
    }
  });
});

describe("busy indicator", () => {
  it("uses equal-width frames in every style", () => {
    for (const [name, style] of Object.entries(INDICATOR_STYLES)) {
      const widths = new Set(style.frames.map(cellWidth));
      expect(widths.size, name).toBe(1);
      const shown = new Set(
        Array.from({ length: 200 }, (_, frame) => cellWidth(busyIndicator(name, frame))),
      );
      expect(shown.size, name).toBe(1);
    }
  });

  it("falls back to the default style and replaces the status while busy", () => {
    expect(busyIndicator("nope", 0)).toBe(busyIndicator("dots", 0));
    expect(ids({ ...full, cols: 60, busy: true })[0]).toBe("indicator");
  });
});

describe("formatting and store input", () => {
  it("formats numbers, clocks, bars and folders", () => {
    expect(compactNumber(950)).toBe("950");
    expect(compactNumber(1_250)).toBe("1.3k");
    expect(compactNumber(200_000)).toBe("200k");
    expect(compactNumber(1_500_000)).toBe("1.5M");
    expect(formatClock(42_000)).toBe("42s");
    expect(formatClock(185_000)).toBe("3m 5s");
    expect(formatClock(3_720_000)).toBe("1h 2m");
    expect(contextBar(25)).toBe("▰▰▰▱▱▱▱▱▱▱");
    expect(cwdLabel("/home/me/code/app", "/home/me")).toBe("~/code/app");
    expect(cwdLabel("/srv/a/b/c/d")).toBe("…/c/d");
  });

  it("reads session info and usage from the ui store shape", () => {
    const input = statusFromStores(
      {
        busy: false,
        statusText: "ready",
        indicatorStyle: "bar",
        info: { model: "m2", reasoning_effort: "high", cwd: "/home/me/w", title: "T" },
        usage: {
          context_tokens: 1000,
          context_window: 10_000,
          input_tokens: 300,
          cache_read_tokens: 700,
        },
      },
      { cols: 100, home: "/home/me", branch: "dev" },
    );
    expect(input).toMatchObject({
      model: "m2",
      reasoningEffort: "high",
      cwdLabel: "~/w",
      branch: "dev",
      title: "T",
      contextUsed: 1000,
      contextMax: 10_000,
      cacheHitPct: 70,
    });
    expect(statusSegments(input).left.find((s) => s.id === "model")?.text).toBe("m2 · high");
  });
});
