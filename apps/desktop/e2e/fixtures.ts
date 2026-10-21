// The Electron end-to-end harness (P20-40): the built renderer and main process in a real Electron
// 44 window (xvfb on a headless Linux box), driven over the DevTools protocol with real input
// events (`testing/electron/cdp.ts`). Each app runs from a copy outside the checkout, with its own
// temp home and userData (so the single-instance lock never collides), the quit prompt skipped,
// agent and provider variables stripped, and only the model faked:
//
//   fake backend   `testing/fakeBackend` behind a fake `indus` (scripted daemon + serve);
//   real runtime   `tests/fakes/web_stack.py`: the scripted fake model on loopback, a real profile
//                  home, and either a daemon it started or none (the app starts one through
//                  `indus gateway ensure`, as on a person's computer).
//
// The OS browser is never touched: `xdg-open` on the app's PATH is a recorder, so a test can see
// exactly what the external opener let through. Nothing is written outside the test's temp folder.

import { type ChildProcess, spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath } from "node:url";
import { build as viteBuild } from "vite";
// @ts-expect-error -- plain JS module without types
import { buildElectron } from "../scripts/build-electron.mjs";
import { displayWrapper, electronBinary, rootSandboxArgs } from "../testing/electron/binary.ts";
import { appPage, Cdp, hasButton } from "../testing/electron/cdp.ts";
// @ts-expect-error -- plain JS module without types
import { controlQuery } from "../testing/fakeBackend/control.mjs";
import { tempDir, writeFakeIndus } from "../testing/fakeIndus.ts";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const APP_ROOT = path.resolve(HERE, "..");
export const REPO = path.resolve(APP_ROOT, "..", "..");
export const PYTHON = path.join(REPO, ".venv", "bin", "python");
export const REAL_INDUS = path.join(REPO, ".venv", "bin", "indus");
export const GUEST_PAGES = path.join(APP_ROOT, "testing", "guestPages");

/** The dev build's deep-link scheme (`singleInstance.ts`). */
export const DEV_SCHEME = "indus-dev";

const electron = electronBinary();
const display = displayWrapper();

/** Why the suite cannot run here ("" when it can). */
export const skipReason =
  process.platform !== "linux"
    ? "Linux only for now (the xdg-open recorder stands in for the OS browser; the fake daemon's control socket is POSIX)"
    : electron.path === null
      ? electron.reason
      : display === null
        ? "no display and no xvfb-run"
        : "";

/** Why the real-runtime cases cannot run here ("" when they can). */
export const realSkip =
  skipReason !== ""
    ? skipReason
    : fs.existsSync(PYTHON) && fs.existsSync(path.join(REPO, "tests", "fakes", "web_stack.py"))
      ? ""
      : "no .venv in this checkout";

export const ELECTRON_PATH = electron.path;

export const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** A loopback port nobody holds right now. */
export function freePort(): Promise<number> {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address() as net.AddressInfo;
      server.close(() => resolve(port));
    });
  });
}

// -- the built app ---------------------------------------------------------------------------------

let built: Promise<string> | null = null;

/** Build the renderer and main once per test file into a temp app folder (outside the checkout). */
export function buildApp(): Promise<string> {
  built ??= (async () => {
    const work = tempDir("indus-e2e-app-");
    const appDir = path.join(work, "app");
    fs.mkdirSync(appDir);
    fs.copyFileSync(path.join(APP_ROOT, "package.json"), path.join(appDir, "package.json"));
    // main loads node-pty (P20-31) by name at run time, as the packaged app does from its own
    // node_modules: point the copy at the workspace's
    fs.symlinkSync(path.join(APP_ROOT, "node_modules"), path.join(appDir, "node_modules"), "dir");
    await viteBuild({
      configFile: path.join(APP_ROOT, "vite.config.ts"),
      root: APP_ROOT,
      logLevel: "silent",
      build: { outDir: path.join(appDir, "dist"), emptyOutDir: true },
    });
    await (buildElectron as (dir: string) => Promise<string>)(path.join(appDir, "dist-electron"));
    return appDir;
  })();
  return built;
}

// -- environment ------------------------------------------------------------------------------------

/** Variables that must never reach the app under test (agent, provider and proxy settings). */
const STRIPPED =
  /^(?:INDUS_|OPENAI_|ANTHROPIC_|OPENROUTER_|GEMINI_|GOOGLE_API|AZURE_|AWS_|GH_|GITHUB_|ELECTRON_RUN_AS_NODE|NODE_OPTIONS|CLAUDE)/;

export function cleanEnv(extra: Record<string, string>): Record<string, string> {
  const env: Record<string, string> = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (value === undefined || STRIPPED.test(key)) continue;
    env[key] = value;
  }
  return {
    ...env,
    NO_PROXY: "127.0.0.1,localhost",
    no_proxy: "127.0.0.1,localhost",
    INDUS_DESKTOP_SKIP_QUIT_PROMPT: "1",
    INDUS_DESKTOP_SKIP_PROTOCOL_REGISTRATION: "1",
    INDUS_DESKTOP_NO_UPDATE_CHECK: "1",
    ...extra,
  };
}

/** A folder holding a fake `xdg-open` that appends each URL it is asked to open to `log`. */
export function openRecorder(dir: string): { bin: string; log: string; opened(): string[] } {
  const bin = path.join(dir, "recorder-bin");
  const log = path.join(dir, "opened.log");
  fs.mkdirSync(bin, { recursive: true });
  for (const name of ["xdg-open", "gio", "kde-open", "gnome-open"]) {
    fs.writeFileSync(
      path.join(bin, name),
      `#!/bin/sh\nprintf '%s\\n' "$*" >> ${JSON.stringify(log)}\nexit 0\n`,
      { mode: 0o755 },
    );
  }
  return {
    bin,
    log,
    opened: () =>
      fs.existsSync(log)
        ? fs
            .readFileSync(log, "utf8")
            .split("\n")
            .filter((line) => line.trim() !== "")
        : [],
  };
}

// -- one running app --------------------------------------------------------------------------------

export interface DevtoolsTarget {
  type: string;
  url: string;
  webSocketDebuggerUrl: string;
}

export interface LaunchOptions {
  /** The app's INDUS_HOME (required). */
  home: string;
  /** The `indus` the resolver uses (INDUS_DESKTOP_INDUS). */
  indus: string;
  /** userData; default a fresh folder per launch. */
  userData?: string;
  env?: Record<string, string>;
  args?: string[];
  /** Run in this folder (its `recorder-bin` goes first on PATH). */
  work: string;
}

export class RunningApp {
  readonly child: ChildProcess;
  readonly port: number;
  readonly userData: string;
  readonly recorder: ReturnType<typeof openRecorder>;
  cdp!: Cdp;
  private readonly appDir: string;
  private readonly env: Record<string, string>;

  constructor(
    child: ChildProcess,
    port: number,
    userData: string,
    recorder: ReturnType<typeof openRecorder>,
    appDir: string,
    env: Record<string, string>,
  ) {
    this.child = child;
    this.port = port;
    this.userData = userData;
    this.recorder = recorder;
    this.appDir = appDir;
    this.env = env;
  }

  async targets(): Promise<DevtoolsTarget[]> {
    return (await fetch(`http://127.0.0.1:${this.port}/json/list`)
      .then((answer) => answer.json())
      .catch(() => [])) as DevtoolsTarget[];
  }

  /** Wait for a target matching `match` and open a DevTools session on it. */
  async attach(
    match: (target: DevtoolsTarget) => boolean,
    what: string,
    timeoutMs = 20_000,
  ): Promise<Cdp> {
    const until = Date.now() + timeoutMs;
    let last = "";
    while (Date.now() < until) {
      const target = (await this.targets()).find(match);
      if (target !== undefined) {
        // A guest caught mid-navigation (the preview reuses its guest for the next address) can
        // leave the first session unanswered: drop it and attach again.
        const cdp = await Cdp.open(target.webSocketDebuggerUrl).catch(() => null);
        if (cdp !== null) {
          try {
            await cdp.send("Runtime.enable", {}, 5_000);
            return cdp;
          } catch (error) {
            last = error instanceof Error ? error.message : String(error);
            cdp.close();
          }
        }
      }
      await sleep(200);
    }
    throw new Error(`no ${what} appeared${last === "" ? "" : ` (${last})`}`);
  }

  /** Deliver a deep link the way the OS does: a second launch that hands its argv over. */
  async secondLaunch(link: string): Promise<number | null> {
    const [command, ...args] = [
      ...(display?.command ?? []),
      ELECTRON_PATH as string,
      ...rootSandboxArgs(),
      "--disable-gpu",
      this.appDir,
      link,
    ];
    const second = spawn(command as string, args, {
      stdio: "ignore",
      env: { ...this.env, INDUS_DESKTOP_USER_DATA_DIR: this.userData },
    });
    return await new Promise((resolve) => {
      const timer = setTimeout(() => {
        try {
          second.kill("SIGKILL");
        } catch {
          // gone
        }
        resolve(null);
      }, 20_000);
      second.once("exit", (code) => {
        clearTimeout(timer);
        resolve(code);
      });
    });
  }

  /** Quit the way the OS asks an app to (SIGTERM to the group this test started), then wait. */
  async stop(timeoutMs = 10_000): Promise<boolean> {
    try {
      this.cdp?.close();
    } catch {
      // closed
    }
    if (this.child.exitCode !== null || this.child.signalCode !== null) return true;
    if (this.child.pid === undefined) return true;
    const gone = new Promise<boolean>((resolve) => this.child.once("exit", () => resolve(true)));
    try {
      process.kill(-this.child.pid, "SIGTERM");
    } catch {
      return true;
    }
    const clean = await Promise.race([gone, sleep(timeoutMs).then(() => false)]);
    if (!clean) {
      try {
        process.kill(-this.child.pid, "SIGKILL");
      } catch {
        // gone
      }
    }
    return clean;
  }
}

/** Start the app (under xvfb-run when headless) and attach to its main window. */
export async function launchApp(options: LaunchOptions): Promise<RunningApp> {
  const appDir = await buildApp();
  const port = await freePort();
  const userData = options.userData ?? path.join(options.work, `user-data-${port}`);
  const recorder = openRecorder(options.work);
  const env = cleanEnv({
    PATH: `${recorder.bin}:${process.env.PATH ?? "/usr/bin:/bin"}`,
    INDUS_HOME: options.home,
    INDUS_DESKTOP_INDUS: options.indus,
    INDUS_DESKTOP_USER_DATA_DIR: userData,
    ...options.env,
  });
  const [command, ...args] = [
    ...(display?.command ?? []),
    ELECTRON_PATH as string,
    ...rootSandboxArgs(),
    "--disable-gpu",
    `--remote-debugging-port=${port}`,
    appDir,
    ...(options.args ?? []),
  ];
  const child = spawn(command as string, args, {
    cwd: options.work,
    detached: true,
    stdio: "ignore",
    env,
  });
  const app = new RunningApp(child, port, userData, recorder, appDir, env);
  try {
    app.cdp = await Cdp.open(await appPage(port, 60_000));
    await app.cdp.send("Runtime.enable");
    return app;
  } catch (error) {
    await app.stop();
    throw error;
  }
}

// -- page helpers -----------------------------------------------------------------------------------

export const CONNECTED = `document.querySelector('.connection-badge')?.dataset.phase === 'open'`;

export async function waitConnected(cdp: Cdp, timeoutMs = 90_000): Promise<void> {
  await cdp.waitFor(CONNECTED, "the connection", timeoutMs);
}

/** Type into the composer and press Enter; resolves once the person's row is on screen. */
export async function sendPrompt(cdp: Cdp, prompt: string): Promise<void> {
  await cdp.waitFor(`!!document.querySelector('textarea')`, "the composer");
  await cdp.type("textarea", prompt);
  await cdp.pressEnter();
  await cdp.waitFor(`location.hash.length > 2`, "the new session's route", 20_000);
  await cdp.waitFor(
    `[...document.querySelectorAll('.msg-user')].some((m) => m.innerText.includes(${JSON.stringify(prompt)}))`,
    "the prompt's row",
  );
}

/** Click a button by its exact text with a real mouse event. */
export async function clickButton(cdp: Cdp, label: string, timeoutMs = 20_000): Promise<void> {
  await cdp.waitFor(hasButton(label), `the "${label}" button`, timeoutMs);
  await sleep(150); // a button that just appeared may still be moving into place
  await cdp.click(label);
}

/** A real click on the first button whose text starts with `text` (labels with a hint line). */
export async function clickButtonStarting(
  cdp: Cdp,
  text: string,
  timeoutMs = 20_000,
): Promise<void> {
  const find = `[...document.querySelectorAll('button')].find((b) => b.textContent.trim().startsWith(${JSON.stringify(text)}))`;
  await cdp.waitFor(`!!${find}`, `a "${text}" button`, timeoutMs);
  await sleep(300); // let an overlay that just opened settle before aiming
  const at = await cdp.eval<{ x: number; y: number; hit: string }>(`(() => {
    const el = ${find};
    el.scrollIntoView({ block: "center" });
    const r = el.getBoundingClientRect();
    const x = r.x + r.width / 2, y = r.y + r.height / 2;
    const top = document.elementFromPoint(x, y);
    const hit = top === null ? "nothing" : el.contains(top) ? "" : top.outerHTML.slice(0, 160);
    return { x, y, hit };
  })()`);
  if (at.hit !== "") throw new Error(`"${text}" is covered by ${at.hit}`);
  for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) {
    await cdp.send("Input.dispatchMouseEvent", {
      type,
      x: at.x,
      y: at.y,
      button: "left",
      clickCount: 1,
    });
  }
}

/** A real click on the element `selector` (its centre). */
export async function clickAt(cdp: Cdp, selector: string): Promise<void> {
  const at = await cdp.eval<{ x: number; y: number } | null>(`(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) return null;
    el.scrollIntoView({ block: "center" });
    const r = el.getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  })()`);
  if (at === null) throw new Error(`no element ${selector}`);
  for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) {
    await cdp.send("Input.dispatchMouseEvent", { type, ...at, button: "left", clickCount: 1 });
  }
}

/** Set an input's value the way typing does (React hears `input`), then leave it (`focusout`). */
export async function fillField(cdp: Cdp, selector: string, value: string): Promise<void> {
  await cdp.waitFor(`!!document.querySelector(${JSON.stringify(selector)})`, selector);
  await cdp.eval(`(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    el.focus();
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, "");
    el.dispatchEvent(new Event("input", { bubbles: true }));
  })()`);
  await cdp.send("Input.insertText", { text: value });
  await cdp.eval(`document.querySelector(${JSON.stringify(selector)}).blur()`);
}

/** The shown user/assistant messages, in order (the oracle's "shown" input). */
export async function shownMessages(
  cdp: Cdp,
): Promise<Array<{ role: "user" | "assistant"; text: string }>> {
  // one entry per user bubble and per assistant text part (a turn's row holds its interim text,
  // tool rows and final text; each text part is one stored assistant message)
  return await cdp.eval(`[...document.querySelectorAll('.transcript .msg-user .msg-bubble, .transcript .msg-assistant .msg-text')]
    .map((el) => ({ role: el.closest('.msg-user') ? 'user' : 'assistant', text: el.innerText }))`);
}

/** Call the preload bridge from the page: `expr` sees `b` (= window.indusDesktop). */
export type BridgeAnswer<T> = { ok: true; value: T } | { ok: false; error: string };

export async function bridge<T>(cdp: Cdp, expr: string): Promise<BridgeAnswer<T>> {
  return await cdp.eval<BridgeAnswer<T>>(`(async () => {
    const b = window.indusDesktop;
    try { return { ok: true, value: await (${expr}) }; }
    catch (e) { return { ok: false, error: String(e && e.message || e) }; }
  })()`);
}

// -- the fake backend -------------------------------------------------------------------------------

export interface FakeBackend {
  home: string;
  indus: string;
  emit(event: Record<string, unknown>): Promise<void>;
  received(): Promise<Array<{ method?: string; params?: Record<string, unknown> }>>;
  restCalls(): Promise<Array<{ method: string; path: string }>>;
}

/** A temp home whose `indus` runs the scripted fake daemon + serve with `scenario`. */
export function fakeBackend(work: string, scenario: Record<string, unknown>): FakeBackend {
  const home = path.join(work, "home");
  fs.mkdirSync(home, { recursive: true, mode: 0o700 });
  const file = path.join(work, "scenario.json");
  fs.writeFileSync(file, JSON.stringify(scenario));
  const indus = writeFakeIndus(path.join(work, "bin", "indus"), file);
  return {
    home,
    indus,
    emit: async (event) => {
      await controlQuery(home, "fake-emit", { event });
    },
    received: async () =>
      (
        (await controlQuery(home, "fake-received", {})) as {
          result: { frames: Array<{ method?: string; params?: Record<string, unknown> }> };
        }
      ).result.frames,
    restCalls: async () => {
      const log = path.join(home, "fake-rest.log");
      if (!fs.existsSync(log)) return [];
      return fs
        .readFileSync(log, "utf8")
        .split("\n")
        .filter((line) => line !== "")
        .map((line) => {
          const [method = "", ...rest] = line.split(" ");
          return { method, path: rest.join(" ") };
        });
    },
  };
}

/** One provider set up, so the onboarding overlay (P20-33) stays away. */
export const CONFIGURED_OPTIONS = {
  status: 200,
  body: {
    providers: [
      { name: "fake", label: "Fake", auth_type: "api_key", configured: true, device_login: false },
    ],
    current: { provider: "fake", model: "fake-model" },
  },
};

export const HEALTHY = {
  description: "P20-40: a healthy fake backend with one provider set up.",
  version: "0.20.0",
  rpc: {
    readyOnConnect: true,
    methods: {
      "client.capabilities": { result: { features: ["request.decline"] } },
      "session.create": { result: { session_id: "s-e2e-1" } },
      "session.history": { result: { messages: [] } },
      "session.subscribe": { result: { session_id: "s-e2e-1", replay_epoch: "e1", last_seq: 0 } },
      "config.get": { result: { key: "x", value: null } },
      "commands.catalog": { result: { commands: [] } },
    },
  },
  rest: {
    routes: {
      "GET /api/sessions": { status: 200, body: { sessions: [] } },
      "GET /api/model/options": CONFIGURED_OPTIONS,
    },
  },
};

// -- the real runtime -------------------------------------------------------------------------------

export interface StackSpec {
  turns: unknown[];
  /** Start a daemon in the stack (false: the app starts one). */
  daemon: boolean;
  configExtra?: string;
}

export interface Stack {
  root: string;
  home: string;
  workDir: string;
  modelPort: number;
  /** Env a process of this profile home carries (what the app passes on to `indus`). */
  env: Record<string, string>;
  command(op: Record<string, unknown>): Promise<Record<string, unknown>>;
  sql<T = unknown[]>(query: string): Promise<T[]>;
  quit(): Promise<{ stray: number[] }>;
}

/** `tests/fakes/web_stack.py` with the scripted model; resolves when its ready line arrives. */
export async function startStack(work: string, spec: StackSpec): Promise<Stack> {
  const root = path.join(work, "stack");
  const specFile = path.join(work, "stack.json");
  fs.writeFileSync(
    specFile,
    JSON.stringify({
      root,
      turns: spec.turns,
      daemon: spec.daemon,
      dashboard: null,
      config_extra: spec.configExtra ?? "",
      basic_auth: null,
    }),
  );
  const child = spawn(PYTHON, ["-m", "tests.fakes.web_stack", specFile], {
    cwd: REPO,
    env: { ...cleanEnv({}), PYTHONPATH: REPO },
    stdio: ["pipe", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr?.on("data", (chunk: Buffer) => {
    stderr = (stderr + chunk.toString("utf8")).slice(-4000);
  });
  const lines = readline.createInterface({ input: child.stdout as NodeJS.ReadableStream });
  const queue: string[] = [];
  const waiting: Array<(line: string | null) => void> = [];
  lines.on("line", (line) => (waiting.shift() ?? ((l: string) => queue.push(l)))(line));
  lines.on("close", () => {
    for (const waiter of waiting.splice(0)) waiter(null);
  });
  const next = () =>
    queue.length > 0
      ? Promise.resolve(queue.shift() ?? null)
      : new Promise<string | null>((resolve) => waiting.push(resolve));
  const ready = JSON.parse((await next()) ?? "{}") as {
    ready?: boolean;
    home?: string;
    work?: string;
    model_port?: number;
    error?: string;
  };
  if (ready.ready !== true) {
    child.kill("SIGTERM");
    throw new Error(`the stack did not start: ${ready.error ?? stderr}`);
  }
  let chain: Promise<unknown> = Promise.resolve();
  const command = (op: Record<string, unknown>) => {
    const run = chain.then(async () => {
      child.stdin?.write(`${JSON.stringify(op)}\n`);
      const line = await next();
      return JSON.parse(line ?? "{}") as Record<string, unknown>;
    });
    chain = run.catch(() => undefined);
    return run;
  };
  return {
    root,
    home: String(ready.home),
    workDir: String(ready.work),
    modelPort: Number(ready.model_port),
    env: {
      HOME: path.join(root, "user-home"),
      XDG_CONFIG_HOME: path.join(root, "config"),
      INDUS_GATEWAY_LOCK_DIR: path.join(root, "token-locks"),
    },
    command,
    sql: async <T = unknown[]>(query: string) =>
      ((await command({ op: "sql", query })) as { rows?: T[] }).rows ?? [],
    quit: async () => {
      if (child.exitCode !== null) return { stray: [] };
      child.stdin?.write(`${JSON.stringify({ op: "quit" })}\n`);
      let stopped: { stray?: number[] } = {};
      for (;;) {
        const line = await Promise.race([next(), sleep(60_000).then(() => null)]);
        if (line === null) break;
        const parsed = JSON.parse(line) as { stopped?: boolean; stray?: number[] };
        if (parsed.stopped === true) {
          stopped = parsed;
          break;
        }
      }
      if (child.exitCode === null) child.kill("SIGTERM");
      return { stray: stopped.stray ?? [] };
    },
  };
}

/** Processes (pid → argv) whose environment names `home` as INDUS_HOME (ours only, by home). */
export function processesWithHome(home: string): Array<{ pid: number; argv: string }> {
  const found: Array<{ pid: number; argv: string }> = [];
  for (const entry of fs.readdirSync("/proc")) {
    if (!/^\d+$/.test(entry) || Number(entry) === process.pid) continue;
    try {
      const environ = fs.readFileSync(`/proc/${entry}/environ`, "utf8").split("\0");
      if (!environ.includes(`INDUS_HOME=${home}`)) continue;
      const stat = fs.readFileSync(`/proc/${entry}/stat`, "utf8");
      if (stat.slice(stat.lastIndexOf(")") + 2, stat.lastIndexOf(")") + 3) === "Z") continue;
      const argv = fs.readFileSync(`/proc/${entry}/cmdline`, "utf8").split("\0").join(" ").trim();
      found.push({ pid: Number(entry), argv });
    } catch {
      // gone, or not ours to read
    }
  }
  return found;
}

/**
 * Stop what is left of a test's own homes: every process whose INDUS_HOME lies under `root` (a
 * profile created during the test runs its own daemon and serve there). SIGTERM, then SIGKILL.
 * Answers what was still running before the stop.
 */
export async function stopUnder(root: string): Promise<Array<{ pid: number; argv: string }>> {
  const found: Array<{ pid: number; argv: string }> = [];
  for (const entry of fs.readdirSync("/proc")) {
    if (!/^\d+$/.test(entry) || Number(entry) === process.pid) continue;
    try {
      const home = fs
        .readFileSync(`/proc/${entry}/environ`, "utf8")
        .split("\0")
        .find((line) => line.startsWith("INDUS_HOME="));
      if (home === undefined || !home.slice("INDUS_HOME=".length).startsWith(`${root}/`)) continue;
      const argv = fs.readFileSync(`/proc/${entry}/cmdline`, "utf8").split("\0").join(" ").trim();
      found.push({ pid: Number(entry), argv });
    } catch {
      // gone, or not ours to read
    }
  }
  for (const signal of ["SIGTERM", "SIGKILL"] as const) {
    for (const { pid } of found) {
      try {
        process.kill(pid, signal);
      } catch {
        // already gone
      }
    }
    await sleep(signal === "SIGTERM" ? 3_000 : 200);
  }
  return found;
}

/** A git repository with one commit in `dir` (no global config read or written). */
export function gitRepo(dir: string): string {
  fs.mkdirSync(dir, { recursive: true });
  const env = {
    ...process.env,
    GIT_CONFIG_GLOBAL: "/dev/null",
    GIT_CONFIG_NOSYSTEM: "1",
    GIT_AUTHOR_NAME: "E2E",
    GIT_AUTHOR_EMAIL: "e2e@example.invalid",
    GIT_COMMITTER_NAME: "E2E",
    GIT_COMMITTER_EMAIL: "e2e@example.invalid",
  };
  const git = (...args: string[]) => {
    const ran = spawnSync("git", args, { cwd: dir, env, encoding: "utf8" });
    if (ran.status !== 0) throw new Error(`git ${args.join(" ")}: ${ran.stderr}`);
  };
  git("init", "-q", "-b", "main");
  fs.writeFileSync(path.join(dir, "README.md"), "# demo\n");
  git("add", "README.md");
  git("commit", "-q", "-m", "first");
  return dir;
}

export { Cdp, hasButton, tempDir };
