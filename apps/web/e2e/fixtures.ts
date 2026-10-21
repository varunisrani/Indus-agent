// Fixtures for the browser suite (P18-30).
//
// `launch(spec)` starts one whole stack in a fresh temp dir through the Python harness
// (`tests/fakes/web_stack.py`): the scripted fake model, a profile home pointing at it, a real
// `indus gateway run` daemon and a real `indus dashboard`. The harness answers JSON commands
// (restart the dashboard, run or attach the classic CLI, read the store, ...); every stack is
// stopped when the test ends and the test fails if any process naming its home is left.
//
// `urls` records every URL the browser asks for — page loads, fetches, WebSocket dials — and the
// credentials it was handed (the loopback session token read from each served page, every WS
// ticket minted). When the test ends none of those values, and no `token=` or `ticket=` query,
// may appear in any of those URLs: credentials ride only in headers, cookies and subprotocols.
//
// `term` reads and types into the Chat tab's xterm (DOM renderer: headless Chromium has a
// software GL, so the host never loads the WebGL addon).

import { type ChildProcess, execFileSync, spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createInterface, type Interface } from "node:readline";
import { test as base, expect, type Page } from "@playwright/test";

// The values the server writes into each page (lib/bootstrap.ts reads the same ones).
declare global {
  interface Window {
    __INDUS_SESSION_TOKEN__?: string;
    __INDUS_AUTH_REQUIRED__?: boolean;
  }
}

export const ROOT = resolve(import.meta.dirname, "../../..");
const PYTHON = join(ROOT, ".venv", "bin", "python");
const TUI_ENTRY = join(ROOT, "apps", "tui", "dist", "entry.js");

/** One fake-model turn: P03 steps (`{text}`, `{tool_call}`, `{finish}`, `{stall}`). */
export type Turn = Record<string, unknown>[];

export interface StackSpec {
  turns: Turn[];
  daemon?: boolean;
  /** Omit for a stack without a dashboard (a spec may start one itself with `dashboard.try`). */
  dashboard?: { host?: string; args?: string[] } | null;
  configExtra?: string;
  /** Username/password login (gated mode needs a provider); stored as its hash. */
  basicAuth?: { username: string; password: string };
}

export type Reply = Record<string, unknown> & { ok?: boolean; error?: string };

export class Stack {
  readonly url: string;
  readonly home: string;
  readonly work: string;
  #proc: ChildProcess;
  #lines: Interface;
  #queue: string[] = [];
  #waiting: ((line: string | null) => void)[] = [];
  #closed = false;
  #stderr = "";
  #dir = "";

  constructor(proc: ChildProcess, lines: Interface, ready: Reply) {
    this.#proc = proc;
    this.#lines = lines;
    this.url = String(ready.url ?? "");
    this.home = String(ready.home);
    this.work = String(ready.work);
    lines.on("line", (line) => this.#push(line));
    lines.on("close", () => this.#end());
    proc.stderr?.on("data", (chunk: Buffer) => {
      this.#stderr = (this.#stderr + chunk.toString("utf8")).slice(-6000);
    });
  }

  static async start(spec: StackSpec): Promise<Stack> {
    const dir = mkdtempSync(join(tmpdir(), "indus-e2e-"));
    const file = join(dir, "spec.json");
    writeFileSync(
      file,
      JSON.stringify({
        root: join(dir, "stack"),
        turns: spec.turns,
        daemon: spec.daemon ?? true,
        dashboard: spec.dashboard === undefined ? {} : spec.dashboard,
        config_extra: spec.configExtra ?? "",
        basic_auth: spec.basicAuth ?? null,
      }),
    );
    const proc = spawn(PYTHON, ["-m", "tests.fakes.web_stack", file], {
      cwd: ROOT,
      env: { ...process.env, PYTHONPATH: ROOT },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stderr = "";
    proc.stderr?.on("data", (chunk: Buffer) => {
      stderr = (stderr + chunk.toString("utf8")).slice(-6000);
    });
    const lines = createInterface({ input: proc.stdout as NodeJS.ReadableStream });
    const first = await new Promise<string | null>((done) => {
      lines.once("line", (line) => done(line));
      lines.once("close", () => done(null));
    });
    const ready = first ? (JSON.parse(first) as Reply) : null;
    if (!ready?.ready) {
      proc.kill("SIGTERM");
      await new Promise<void>((done) => {
        if (proc.exitCode !== null) done();
        else proc.once("exit", () => done());
      });
      rmSync(dir, { recursive: true, force: true });
      throw new Error(`the stack did not start: ${first ?? "(no output)"}\n${stderr}`);
    }
    const stack = new Stack(proc, lines, ready);
    stack.#dir = dir;
    return stack;
  }

  #push(line: string): void {
    const waiter = this.#waiting.shift();
    if (waiter) waiter(line);
    else this.#queue.push(line);
  }

  #end(): void {
    this.#closed = true;
    for (const waiter of this.#waiting.splice(0)) waiter(null);
  }

  /** One harness command; throws when the harness refused it. */
  async ask(op: string, args: Record<string, unknown> = {}): Promise<Reply> {
    if (this.#closed) throw new Error(`the stack has ended:\n${this.#stderr}`);
    this.#proc.stdin?.write(`${JSON.stringify({ op, ...args })}\n`);
    const line =
      this.#queue.shift() ?? (await new Promise<string | null>((done) => this.#waiting.push(done)));
    if (line === null) throw new Error(`the stack ended during ${op}:\n${this.#stderr}`);
    const reply = JSON.parse(line) as Reply;
    if (reply.ok === false) throw new Error(`${op}: ${reply.error}`);
    return reply;
  }

  /** The classic CLI's screen matched `pattern` (a regular expression) within `timeout` s. */
  async cliExpect(pattern: string, timeout = 60): Promise<string> {
    const reply = await this.ask("cli.expect", { pattern, timeout });
    if (!reply.matched) {
      throw new Error(`the CLI never showed /${pattern}/:\n${String(reply.screen)}`);
    }
    return String(reply.screen);
  }

  async rows(query: string): Promise<unknown[][]> {
    return (await this.ask("sql", { query })).rows as unknown[][];
  }

  /** Stop everything; the pids still naming the home (none expected). */
  async close(): Promise<number[]> {
    if (this.#closed) return [];
    this.#proc.stdin?.write(`${JSON.stringify({ op: "quit" })}\n`);
    let stray: number[] = [];
    for (;;) {
      const line =
        this.#queue.shift() ??
        (await new Promise<string | null>((done) => this.#waiting.push(done)));
      if (line === null) break;
      const reply = JSON.parse(line) as Reply;
      if (reply.stopped) {
        stray = (reply.stray as number[]) ?? [];
        break;
      }
    }
    await new Promise<void>((done) => {
      if (this.#proc.exitCode !== null) done();
      else this.#proc.once("exit", () => done());
    });
    this.#lines.close();
    if (this.#dir) rmSync(this.#dir, { recursive: true, force: true });
    return stray;
  }
}

/** The Chat tab's terminal. */
export class Term {
  readonly page: Page;

  constructor(page: Page) {
    this.page = page;
  }

  /** The visible rows as text (one line per row). */
  async text(): Promise<string> {
    return this.page
      .locator('[data-testid="chat-terminal"] .xterm-rows')
      .evaluate((rows) => Array.from(rows.children, (row) => row.textContent ?? "").join("\n"));
  }

  /** Wait until the screen shows `needle` (a string or pattern). */
  async shows(needle: string | RegExp, timeout = 60_000): Promise<void> {
    await expect
      .poll(
        async () => {
          const screen = await this.text();
          return typeof needle === "string" ? screen.includes(needle) : needle.test(screen);
        },
        { timeout, message: `the terminal to show ${String(needle)}` },
      )
      .toBe(true);
  }

  async focus(): Promise<void> {
    await this.page.locator('[data-testid="chat-terminal"] .xterm-helper-textarea').focus();
  }

  /** Type text into the TUI, then Enter. */
  async submit(text: string): Promise<void> {
    await this.focus();
    await this.page.keyboard.type(text, { delay: 15 });
    await this.page.keyboard.press("Enter");
  }

  async press(key: string): Promise<void> {
    await this.focus();
    await this.page.keyboard.press(key);
  }
}

export interface UrlLog {
  urls: string[];
  secrets: Set<string>;
  sockets: string[];
}

const SECRET_QUERY = /[?&](token|ticket|session_token|access_token)=/i;

/** Every recorded URL that carries a credential (empty when the rule holds). */
export function leaks(log: UrlLog): string[] {
  return log.urls.filter(
    (url) => SECRET_QUERY.test(url) || [...log.secrets].some((secret) => url.includes(secret)),
  );
}

const TOKEN_IN_PAGE = /__INDUS_SESSION_TOKEN__\s*=\s*("(?:[^"\\]|\\.)*")/;

function ensureTui(): void {
  if (existsSync(TUI_ENTRY)) return;
  execFileSync("pnpm", ["--filter", "@indus/tui", "run", "build"], { cwd: ROOT, stdio: "inherit" });
}

export const test = base.extend<{
  launch: (spec: StackSpec) => Promise<Stack>;
  urls: UrlLog;
  term: Term;
}>({
  // biome-ignore lint/correctness/noEmptyPattern: Playwright reads a fixture's needs from this pattern
  launch: async ({}, use) => {
    ensureTui();
    const started: Stack[] = [];
    await use(async (spec) => {
      const stack = await Stack.start(spec);
      started.push(stack);
      return stack;
    });
    const strays: number[] = [];
    for (const stack of started.reverse()) strays.push(...(await stack.close()));
    expect(strays, "processes still naming a test home").toEqual([]);
  },
  urls: [
    async ({ context }, use) => {
      const log: UrlLog = { urls: [], secrets: new Set(), sockets: [] };
      const watch = (page: Page) => {
        page.on("websocket", (socket) => {
          log.urls.push(socket.url());
          log.sockets.push(socket.url());
        });
      };
      context.pages().forEach(watch);
      context.on("page", watch);
      context.on("request", (request) => log.urls.push(request.url()));
      context.on("response", async (response) => {
        try {
          const kind = response.request().resourceType();
          if (kind === "document") {
            const found = TOKEN_IN_PAGE.exec(await response.text());
            const token = found ? (JSON.parse(found[1] as string) as string) : "";
            if (token) log.secrets.add(token);
          } else if (response.url().includes("/api/auth/ws-ticket") && response.ok()) {
            const body = (await response.json()) as { ticket?: string };
            if (body.ticket) log.secrets.add(body.ticket);
          }
        } catch {
          // A body that is gone (navigation) holds nothing to learn.
        }
      });
      await use(log);
      expect(leaks(log), "URLs carrying a credential").toEqual([]);
    },
    { auto: true },
  ],
  term: async ({ page }, use) => {
    await use(new Term(page));
  },
});

export type { Page };
export { expect };
