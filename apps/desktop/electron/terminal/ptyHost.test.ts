import os from "node:os";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { FakeTimers } from "../../testing/fakeTimers.ts";
import { type PaneIpcHarness, paneIpcHarness } from "../../testing/paneIpc.ts";
import {
  chooseShell,
  installTerminalIpc,
  loadNodePty,
  OUTPUT_BATCH_MS,
  type Pty,
  PtyHost,
  type PtySpawnOptions,
} from "./ptyHost.ts";

class FakePty implements Pty {
  static next = 4000;
  readonly pid = FakePty.next++;
  readonly writes: string[] = [];
  readonly sizes: Array<[number, number]> = [];
  killed = 0;
  private data: Array<(d: string) => void> = [];
  private exit: Array<(e: { exitCode: number; signal?: number }) => void> = [];
  readonly file: string;
  readonly args: string[];
  readonly options: PtySpawnOptions;
  constructor(file: string, args: string[], options: PtySpawnOptions) {
    this.file = file;
    this.args = args;
    this.options = options;
  }
  onData(listener: (d: string) => void) {
    this.data.push(listener);
    return { dispose() {} };
  }
  onExit(listener: (e: { exitCode: number; signal?: number }) => void) {
    this.exit.push(listener);
    return { dispose() {} };
  }
  write(data: string) {
    this.writes.push(data);
  }
  resize(cols: number, rows: number) {
    this.sizes.push([cols, rows]);
  }
  kill() {
    this.killed += 1;
  }
  emit(data: string) {
    for (const l of this.data) l(data);
  }
  end(exitCode: number, signal?: number) {
    for (const l of this.exit) l(signal === undefined ? { exitCode } : { exitCode, signal });
  }
}

const fakeKey = ["sk", "parent", "only"].join("-");
let h: PaneIpcHarness;
let timers: FakeTimers;
let ptys: FakePty[];
let host: PtyHost;
let available = true;
let folders: Array<string | undefined>;

beforeEach(() => {
  h = paneIpcHarness([11, 12]);
  timers = new FakeTimers();
  ptys = [];
  available = true;
  folders = [];
  let n = 0;
  host = new PtyHost({
    registry: h.registry,
    guard: {
      workingFolder: async (_owner, raw) => {
        folders.push(raw);
        return raw === "/home/me/ws" ? raw : "/home/me";
      },
    },
    loadSpawn: async () =>
      available
        ? (file, args, options) => {
            const pty = new FakePty(file, args, options);
            ptys.push(pty);
            return pty;
          }
        : null,
    shell: () => ({ command: "/bin/bash", args: ["-il"], name: "bash" }),
    env: { PATH: "/bin", OPENAI_API_KEY: fakeKey, LANG: "C.UTF-8" },
    platform: "linux",
    appVersion: "0.0.1",
    contents: (id) => h.contents(id),
    timers,
    newId: () => `t${++n}`,
    log: () => {},
  });
  installTerminalIpc(h.registry, host);
});

afterEach(() => host.disposeAll());

type Started = { terminalId: string; pid: number; shell: string; cwd: string };
const start = async (payload: Record<string, unknown> = { cols: 80, rows: 24 }, sender = 11) =>
  (await h.invoke("indus:terminal:start", payload, sender)) as Started;
const pty = (index = 0) => {
  const found = ptys[index];
  if (found === undefined) throw new Error("no pty");
  return found;
};

describe("PTY host", () => {
  it("spawned shell env lacks OPENAI_API_KEY set in the parent", async () => {
    const started = await start({ cols: 100, rows: 30, cwd: "/home/me/ws" });
    expect(started).toEqual({
      terminalId: "t1",
      pid: pty().pid,
      shell: "bash",
      cwd: "/home/me/ws",
    });
    expect(pty().options.env.OPENAI_API_KEY).toBeUndefined();
    expect(Object.values(pty().options.env)).not.toContain(fakeKey);
    expect(pty().options).toMatchObject({
      name: "xterm-256color",
      cols: 100,
      rows: 30,
      cwd: "/home/me/ws",
    });
    expect(pty().options.env.TERM).toBe("xterm-256color");
    expect([pty().file, pty().args]).toEqual(["/bin/bash", ["-il"]]);
  });

  it("a folder outside the roots starts in home", async () => {
    expect((await start({ cols: 80, rows: 24, cwd: "/etc" })).cwd).toBe("/home/me");
    expect(folders).toEqual(["/etc"]);
  });

  it("output is batched to the owning window only", async () => {
    const { terminalId } = await start();
    pty().emit("a");
    pty().emit("b");
    expect(h.window(11).events("indus:terminal:data")).toEqual([]);
    timers.advance(OUTPUT_BATCH_MS);
    pty().emit("c");
    timers.advance(OUTPUT_BATCH_MS);
    expect(h.window(11).events("indus:terminal:data")).toEqual([
      { terminalId, data: "ab" },
      { terminalId, data: "c" },
    ]);
    expect(h.window(12).events("indus:terminal:data")).toEqual([]);
  });

  it("write, resize and dispose only from the owning window", async () => {
    const { terminalId } = await start();
    h.send("indus:terminal:write", { terminalId, data: "ls\r" }, 12);
    h.send("indus:terminal:resize", { terminalId, cols: 10, rows: 10 }, 12);
    await h.invoke("indus:terminal:dispose", { terminalId }, 12);
    expect(pty().writes).toEqual([]);
    expect(pty().sizes).toEqual([]);
    expect(pty().killed).toBe(0);
    h.send("indus:terminal:write", { terminalId, data: "ls\r" }, 11);
    h.send("indus:terminal:resize", { terminalId, cols: 120, rows: 40 }, 11);
    expect(pty().writes).toEqual(["ls\r"]);
    expect(pty().sizes).toEqual([[120, 40]]);
  });

  it("hide/show keeps the same pty pid (nothing in main restarts it)", async () => {
    const first = await start();
    // the pane hides and shows: it only keeps writing to the same id
    h.send("indus:terminal:write", { terminalId: first.terminalId, data: "echo hi\r" });
    h.send("indus:terminal:resize", { terminalId: first.terminalId, cols: 81, rows: 24 });
    expect(ptys).toHaveLength(1);
    expect(host.size()).toBe(1);
    expect(pty().pid).toBe(first.pid);
  });

  it("dispose kills the pty; exit reports, flushes and releases", async () => {
    const one = await start();
    expect(await h.invoke("indus:terminal:dispose", { terminalId: one.terminalId })).toEqual({
      ok: true,
    });
    expect(pty(0).killed).toBe(1);
    expect(host.size()).toBe(0);
    const two = await start();
    pty(1).emit("bye\r\n");
    pty(1).end(0);
    expect(h.window(11).events("indus:terminal:data")).toEqual([
      { terminalId: two.terminalId, data: "bye\r\n" },
    ]);
    expect(h.window(11).events("indus:terminal:exit")).toEqual([
      { terminalId: two.terminalId, exitCode: 0, signal: null },
    ]);
    expect(pty(1).killed).toBe(1);
    // disposing an exited terminal is a no-op
    expect(await h.invoke("indus:terminal:dispose", { terminalId: two.terminalId })).toEqual({
      ok: true,
    });
    expect(pty(1).killed).toBe(1);
  });

  it("a closed window kills its terminals; quitting kills all", async () => {
    await start({ cols: 80, rows: 24 }, 11);
    await start({ cols: 80, rows: 24 }, 11);
    await start({ cols: 80, rows: 24 }, 12);
    h.window(11).destroy();
    expect(ptys.map((p) => p.killed)).toEqual([1, 1, 0]);
    host.disposeAll();
    expect(ptys.map((p) => p.killed)).toEqual([1, 1, 1]);
    expect(host.size()).toBe(0);
  });

  it("without node-pty: not-available with a message", async () => {
    available = false;
    const fresh = paneIpcHarness([11]);
    installTerminalIpc(
      fresh.registry,
      new PtyHost({
        registry: fresh.registry,
        guard: { workingFolder: async () => "/" },
        loadSpawn: async () => null,
        shell: () => ({ command: "/bin/sh", args: [], name: "sh" }),
        env: {},
        platform: "linux",
        appVersion: "0",
        contents: (id) => fresh.contents(id),
        log: () => {},
      }),
    );
    await expect(fresh.invoke("indus:terminal:start", { cols: 80, rows: 24 })).rejects.toThrow(
      /^not-available: terminals are not available/,
    );
  });
});

describe("shell choice", () => {
  const exists = (set: string[]) => (file: string) => set.includes(file);
  it("override, then $SHELL, then zsh/bash/sh; login flags for bash and zsh", () => {
    expect(
      chooseShell({
        platform: "linux",
        env: { SHELL: "/usr/bin/fish" },
        isExecutable: exists(["/usr/bin/fish"]),
        findOnPath: () => null,
      }),
    ).toEqual({ command: "/usr/bin/fish", args: ["-i"], name: "fish" });
    expect(
      chooseShell({
        platform: "linux",
        env: { INDUS_DESKTOP_SHELL: "zsh", SHELL: "/bin/bash" },
        isExecutable: () => false,
        findOnPath: (n) => (n === "zsh" ? "/usr/bin/zsh" : null),
      }),
    ).toEqual({ command: "/usr/bin/zsh", args: ["-il"], name: "zsh" });
    expect(
      chooseShell({
        platform: "darwin",
        env: { SHELL: "/missing" },
        isExecutable: exists(["/bin/bash"]),
        findOnPath: () => null,
      }),
    ).toEqual({ command: "/bin/bash", args: ["-il"], name: "bash" });
  });
  it("Windows: pwsh, Windows PowerShell, then COMSPEC; $SHELL ignored", () => {
    expect(
      chooseShell({
        platform: "win32",
        env: { SHELL: "/usr/bin/bash" },
        isExecutable: () => false,
        findOnPath: (n) => (n === "pwsh" ? "C:\\pwsh\\pwsh.exe" : null),
      }),
    ).toEqual({ command: "C:\\pwsh\\pwsh.exe", args: ["-NoLogo"], name: "pwsh" });
    expect(
      chooseShell({
        platform: "win32",
        env: { SystemRoot: "C:\\Windows", COMSPEC: "C:\\Windows\\cmd.exe" },
        isExecutable: () => false,
        findOnPath: () => null,
      }),
    ).toEqual({ command: "C:\\Windows\\cmd.exe", args: [], name: "cmd" });
  });
});

describe("a real PTY (when node-pty is installed)", () => {
  it("runs a shell, lacks the parent's key, and dies on dispose", async (context) => {
    const spawnPty = await loadNodePty();
    if (spawnPty === null || process.platform === "win32") {
      context.skip();
      return;
    }
    const real = paneIpcHarness([11]);
    const realHost = new PtyHost({
      registry: real.registry,
      guard: { workingFolder: async () => os.tmpdir() },
      loadSpawn: async () => spawnPty,
      shell: () => ({ command: "/bin/sh", args: [], name: "sh" }),
      env: { PATH: process.env.PATH, OPENAI_API_KEY: fakeKey },
      platform: process.platform,
      appVersion: "0",
      contents: (id) => real.contents(id),
      log: () => {},
    });
    installTerminalIpc(real.registry, realHost);
    const started = (await real.invoke("indus:terminal:start", { cols: 80, rows: 24 })) as Started;
    real.send("indus:terminal:write", {
      terminalId: started.terminalId,
      // biome-ignore lint/suspicious/noTemplateCurlyInString: shell syntax, not a template
      data: 'echo "[${OPENAI_API_KEY:-none}]"\n',
    });
    await new Promise((r) => setTimeout(r, 500));
    const out = real
      .window(11)
      .events("indus:terminal:data")
      .map((e) => (e as { data: string }).data)
      .join("");
    expect(out).toContain("[none]");
    await real.invoke("indus:terminal:dispose", { terminalId: started.terminalId });
    await new Promise((r) => setTimeout(r, 200));
    expect(() => process.kill(started.pid, 0)).toThrow();
  });
});
