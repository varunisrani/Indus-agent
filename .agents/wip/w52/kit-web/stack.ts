// Shared by explore.ts and capture.ts: the repo's web_stack harness (scripted fake model, temp
// profile home, real daemon, real `indus dashboard` on the prebuilt apps/web/dist) and the seeding.
import { type ChildProcess, spawn } from "node:child_process";
import fs from "node:fs";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { createInterface } from "node:readline";

export const WT = process.env.WT ?? "";
export const OUT = process.env.OUT ?? "";
if (WT === "" || OUT === "") throw new Error("set WT and OUT");
fs.mkdirSync(OUT, { recursive: true });
if (!fs.existsSync(path.join(WT, "apps/web/dist/index.html"))) throw new Error("build apps/web first");
if (!fs.existsSync(path.join(WT, "apps/tui/dist/entry.js"))) throw new Error("build apps/tui first");

const require = createRequire(path.join(WT, "apps/web/package.json"));
export const { chromium } = require("@playwright/test") as typeof import("@playwright/test");

const PYTHON = path.join(WT, ".venv/bin/python");
export const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export type Reply = Record<string, unknown> & { ok?: boolean; error?: string };

export class Stack {
  url = "";
  dir = "";
  proc!: ChildProcess;
  lines: string[] = [];
  waiting: Array<(l: string | null) => void> = [];

  static async start(spec: Record<string, unknown>): Promise<Stack> {
    const s = new Stack();
    s.dir = fs.mkdtempSync(path.join(os.tmpdir(), "indus-shots-web-"));
    const file = path.join(s.dir, "spec.json");
    fs.writeFileSync(
      file,
      JSON.stringify({ root: path.join(s.dir, "stack"), daemon: true, dashboard: {}, config_extra: "", basic_auth: null, ...spec }),
    );
    s.proc = spawn(PYTHON, ["-m", "tests.fakes.web_stack", file], {
      cwd: WT,
      env: { ...process.env, PYTHONPATH: WT },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let err = "";
    s.proc.stderr?.on("data", (c: Buffer) => {
      err = (err + c.toString()).slice(-4000);
    });
    const rl = createInterface({ input: s.proc.stdout as NodeJS.ReadableStream });
    rl.on("line", (l) => (s.waiting.shift() ?? ((x: string) => s.lines.push(x)))(l));
    rl.on("close", () => {
      for (const w of s.waiting.splice(0)) w(null);
    });
    const first = await s.next();
    const ready = first ? (JSON.parse(first) as Reply) : null;
    if (!ready?.ready) throw new Error(`stack did not start: ${first}\n${err}`);
    s.url = String(ready.url);
    return s;
  }

  next(): Promise<string | null> {
    return this.lines.length > 0
      ? Promise.resolve(this.lines.shift() ?? null)
      : new Promise((r) => this.waiting.push(r));
  }

  async ask(op: string, args: Record<string, unknown> = {}): Promise<Reply> {
    this.proc.stdin?.write(`${JSON.stringify({ op, ...args })}\n`);
    const line = await this.next();
    const reply = JSON.parse(line ?? "{}") as Reply;
    if (reply.ok === false) throw new Error(`${op}: ${reply.error}`);
    return reply;
  }

  async close(): Promise<void> {
    if (this.proc.exitCode === null) {
      this.proc.stdin?.write(`${JSON.stringify({ op: "quit" })}\n`);
      for (;;) {
        const line = await Promise.race([this.next(), sleep(60_000).then(() => null)]);
        if (line === null) break;
        const r = JSON.parse(line) as Reply;
        if (r.stopped) {
          if ((r.stray as number[])?.length) console.error(`stray: ${JSON.stringify(r.stray)}`);
          break;
        }
      }
      if (this.proc.exitCode === null) this.proc.kill("SIGTERM");
    }
    fs.rmSync(this.dir, { recursive: true, force: true });
  }
}


export const TURNS = [
  [
    {
      text: [
        "The repository has three parts:\n\n",
        "- `src/` — the Python runtime and CLI\n",
        "- `apps/` — the web dashboard, desktop app and terminal UI\n",
        "- `tests/` — unit, integration and end-to-end suites\n",
      ],
    },
    { usage: { prompt: 1840, completion: 96 } },
  ],
  [{ text: ["Grey pelicans glide —\n", "the tide pulls the harbour lights\n", "into folded wings."] }, { usage: { prompt: 1310, completion: 24 } }],
  [{ text: ["Use `git switch -c <name>` to create ", "and switch to a new branch in one step."] }, { usage: { prompt: 1420, completion: 31 } }],
  // the browser chat (the TUI over /api/pty)
  [
    { text: ["Let me look. "] },
    { tool_call: { name: "terminal", arguments: { command: "ls" }, id: "call_ls" } },
    { usage: { prompt: 2650, completion: 40 } },
    { finish: "tool_calls" },
  ],
  [{ text: ["This folder holds the profile's working directory; ", "it is empty apart from the files you add."] }, { usage: { prompt: 2790, completion: 28 } }],
  [{ text: ["Done."] }],
  [{ text: ["Done."] }],
  [{ text: ["Done."] }],
];

export const CONFIG_EXTRA = "dashboard:\n  show_token_analytics: true\n";

/** A few rows on each page: CLI sessions, cron jobs, a webhook route, MCP servers, a profile. */
export async function seed(stack: Stack): Promise<string[]> {
  const notes: string[] = [];
  const run = async (args: string[]) => {
    const r = await stack.ask("indus", { args });
    const line = `indus ${args.join(" ")} -> ${r.code}`;
    console.log(line);
    if (r.code !== 0) notes.push(`${line}: ${String(r.stderr).slice(0, 300)} ${String(r.stdout).slice(0, 300)}`);
    return r;
  };
  for (const prompt of [
    "Summarise the repository layout",
    "Write a haiku about pelicans",
    "How do I create a new git branch?",
  ]) {
    const r = await run(["chat", "-q", prompt]);
    if (r.code !== 0) throw new Error(`indus chat failed: ${r.stderr}`);
  }
  await run(["skills", "sync"]);
  const py = path.join(WT, ".venv/bin/python");
  const fakeMcp = path.join(WT, "tests/fakes/mcp/stdio_server.py");
  await run(["mcp", "add", "demo-tools", "--command", py, "--arg", fakeMcp, "--arg", "--name", "--arg", "demo-tools"]);
  await run(["mcp", "add", "docs-search", "--url", "http://127.0.0.1:9/mcp", "--transport", "http", "--no-probe"]);
  await run(["mcp", "disable", "docs-search"]);
  await run(["cron", "create", "every day at 9am", "Summarise yesterday's commits", "--name", "Morning digest"]);
  await run(["cron", "create", "every 30m", "Check the nightly build", "--name", "Build watch", "--paused", "--reason", "Waiting for CI"]);
  await run([
    "webhook", "subscribe", "github-pushes", "--events", "push", "--prompt",
    "Summarise the push to {repository.full_name}", "--auth", "github", "--description", "Push summaries",
  ]);
  await run(["profile", "create", "research", "--description", "Literature reviews and notes", "--no-alias", "--no-skills"]);
  return notes;
}
