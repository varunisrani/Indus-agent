// Demo 2 harness (P12-33): the built renderer (`dist/entry.js`) in a real pseudo-terminal
// (node-pty), its screen read back through a headless xterm, the real `indus tui-bridge --stdio`
// child, and the scripted fake model (P03) served on a loopback port by
// `tests/fakes/model/http_serve.py`. Everything lives in a temporary home.

import { type ChildProcess, execFileSync, spawn } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";
import xterm from "@xterm/headless";
import pty from "node-pty";

export const ROOT = resolve(import.meta.dirname, "../../..");
export const APP_DIR = resolve(import.meta.dirname, "..");
export const ENTRY = join(APP_DIR, "dist", "entry.js");
const BIN = join(ROOT, ".venv", process.platform === "win32" ? "Scripts" : "bin");
export const PYTHON = join(BIN, process.platform === "win32" ? "python.exe" : "python");
export const INDUS = join(BIN, process.platform === "win32" ? "indus.exe" : "indus");

/** Why the suite cannot run here, or null when it can. */
export function missingPrerequisite(): string | null {
  if (process.platform === "win32") return "no pseudo-terminal driver for Windows in this suite";
  if (!existsSync(INDUS)) return `no ${INDUS} (run uv sync)`;
  if (!existsSync(ENTRY)) return `no ${ENTRY} (run pnpm --filter @indus/tui run build)`;
  return null;
}

export function sleep(ms: number): Promise<void> {
  return new Promise((done) => setTimeout(done, ms));
}

/** Poll `check` every 25 ms until it holds, else fail naming `what` (and the screen, if given). */
export async function until(
  check: () => boolean,
  what: string,
  ms = 20_000,
  context: () => string = () => "",
): Promise<void> {
  const end = Date.now() + ms;
  while (!check()) {
    if (Date.now() > end) {
      const extra = context();
      throw new Error(`timed out after ${ms} ms waiting for ${what}${extra ? `\n${extra}` : ""}`);
    }
    await sleep(25);
  }
}

// -- the fake model ---------------------------------------------------------------------------

/** One fake-model script step (the P03 format, see tests/fakes/model/README.md). */
export type Step = Record<string, unknown>;

export interface ModelRequest {
  kind: "model" | "title" | "models";
  path: string;
  user: string;
  turn?: number;
  model?: string;
}

export class FakeModel {
  readonly requests: ModelRequest[] = [];
  port = 0;
  private readonly child: ChildProcess;

  private constructor(child: ChildProcess) {
    this.child = child;
  }

  /** Serve a script: a name from tests/fixtures/model_scripts, or turns written to `dir`. */
  static async start(script: string | Step[][], dir: string): Promise<FakeModel> {
    let source = typeof script === "string" ? script : "";
    if (typeof script !== "string") {
      source = join(dir, `script-${Date.now()}.json`);
      writeFileSync(source, JSON.stringify({ turns: script.map((steps) => ({ steps })) }));
    }
    const child = spawn(PYTHON, ["-m", "tests.fakes.model.http_serve", source], {
      cwd: ROOT,
      stdio: ["pipe", "pipe", "inherit"],
      env: { ...process.env, PYTHONPATH: ROOT },
    });
    const model = new FakeModel(child);
    const lines = createInterface({ input: child.stdout as NodeJS.ReadableStream });
    lines.on("line", (line) => {
      const data = JSON.parse(line) as { port?: number } & ModelRequest;
      if (typeof data.port === "number") model.port = data.port;
      else model.requests.push(data);
    });
    await until(() => model.port !== 0 || child.exitCode !== null, "the fake model to listen");
    if (model.port === 0) throw new Error(`fake model exited with ${child.exitCode}`);
    return model;
  }

  /** Requests that used up a script turn (title and model-list calls are not counted). */
  get turns(): ModelRequest[] {
    return this.requests.filter((r) => r.kind === "model");
  }

  stop(): void {
    this.child.stdin?.end();
    this.child.kill("SIGTERM");
  }
}

// -- a temporary home -----------------------------------------------------------------------

export class Home {
  readonly root: string;
  readonly home: string;
  readonly work: string;
  readonly activeFile: string;

  constructor(label: string) {
    // A short folder keeps the screen goldens independent of the platform's long temp paths.
    const base = existsSync("/tmp") ? "/tmp" : tmpdir();
    this.root = realpathSync(mkdtempSync(join(base, `ie2e-${label}-`)));
    this.home = join(this.root, "home");
    this.work = join(this.root, "work");
    this.activeFile = join(this.root, "active-session.json");
    mkdirSync(this.home);
    mkdirSync(this.work);
  }

  /** Point the profile at the fake model (one model id, or several for the picker). */
  configure(port: number, extra: string[] = []): void {
    writeFileSync(
      join(this.home, "config.yaml"),
      [
        "model:",
        "  default: fake-model",
        "  provider: custom",
        `  base_url: http://127.0.0.1:${port}/v1`,
        ...extra,
        "",
      ].join("\n"),
    );
  }

  /** The stored id the renderer last reported through the launcher's active-session file. */
  activeSession(): string | null {
    try {
      const data = JSON.parse(readFileSync(this.activeFile, "utf8")) as { session_id?: unknown };
      return typeof data.session_id === "string" ? data.session_id : null;
    } catch {
      return null;
    }
  }

  /** The stored transcript of one session, read through `indus sessions export`. */
  storedMessages(sessionId: string): { role: string; content: string }[] {
    const out = join(this.root, `export-${Date.now()}.jsonl`);
    execFileSync(
      INDUS,
      ["sessions", "export", out, "--session-id", sessionId, "--no-redact", "--force", "-y"],
      { env: cleanEnv({ INDUS_HOME: this.home }), cwd: this.work, stdio: "pipe" },
    );
    const rows: { role: string; content: string }[] = [];
    for (const line of readFileSync(out, "utf8").split("\n")) {
      if (!line.trim()) continue;
      const record = JSON.parse(line) as { kind?: string; role?: string; content?: unknown };
      if (record.kind !== "message") continue;
      rows.push({
        role: String(record.role),
        content:
          typeof record.content === "string" ? record.content : JSON.stringify(record.content),
      });
    }
    return rows;
  }

  remove(): void {
    rmSync(this.root, { recursive: true, force: true });
  }
}

/** The process env minus anything that could leak a real key, profile or launch option in. */
export function cleanEnv(extra: Record<string, string>): Record<string, string> {
  const env: Record<string, string> = {};
  for (const [name, value] of Object.entries(process.env)) {
    if (value === undefined) continue;
    if (/^INDUS_|_API_KEY$|^NODE_OPTIONS$|^TMUX|^TERM_PROGRAM|^KITTY_|^WEZTERM_/.test(name))
      continue;
    env[name] = value;
  }
  return {
    ...env,
    NO_PROXY: "127.0.0.1,localhost",
    no_proxy: "127.0.0.1,localhost",
    ...extra,
  };
}

// -- the renderer in a pseudo-terminal --------------------------------------------------------

export interface LaunchOptions {
  cols?: number;
  rows?: number;
  env?: Record<string, string>;
}

export interface ExitStatus {
  exitCode: number;
  signal: number | undefined;
}

export class Tui {
  /** Every byte the renderer wrote, in order. */
  raw = "";
  readonly term: xterm.Terminal;
  readonly exited: Promise<ExitStatus>;
  exit: ExitStatus | null = null;
  private readonly proc: pty.IPty;
  readonly home: Home;
  private pending = 0;
  private readonly listeners = new Set<(data: string, at: number) => void>();

  constructor(home: Home, options: LaunchOptions = {}) {
    this.home = home;
    const cols = options.cols ?? 100;
    const rows = options.rows ?? 30;
    this.term = new xterm.Terminal({ cols, rows, allowProposedApi: true, scrollback: 0 });
    this.proc = pty.spawn(process.execPath, [ENTRY], {
      name: "xterm-256color",
      cols,
      rows,
      cwd: home.work,
      env: cleanEnv({
        INDUS_HOME: home.home,
        INDUS_TUI_BRIDGE_COMMAND: INDUS,
        INDUS_TUI_ACTIVE_SESSION_FILE: home.activeFile,
        NODE_ENV: "production",
        TERM: "xterm-256color",
        COLORTERM: "truecolor",
        ...options.env,
      }),
    });
    this.proc.onData((data) => {
      this.raw += data;
      const at = performance.now();
      for (const listener of this.listeners) listener(data, at);
      this.pending += 1;
      this.term.write(data, () => {
        this.pending -= 1;
      });
    });
    // Answers to the renderer's queries (device attributes, background colour) go back to it,
    // as a real terminal's would.
    this.term.onData((data) => {
      if (this.exit === null) this.proc.write(data);
    });
    this.exited = new Promise((done) => {
      this.proc.onExit((status) => {
        this.exit = { exitCode: status.exitCode, signal: status.signal };
        done(this.exit);
      });
    });
  }

  /** See every chunk the renderer writes, with its arrival time; returns the unsubscribe. */
  listen(listener: (data: string, at: number) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  get pid(): number {
    return this.proc.pid;
  }

  get cols(): number {
    return this.term.cols;
  }

  /** The visible screen, one string per row, trailing blanks trimmed. */
  lines(): string[] {
    const buffer = this.term.buffer.active;
    const out: string[] = [];
    for (let row = 0; row < this.term.rows; row += 1) {
      out.push(buffer.getLine(buffer.viewportY + row)?.translateToString(true) ?? "");
    }
    return out;
  }

  screen(): string {
    return this.lines().join("\n");
  }

  /** Wait until everything written so far has reached the headless terminal. */
  async settled(): Promise<void> {
    await until(() => this.pending === 0, "the terminal to catch up", 5_000);
  }

  async waitFor(pattern: string | RegExp, ms = 20_000): Promise<void> {
    const test = (text: string): boolean =>
      typeof pattern === "string" ? text.includes(pattern) : pattern.test(text);
    await until(
      () => this.pending === 0 && test(this.screen()),
      `${pattern} on screen`,
      ms,
      () => this.dump(),
    );
  }

  async waitForGone(pattern: string | RegExp, ms = 20_000): Promise<void> {
    const test = (text: string): boolean =>
      typeof pattern === "string" ? text.includes(pattern) : pattern.test(text);
    await until(
      () => this.pending === 0 && !test(this.screen()),
      `${pattern} to leave the screen`,
      ms,
      () => this.dump(),
    );
  }

  /** Wait until the screen has not changed for `quietMs`. */
  async quiet(quietMs = 300, ms = 10_000): Promise<void> {
    let last = this.screen();
    let since = Date.now();
    await until(
      () => {
        const now = this.screen();
        if (now !== last || this.pending > 0) {
          last = now;
          since = Date.now();
          return false;
        }
        return Date.now() - since >= quietMs;
      },
      "a quiet screen",
      ms,
      () => this.dump(),
    );
  }

  dump(): string {
    return `--- screen (${this.term.cols}x${this.term.rows}) ---\n${this.screen()}\n---`;
  }

  write(keys: string): void {
    this.proc.write(keys);
  }

  /** Type text the way a person does: one key at a time. */
  async type(text: string): Promise<void> {
    for (const ch of text) {
      this.proc.write(ch);
      await sleep(4);
    }
  }

  /** Type a line and press Enter. */
  async submit(text: string): Promise<void> {
    await this.type(text);
    await sleep(30);
    this.proc.write("\r");
  }

  resize(cols: number, rows: number): void {
    this.proc.resize(cols, rows);
    this.term.resize(cols, rows);
  }

  /** The live bridge child's pid (a direct child of the renderer running `tui-bridge`). */
  bridgePid(): number | null {
    try {
      const out = execFileSync("pgrep", ["-P", String(this.proc.pid), "-f", "tui-bridge"], {
        encoding: "utf8",
      });
      const pids = out
        .split(/\s+/)
        .filter(Boolean)
        .map((word) => Number(word));
      return pids[0] ?? null;
    } catch {
      return null;
    }
  }

  signal(name: NodeJS.Signals): void {
    this.proc.kill(name);
  }

  /** Make sure nothing is left running (the renderer, then its bridge group). */
  async dispose(): Promise<void> {
    if (this.exit === null) {
      const bridge = this.bridgePid();
      try {
        this.proc.kill("SIGKILL");
      } catch {
        // already gone
      }
      if (bridge !== null) {
        try {
          process.kill(-bridge, "SIGKILL");
        } catch {
          // already gone
        }
      }
      await Promise.race([this.exited, sleep(2_000)]);
    }
    this.term.dispose();
  }
}

/** Is a process still running? A zombie waiting for a parent that never reaps it is not. */
export function alive(pid: number): boolean {
  try {
    process.kill(pid, 0);
  } catch {
    return false;
  }
  try {
    const status = readFileSync(`/proc/${pid}/status`, "utf8");
    return !/^State:\s+Z/m.test(status);
  } catch {
    return true; // no /proc (macOS): the signal probe is all we have
  }
}
