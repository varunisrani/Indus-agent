// Web dashboard screenshots, on the repo's browser-suite harness (tests/fakes/web_stack.py: the
// scripted fake model, a temp profile home, a real daemon and a real `indus dashboard` serving the
// prebuilt apps/web/dist). Chromium from PLAYWRIGHT_BROWSERS_PATH; nothing is downloaded.
//
//   WT=<worktree> OUT=<png dir> PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node web.ts

import { type ChildProcess, spawn } from "node:child_process";
import fs from "node:fs";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { createInterface } from "node:readline";

const WT = process.env.WT ?? "";
const OUT = process.env.OUT ?? "";
if (WT === "" || OUT === "") throw new Error("set WT and OUT");
fs.mkdirSync(OUT, { recursive: true });
if (!fs.existsSync(path.join(WT, "apps/web/dist/index.html"))) throw new Error("build apps/web first");
if (!fs.existsSync(path.join(WT, "apps/tui/dist/entry.js"))) throw new Error("build apps/tui first");

const require = createRequire(path.join(WT, "apps/web/package.json"));
const { chromium } = require("@playwright/test") as typeof import("@playwright/test");

const PYTHON = path.join(WT, ".venv/bin/python");
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

type Reply = Record<string, unknown> & { ok?: boolean; error?: string };

class Stack {
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

const browser = await chromium.launch({ headless: true });
const stacks: Stack[] = [];
const VIEW = { width: 1440, height: 900 };

async function shot(page: import("@playwright/test").Page, name: string, settle = 700) {
  await sleep(settle);
  await page.screenshot({ path: path.join(OUT, name) });
  console.log(`shot ${name}`);
}

try {
  // -- 1. the gated sign-in page (username/password login on a non-loopback bind) ----------------
  {
    const password = ["shots", "pass", String(Date.now())].join("-");
    const stack = await Stack.start({
      turns: [[{ text: "Hello." }]],
      dashboard: { host: "127.0.0.2" },
      basic_auth: { username: "admin", password },
    });
    stacks.push(stack);
    const context = await browser.newContext({ viewport: VIEW });
    const page = await context.newPage();
    await page.goto(`${stack.url}/sessions`);
    await page.getByLabel("Username").waitFor();
    await page.getByLabel("Username").fill("admin");
    await shot(page, "web-01-login.png");
    await context.close();
    await stack.close();
    stacks.pop();
  }

  // -- 2. the loopback dashboard with a few sessions -----------------------------------------------
  const stack = await Stack.start({
    turns: [
      [
        {
          text: [
            "The repository has three parts:\n\n",
            "- `src/` — the Python runtime and CLI\n",
            "- `apps/` — the web dashboard, desktop app and terminal UI\n",
            "- `tests/` — unit, integration and end-to-end suites\n",
          ],
        },
      ],
      [{ text: ["Grey pelicans glide —\n", "the tide pulls the harbour lights\n", "into folded wings."] }],
      [{ text: ["Use `git switch -c <name>` to create ", "and switch to a new branch in one step."] }],
      // the browser chat (the TUI over /api/pty)
      [
        { text: ["Let me look. "] },
        { tool_call: { name: "terminal", arguments: { command: "ls" }, id: "call_ls" } },
        { finish: "tool_calls" },
      ],
      [{ text: ["This folder holds the profile's working directory; ", "it is empty apart from the files you add."] }],
    ],
  });
  stacks.push(stack);
  for (const prompt of [
    "Summarise the repository layout",
    "Write a haiku about pelicans",
    "How do I create a new git branch?",
  ]) {
    const ran = await stack.ask("indus", { args: ["chat", "-q", prompt] });
    if (ran.code !== 0) throw new Error(`indus chat failed: ${ran.stderr}`);
  }

  const context = await browser.newContext({ viewport: VIEW });
  const page = await context.newPage();

  await page.goto(`${stack.url}/sessions`);
  await page.getByRole("textbox", { name: "Search sessions" }).waitFor();
  await page.getByRole("list", { name: "Sessions" }).getByRole("listitem").first().waitFor();
  await shot(page, "web-02-sessions.png", 1200);

  // expand the haiku session's transcript
  const row = page.getByRole("list", { name: "Sessions" }).getByRole("listitem").filter({ hasText: /haiku/i }).first();
  await row.locator("button[aria-expanded]").first().click();
  await shot(page, "web-03-session-detail.png", 1500);

  // search
  await page.getByRole("textbox", { name: "Search sessions" }).fill("branch");
  await page.getByRole("button", { name: "Search" }).click();
  await page.getByRole("list", { name: "Search results" }).getByRole("listitem").first().waitFor();
  await shot(page, "web-04-sessions-search.png", 1000);

  // the Chat tab: the real TUI in xterm over /api/pty
  await page.goto(`${stack.url}/chat`);
  const rows = page.locator('[data-testid="chat-terminal"] .xterm-rows');
  const screen = () => rows.evaluate((r) => Array.from(r.children, (c) => c.textContent ?? "").join("\n"));
  const shows = async (needle: string, timeout = 60_000) => {
    const until = Date.now() + timeout;
    while (Date.now() < until) {
      if ((await screen().catch(() => "")).includes(needle)) return;
      await sleep(250);
    }
    throw new Error(`terminal never showed ${needle}:\n${await screen().catch(() => "")}`);
  };
  await shows("Ask anything");
  await shot(page, "web-05-chat-welcome.png", 1000);
  await page.locator('[data-testid="chat-terminal"] .xterm-helper-textarea').focus();
  await page.keyboard.type("What is in this folder?", { delay: 15 });
  await page.keyboard.press("Enter");
  await shows("it is empty apart from");
  await shot(page, "web-06-chat-exchange-light.png", 1500);
  // the same exchange in the dashboard's dark theme (the TUI's colours assume a dark terminal)
  await page.getByRole("button", { name: "Switch to dark theme" }).click();
  await shot(page, "web-07-chat-exchange-dark.png", 1500);
  await page.getByRole("button", { name: "Switch to light theme" }).click();
  await sleep(500);

  for (const [route, name, ready] of [
    ["/models", "web-08-models.png", null],
    ["/config", "web-09-config.png", "agent.max_turns"],
    ["/system", "web-11-system.png", null],
  ] as const) {
    await page.goto(`${stack.url}${route}`);
    if (ready) await page.getByRole("spinbutton", { name: ready }).waitFor();
    else await page.waitForLoadState("networkidle").catch(() => undefined);
    await shot(page, name, 1500);
    if (route === "/config") {
      await page.goto(`${stack.url}/logs`);
      await page.getByRole("group", { name: "File" }).getByRole("button", { name: "gateway" }).click();
      await page.getByRole("heading", { name: "gateway.log" }).waitFor();
      await page.getByRole("list", { name: "Log lines" }).getByRole("listitem").first().waitFor();
      await shot(page, "web-10-logs.png", 1200);
    }
  }
  await context.close();
} finally {
  await browser.close().catch(() => undefined);
  for (const s of stacks.reverse()) await s.close().catch((e) => console.error(`close: ${e}`));
}
console.log("web done");
process.exit(0);
