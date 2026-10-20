// The TUI's dual-host setup (P17-26): the renderer's real runtime (`AppRuntime`: client, turn
// controller, event handler, cards, stores) linked to either host an agent can run in —
//
// - `stdio`: the TUI's own `indus tui-bridge --stdio` child (`indus --tui --local`);
// - `daemon`: the profile's gateway daemon over its loopback WebSocket, reached the way the entry
//   does it by default: `indus gateway grant --json` (which starts the daemon when none runs)
//   gives a single-use ticket, then the attach transport dials and waits for ready.
//
// Both run in a temporary profile (`INDUS_HOME`, `HOME` and the config folder are temp folders)
// whose model is the scripted fake (P03) served on a loopback port by
// `tests/fakes/model/http_serve.py`. A test is written once against `HostRig` and run for each
// kind with `describe.each(HOST_KINDS)`; `hostsMissing()` says why the suite cannot run here (no
// `.venv`, Windows). `HostRig.dispose()` stops the link, the daemon and any process still naming
// the temp home — only those, so nothing outside the test is ever signalled.

import { type ChildProcess, execFile, spawn } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { type ConnectHooks, IndusClient, type Transport } from "@indus/client";
import { spawnStdioTransport } from "@indus/client/stdio";
import { AppRuntime, LinkWatch } from "../app/appRuntime.ts";
import { settle } from "../composer/testSupport.ts";
import { resetStores } from "../testing/harness.ts";
import { attachConnector, grantRunner } from "../transport/attach.ts";

export type HostKind = "stdio" | "daemon";
export const HOST_KINDS: readonly HostKind[] = ["stdio", "daemon"];

export const ROOT = resolve(import.meta.dirname, "../../../..");
const BIN = join(ROOT, ".venv", "bin");
export const PYTHON = join(BIN, "python");
export const INDUS = join(BIN, "indus");

/** Why the dual-host suite cannot run here, or null when it can. */
export function hostsMissing(): string | null {
  if (process.platform === "win32") return "the daemon's control socket is POSIX only here";
  if (!existsSync(INDUS) || !existsSync(PYTHON)) return `no ${INDUS} (run uv sync)`;
  return null;
}

export function sleep(ms: number): Promise<void> {
  return new Promise((done) => setTimeout(done, ms));
}

/** Poll `check` every 25 ms until it holds, else fail naming `what` (and `context()`). */
export async function until(
  check: () => boolean,
  what: string,
  ms = 30_000,
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

// -- the fake model -------------------------------------------------------------------------------

/** One fake-model script step (the P03 format, see tests/fakes/model/README.md). */
export type Step = Record<string, unknown>;

export interface ModelRequest {
  kind: "model" | "title" | "models";
  user: string;
}

/** The scripted model on a loopback port (`python -m tests.fakes.model.http_serve`). */
export class FakeModel {
  readonly requests: ModelRequest[] = [];
  port = 0;
  private readonly child: ChildProcess;

  private constructor(child: ChildProcess) {
    this.child = child;
  }

  static async start(turns: Step[][], dir: string): Promise<FakeModel> {
    const source = join(dir, `script-${Date.now()}.json`);
    writeFileSync(source, JSON.stringify({ turns: turns.map((steps) => ({ steps })) }));
    const child = spawn(PYTHON, ["-m", "tests.fakes.model.http_serve", source], {
      cwd: ROOT,
      stdio: ["pipe", "pipe", "inherit"],
      env: { ...cleanEnv(), PYTHONPATH: ROOT },
    });
    const model = new FakeModel(child);
    const lines = createInterface({ input: child.stdout as NodeJS.ReadableStream });
    lines.on("line", (line) => {
      const data = JSON.parse(line) as { port?: number } & ModelRequest;
      if (typeof data.port === "number") model.port = data.port;
      else model.requests.push(data);
    });
    await until(() => model.port !== 0 || child.exitCode !== null, "the fake model to listen");
    if (model.port === 0) throw new Error(`the fake model exited with ${child.exitCode}`);
    return model;
  }

  /** Requests that used up a script turn (title and model-list calls are not counted). */
  get turns(): number {
    return this.requests.filter((r) => r.kind === "model").length;
  }

  stop(): void {
    this.child.stdin?.end();
    this.child.kill("SIGTERM");
  }
}

// -- a temporary profile ----------------------------------------------------------------------------

/** The process env minus anything that could leak a real key, profile or launch option in. */
export function cleanEnv(): Record<string, string> {
  const env: Record<string, string> = {};
  for (const [name, value] of Object.entries(process.env)) {
    if (value === undefined) continue;
    if (/^INDUS_|_API_KEY$|^NODE_OPTIONS$|^NOTIFY_SOCKET$/.test(name)) continue;
    env[name] = value;
  }
  return env;
}

export class HostHome {
  readonly root: string;
  readonly home: string;
  readonly work: string;
  private readonly userHome: string;

  constructor(label: string) {
    const base = existsSync("/tmp") ? "/tmp" : tmpdir();
    this.root = realpathSync(mkdtempSync(join(base, `ihost-${label}-`)));
    this.home = join(this.root, "home");
    this.work = join(this.root, "work");
    this.userHome = join(this.root, "user-home");
    for (const dir of [this.home, this.work, this.userHome]) mkdirSync(dir, { mode: 0o700 });
  }

  configure(port: number): void {
    writeFileSync(
      join(this.home, "config.yaml"),
      [
        "model:",
        "  default: fake-model",
        "  provider: custom",
        `  base_url: http://127.0.0.1:${port}/v1`,
        // The grant's ensure starts this daemon: it must not idle out mid-test (P17-23).
        "gateway:",
        "  idle_exit_seconds: 0",
        "",
      ].join("\n"),
    );
  }

  /** What every process of this profile runs with. */
  env(extra: Record<string, string> = {}): Record<string, string> {
    return {
      ...cleanEnv(),
      INDUS_HOME: this.home,
      HOME: this.userHome,
      XDG_CONFIG_HOME: join(this.root, "config"),
      INDUS_GATEWAY_LOCK_DIR: join(this.root, "token-locks"),
      NO_PROXY: "127.0.0.1,localhost",
      no_proxy: "127.0.0.1,localhost",
      PATH: `${BIN}:/usr/bin:/bin`,
      ...extra,
    };
  }

  /** Pids (not ours) whose environment names this home: the daemon and anything it started. */
  strays(): number[] {
    if (!existsSync("/proc")) return [];
    const marker = `INDUS_HOME=${this.home}`;
    const found: number[] = [];
    for (const name of readdirSync("/proc")) {
      if (!/^\d+$/.test(name) || Number(name) === process.pid) continue;
      try {
        if (readFileSync(`/proc/${name}/environ`, "latin1").split("\0").includes(marker)) {
          found.push(Number(name));
        }
      } catch {
        // gone, or not ours to read
      }
    }
    return found;
  }

  /** Stop the profile's daemon (`indus gateway stop`), then anything still naming the home. */
  async stopAll(): Promise<void> {
    await new Promise<void>((done) => {
      execFile(
        INDUS,
        ["gateway", "stop"],
        { env: this.env(), cwd: this.work, timeout: 60_000 },
        () => done(),
      );
    });
    const end = Date.now() + 20_000;
    while (this.strays().length > 0 && Date.now() < end) await sleep(100);
    for (const pid of this.strays()) {
      try {
        process.kill(pid, "SIGKILL");
      } catch {
        // already gone
      }
    }
  }

  remove(): void {
    rmSync(this.root, { recursive: true, force: true });
  }
}

// -- one host under test ----------------------------------------------------------------------------

export interface HostRig {
  readonly kind: HostKind;
  readonly home: HostHome;
  readonly model: FakeModel;
  readonly client: IndusClient;
  readonly runtime: AppRuntime;
  /** Exit codes the runtime asked for (none while it runs). */
  readonly exits: readonly number[];
  dispose(): Promise<void>;
}

export interface HostOptions {
  /** Extra renderer env (e.g. `INDUS_TUI_RESUME`). */
  env?: Record<string, string>;
}

/** A started runtime linked to `kind`, serving `turns`; `dispose()` stops everything. */
export async function startHost(
  kind: HostKind,
  turns: Step[][],
  options: HostOptions = {},
): Promise<HostRig> {
  resetStores();
  const home = new HostHome(kind);
  const model = await FakeModel.start(turns, home.root);
  home.configure(model.port);
  const env = home.env(options.env ?? {});
  const connect =
    kind === "stdio"
      ? (hooks: ConnectHooks): Transport =>
          spawnStdioTransport({
            command: INDUS,
            cwd: home.work,
            env,
            startupTimeoutMs: 60_000,
            ...(hooks.logs ? { logs: hooks.logs } : {}),
            onEvent: hooks.onEvent,
          })
      : attachConnector({
          plan: { mode: "daemon", command: INDUS, args: ["gateway", "grant", "--json"] },
          runGrant: grantRunner({
            command: INDUS,
            args: ["gateway", "grant", "--json"],
            env: home.env(),
            timeoutMs: 90_000,
          }),
          local: null,
          readyTimeoutMs: 60_000,
        });
  const links = new LinkWatch();
  const client = new IndusClient({ connect: links.wrap(connect), replay: kind === "daemon" });
  const exits: number[] = [];
  const runtime = new AppRuntime({
    client,
    links,
    env: options.env ?? {},
    attached: kind === "daemon",
    exit: (code) => exits.push(code),
    history: { entries: () => [], push: () => undefined },
  });
  runtime.start();
  let disposed = false;
  return {
    kind,
    home,
    model,
    client,
    runtime,
    exits,
    dispose: async () => {
      if (disposed) return;
      disposed = true;
      runtime.dispose();
      await client.close();
      await settle(50);
      await home.stopAll();
      model.stop();
      home.remove();
    },
  };
}
