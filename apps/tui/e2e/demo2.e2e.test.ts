// Demo 2 end to end (P12-33): the built renderer in a real pseudo-terminal, the real
// `indus tui-bridge --stdio` child and the scripted fake model on a loopback port, in a temporary
// home. Each case starts its own renderer, bridge and model so a failure cannot leak into the next.
//
// Run with `pnpm --filter @indus/tui run e2e` (builds first). Set INDUS_E2E_UPDATE=1 to rewrite the
// screen goldens in e2e/goldens/ — and review the diff before keeping it.

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { RESTART_LINE } from "../src/app/recovery.ts";
import { MODE_RESET, seq } from "../src/terminal/modes.ts";
import {
  alive,
  FakeModel,
  Home,
  missingPrerequisite,
  type Step,
  sleep,
  Tui,
  until,
} from "./harness.ts";

const missing = missingPrerequisite();
const SESSION_ID = /\d{8}_\d{6}_[0-9a-f]{6}/;
const GOLDENS = join(import.meta.dirname, "goldens");
const UPDATE = process.env.INDUS_E2E_UPDATE === "1";
const KEY = {
  enter: "\r",
  esc: "\u001b",
  down: "\u001b[B",
  ctrlC: "\u0003",
  ctrlL: "\u000c",
  ctrlO: "\u000f",
  ctrlX: "\u0018",
} as const;

interface Run {
  home: Home;
  model: FakeModel;
  tui: Tui;
}

const runs: Run[] = [];

afterEach(async () => {
  for (const run of runs.splice(0)) {
    await run.tui.dispose();
    run.model.stop();
    run.home.remove();
  }
});

interface StartOptions {
  cols?: number;
  rows?: number;
  config?: string[];
  env?: Record<string, string>;
}

/** A fresh home, model and renderer; resolves once the first session is open. */
async function start(
  label: string,
  script: string | Step[][],
  options: StartOptions = {},
): Promise<Run> {
  const home = new Home(label);
  const model = await FakeModel.start(script, home.root);
  home.configure(model.port, options.config ?? []);
  const tui = new Tui(home, {
    cols: options.cols ?? 100,
    rows: options.rows ?? 30,
    ...(options.env ? { env: options.env } : {}),
  });
  const run = { home, model, tui };
  runs.push(run);
  await tui.waitFor(SESSION_ID, 45_000);
  await until(() => home.activeSession() !== null, "the active-session file", 10_000);
  await tui.waitFor("Ask anything");
  return run;
}

/** The status rule (the line that starts with a rule glyph and names the model). */
function statusRule(tui: Tui): string {
  return tui.lines().find((line) => line.startsWith("─ ") && line.includes("│")) ?? "";
}

function userRows(home: Home, sessionId: string, text: string): number {
  return home.storedMessages(sessionId).filter((m) => m.role === "user" && m.content === text)
    .length;
}

/** The screen with what differs between machines and runs replaced by fixed tokens. */
function masked(tui: Tui, home: Home): string {
  const text = tui
    .lines()
    .map((line) =>
      line
        .replaceAll(home.work, "<work>")
        .replace(new RegExp(SESSION_ID.source, "g"), "<session>")
        .replace(/\b\d+(?:\.\d+)?s\b/g, "<t>")
        .replace(/tools(\s+)\d+ available/, "tools$1<n> available")
        .replace(/─{2,}/g, "──"),
    )
    .join("\n");
  return `${text.replace(/\n+$/, "")}\n`;
}

function matchGolden(name: string, actual: string): void {
  const path = join(GOLDENS, name);
  if (UPDATE || !existsSync(path)) {
    if (!UPDATE) throw new Error(`no golden ${name}: run with INDUS_E2E_UPDATE=1 and review it`);
    mkdirSync(GOLDENS, { recursive: true });
    writeFileSync(path, actual);
  }
  expect(actual).toBe(readFileSync(path, "utf8"));
}

/** Every mode the renderer turned on is off again, and the reset went out last. */
function expectTerminalRestored(tui: Tui): void {
  const modes = tui.term.modes;
  expect(tui.term.buffer.active.type).toBe("normal");
  expect(modes.mouseTrackingMode).toBe("none");
  expect(modes.bracketedPasteMode).toBe(false);
  expect(modes.sendFocusMode).toBe(false);
  expect(modes.synchronizedOutputMode).toBe(false);
  const lastEnter = tui.raw.lastIndexOf(seq.altScreenOn);
  expect(tui.raw.indexOf(MODE_RESET, lastEnter)).toBeGreaterThan(lastEnter);
  expect(tui.raw.lastIndexOf(seq.cursorShow)).toBeGreaterThan(tui.raw.lastIndexOf(seq.cursorHide));
}

describe.skipIf(missing !== null)("Demo 2 end to end", () => {
  it.each([80, 120])(
    "streams, shows the tool trail and takes the approval card's 1 (%i columns)",
    async (cols) => {
      const { home, model, tui } = await start(`demo${cols}`, "tui_demo2", { cols, rows: 40 });
      await tui.submit("make a counter script and run it");
      await tui.waitFor("Writing count.sh for you.");
      await tui.waitFor("Needs your approval");
      await tui.waitFor("chmod +x count.sh && ./count.sh");
      expect(tui.screen()).toMatch(/1\. Allow once/);
      expect(existsSync(join(home.work, "runs.log"))).toBe(false);
      tui.write("1");
      await tui.waitFor("All done: count.sh printed counted.");
      await tui.waitFor(/─ ready/);
      await tui.quiet();
      expect(tui.screen()).toMatch(/✓ terminal\(printf/);
      expect(tui.screen()).toMatch(/✓ terminal\(chmod \+x count\.sh && \.\/count\.sh\)/);
      expect(readFileSync(join(home.work, "runs.log"), "utf8")).toBe("once\n");
      expect(model.turns).toHaveLength(3);
      matchGolden(`demo2-${cols}x40.txt`, masked(tui, home));

      const sid = home.activeSession() as string;
      expect(userRows(home, sid, "make a counter script and run it")).toBe(1);
    },
  );

  it("answers a clarify card with a digit", async () => {
    const { model, tui } = await start("clarify", [
      [
        {
          tool_call: {
            name: "clarify",
            arguments: {
              questions: [{ question: "Which name?", choices: ["alpha.txt", "beta.txt"] }],
            },
          },
        },
      ],
      [{ text: "Going with beta.txt." }],
    ]);
    await tui.submit("pick a file name with me");
    await tui.waitFor("The agent asks a question");
    await tui.waitFor("2. beta.txt");
    tui.write("2");
    await tui.waitFor("Going with beta.txt.");
    await tui.quiet();
    const screen = tui.screen();
    expect(screen).toContain("Questions from the agent (answered)");
    expect(screen).toContain("Which name? → beta.txt");
    expect(screen).not.toContain("(timed out)");
    expect(model.turns).toHaveLength(2);
  });

  it("Ctrl+O switches the model for the session; the next request uses it", async () => {
    const { model, tui } = await start("model", [[{ text: "Answer from model b." }]]);
    tui.write(KEY.ctrlO);
    await tui.waitFor("Choose a provider");
    await tui.type("custom");
    await tui.waitFor("filter: custom");
    tui.write(KEY.enter);
    await tui.waitFor("Models of Custom endpoint");
    await tui.type("fake-model-b");
    await tui.waitFor('Enter uses "fake-model-b"');
    tui.write(KEY.enter);
    await tui.waitFor("Reasoning effort for fake-model-b");
    tui.write(KEY.enter);
    await tui.waitFor("Now using fake-model-b for this session.");
    await tui.waitFor(/─ ready │ fake-model-b/);
    await tui.submit("hello");
    await tui.waitFor("Answer from model b.");
    expect(model.turns.map((r) => r.model)).toEqual(["fake-model-b"]);
  });

  it("Ctrl+X resumes another stored session from the switcher", async () => {
    const { home, tui } = await start("switch", [
      [{ text: "Reply about apples." }],
      [{ text: "Reply about pears." }],
    ]);
    await tui.submit("first topic apples");
    await tui.waitFor("Reply about apples.");
    const first = home.activeSession() as string;
    await tui.submit("/new");
    await tui.waitFor("Start a new session?");
    tui.write("y");
    await until(
      () => (home.activeSession() ?? first) !== first,
      "a new session",
      20_000,
      () => tui.dump(),
    );
    await tui.waitForGone("Reply about apples.");
    await tui.submit("second topic pears");
    await tui.waitFor("Reply about pears.");
    const second = home.activeSession() as string;
    expect(second).not.toBe(first);

    tui.write(KEY.ctrlX);
    await tui.waitFor("Sessions");
    // The stored session is listed under the title the model gave it after its first reply.
    const label = "Chat: first topic apples";
    await tui.waitFor(label);
    for (let step = 0; step < 12; step += 1) {
      const selected = tui.lines().find((line) => line.includes("│ › "));
      if (selected?.includes(label)) break;
      tui.write(KEY.down);
      await sleep(150);
      await tui.settled();
    }
    expect(tui.lines().find((line) => line.includes("│ › "))).toContain(label);
    tui.write(KEY.enter);
    await until(
      () => home.activeSession() === first,
      "the switch back",
      20_000,
      () => tui.dump(),
    );
    await tui.waitFor("Reply about apples.");
    await tui.waitFor("first topic apples");
    expect(tui.screen()).not.toContain("Reply about pears.");
  });

  it("queues a prompt typed while busy and sends it after the reply", async () => {
    const { model, tui } = await start("queue", [
      [{ text: ["Slow ", "start"] }, { stall: 2 }, { text: " and the end." }],
      [{ text: "Queued reply." }],
    ]);
    await tui.submit("slow one");
    await tui.waitFor("Slow start");
    await tui.submit("second while busy");
    await tui.waitFor("queued 1. second while busy");
    expect(model.turns).toHaveLength(1);
    await tui.waitFor("Queued reply.");
    await tui.waitForGone("queued 1.");
    expect(model.turns.map((r) => r.user)).toEqual(["slow one", "second while busy"]);
    const lines = tui.lines();
    const firstReply = lines.findIndex((line) => line.includes("Slow start and the end."));
    const queued = lines.findIndex((line) => line.includes("› second while busy"));
    expect(firstReply).toBeGreaterThanOrEqual(0);
    expect(queued).toBeGreaterThan(firstReply);
  });

  it("steers a running turn when the busy mode is steer", async () => {
    const { model, tui } = await start(
      "steer",
      [
        [{ text: ["Working ", "on it"] }, { stall: 2 }, { text: " now." }],
        [{ text: "Took the steer." }],
      ],
      { config: ["display:", "  busy_input_mode: steer"] },
    );
    await tui.submit("long task");
    await tui.waitFor("Working on it");
    await tui.submit("also add tests");
    await tui.waitFor("Took the steer.");
    expect(model.turns).toHaveLength(2);
    expect(model.turns[1]?.user).toContain("also add tests");
    expect(tui.screen()).not.toContain("queued 1.");
  });

  it("Ctrl+C clears the draft, then interrupts the turn, then exits from an empty idle prompt", async () => {
    const { tui } = await start("ctrlc", [[{ text: ["Long ", "partial"] }, { stall: 30 }]]);
    await tui.submit("go on for a while");
    await tui.waitFor("Long partial");
    await tui.type("draft text");
    await tui.waitFor("› draft text");
    tui.write(KEY.ctrlC);
    await tui.waitForGone("draft text");
    expect(statusRule(tui)).not.toContain("interrupted");
    expect(tui.exit).toBeNull();
    tui.write(KEY.ctrlC);
    await tui.waitFor(/─ interrupted/);
    await tui.waitFor("Long partial");
    expect(tui.exit).toBeNull();
    tui.write(KEY.ctrlC);
    const status = await tui.exited;
    expect(status.exitCode).toBe(0);
    await tui.settled();
    expectTerminalRestored(tui);
  });

  it("kill -9 of the bridge mid-turn reopens the same stored session without resending", async () => {
    const { home, model, tui } = await start("kill", [
      [{ text: ["Long ", "partial"] }, { stall: 60 }],
      [{ text: "After the restart." }],
    ]);
    const sid = home.activeSession() as string;
    await tui.submit("start a long one");
    await tui.waitFor("Long partial");
    const first = tui.bridgePid();
    expect(first).not.toBeNull();
    process.kill(first as number, "SIGKILL");
    await tui.waitFor(RESTART_LINE);
    await until(
      () => {
        const pid = tui.bridgePid();
        return pid !== null && pid !== first;
      },
      "a new bridge child",
      20_000,
    );
    await tui.waitFor(/─ ready/, 30_000);
    expect(home.activeSession()).toBe(sid);
    await tui.waitFor("› start a long one");
    await sleep(500);
    expect(model.turns).toHaveLength(1);
    expect(userRows(home, sid, "start a long one")).toBe(1);

    await tui.submit("and now?");
    await tui.waitFor("After the restart.");
    expect(model.turns).toHaveLength(2);
    expect(home.activeSession()).toBe(sid);
    expect(userRows(home, sid, "start a long one")).toBe(1);
    expect(userRows(home, sid, "and now?")).toBe(1);
  });

  it("a 200-event resize storm ends in one clean frame at the last size", async () => {
    const { tui } = await start("resize", [[{ text: "A reply to keep on screen." }]], {
      env: { TERM_PROGRAM: "WezTerm" },
    });
    await tui.submit("say something");
    await tui.waitFor("A reply to keep on screen.");
    await tui.quiet();
    const before = tui.raw.length;
    for (let i = 0; i < 200; i += 1) {
      tui.resize(60 + ((i * 37) % 80), 20 + ((i * 11) % 25));
      await sleep(5);
    }
    tui.resize(117, 33);
    await tui.quiet(500);
    const storm = tui.raw.slice(before);
    // Synchronized-output frames stay whole: every begin has its end, never two begins in a row.
    const marks = storm
      .split(seq.syncBegin.slice(0, -1))
      .slice(1)
      .map((part) => part[0]);
    expect(marks.length).toBeGreaterThan(0);
    expect(marks.join("")).toMatch(/^(hl)+$/);
    const rule = statusRule(tui);
    expect(rule).toContain("ready");
    expect([...rule].length).toBe(117);
    const lines = tui.lines();
    expect(lines.filter((line) => line.includes("Ask anything"))).toHaveLength(1);
    expect(lines.filter((line) => line.includes("A reply to keep on screen."))).toHaveLength(1);
    expect(lines.filter((line) => line.startsWith("─ ") && line.includes("│"))).toHaveLength(1);
    const settled = tui.screen();
    tui.write(KEY.ctrlL);
    await tui.quiet(400);
    expect(tui.screen()).toBe(settled);
  });

  it("/quit leaves a clean terminal and no bridge behind", async () => {
    const { tui } = await start("quit", [[{ text: "Bye." }]]);
    const bridge = tui.bridgePid() as number;
    await tui.submit("/quit");
    const status = await tui.exited;
    expect(status.exitCode).toBe(0);
    await tui.settled();
    expectTerminalRestored(tui);
    await until(() => !alive(bridge), "the bridge to stop", 10_000);
  });

  it("SIGTERM leaves a clean terminal and no bridge behind", async () => {
    const { tui } = await start("term", [[{ text: ["Half ", "way"] }, { stall: 30 }]]);
    await tui.submit("keep going");
    await tui.waitFor("Half way");
    const bridge = tui.bridgePid() as number;
    tui.signal("SIGTERM");
    const status = await tui.exited;
    expect(status.exitCode).toBe(143);
    await tui.settled();
    expectTerminalRestored(tui);
    await until(() => !alive(bridge), "the bridge to stop", 10_000);
  });
});

/** 10,000 tokens, `perTick` per SSE frame, one frame every 4 ms (the D-04.2 (f) stream shape). */
function tokenStream(perTick: number): Step[][] {
  const steps: Step[] = [];
  for (let token = 0; token < 10_000; token += perTick) {
    const words: string[] = [];
    for (let i = token; i < Math.min(10_000, token + perTick); i += 1) words.push(`tok${i} `);
    steps.push({ text: words.join("") }, { stall: 0.004 });
  }
  return [steps];
}

// D-04.2 (f) re-bench on the real renderer: opt-in (INDUS_E2E_BENCH=1), prints its numbers.
describe.runIf(missing === null && process.env.INDUS_E2E_BENCH === "1")("stream bench", () => {
  it.each([
    [5_000, 20],
    [1_000, 4],
  ])("10k tokens at %i tokens/s through the real renderer (100x40)", async (rate, perTick) => {
    const { tui } = await start(`bench${rate}`, tokenStream(perTick), {
      rows: 40,
      env: { TERM_PROGRAM: "WezTerm" },
    });
    let first = 0;
    let last = 0;
    let frames = 0;
    const stop = tui.listen((data, at) => {
      if (first === 0 && data.includes("tok0 ")) first = at;
      if (first !== 0 && last === 0) frames += data.split("\u001b[?2026h").length - 1;
      if (data.includes("tok9999")) last = at;
    });
    await tui.submit("stream");
    await until(
      () => last !== 0,
      "the last token",
      60_000,
      () => tui.dump(),
    );
    stop();
    expect(last).toBeGreaterThan(first);
    const seconds = (last - first) / 1000;
    process.stdout.write(
      `D-04.2 (f) real renderer, ${rate} tok/s: ${(frames / seconds).toFixed(1)} fps ` +
        `(${frames} frames / ${seconds.toFixed(2)} s)\n`,
    );
  });
});

if (missing !== null) {
  describe("Demo 2 end to end (not runnable here)", () => {
    it.skip(`skipped: ${missing}`, () => {});
  });
}
