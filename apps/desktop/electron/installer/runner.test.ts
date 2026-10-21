import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, describe, expect, it, vi } from "vitest";
import { cleanupFakes, posix, tempDir } from "../../testing/fakeIndus.ts";
import { FakeTimers } from "../../testing/fakeTimers.ts";
import {
  type InstallerEvent,
  InstallerRunner,
  LOG_RING_LINES,
  MARKER_FILE,
  PIPE_DRAIN_GRACE_MS,
  type RunnerDeps,
  resolveInstallerScript,
} from "./runner.ts";
import {
  cleanLine,
  decodeConsoleBytes,
  LineSplitter,
  parseManifest,
  parseStageFrame,
} from "./stageProtocol.ts";

const FAKE = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "testing",
  "fakeInstaller.mjs",
);
const holders: number[] = [];

afterAll(() => {
  for (const pid of holders) {
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      // gone
    }
  }
  cleanupFakes();
});

interface Harness {
  runner: InstallerRunner;
  events: InstallerEvent[];
  home: string;
  state: string;
  runs: () => string[][];
  fetches: () => number;
}

function harness(
  stages: object[] | null,
  over: Partial<RunnerDeps> = {},
  extra: Record<string, unknown> = {},
): Harness {
  const base = tempDir();
  const home = path.join(base, "home");
  const state = path.join(base, "state");
  const events: InstallerEvent[] = [];
  let fetches = 0;
  const scenario = stages === null ? "" : JSON.stringify({ stages, ...extra });
  const runner = new InstallerRunner({
    home,
    appVersion: "0.20.0",
    env: {
      ...process.env,
      INDUS_FAKE_INSTALLER_SCENARIO: scenario,
      INDUS_FAKE_INSTALLER_STATE: state,
    },
    fetchScript: async () => {
      fetches += 1;
      return { path: FAKE, kind: "node", source: "local" };
    },
    emit: (event) => events.push(event),
    log: () => {},
    ...over,
  });
  const runs = () => {
    try {
      return fs
        .readFileSync(path.join(state, "runs.jsonl"), "utf8")
        .trim()
        .split("\n")
        .map((line) => JSON.parse(line) as string[]);
    } catch {
      return [];
    }
  };
  return { runner, events, home, state, runs, fetches: () => fetches };
}

const stageEvents = (events: InstallerEvent[]) =>
  events
    .filter((e): e is Extract<InstallerEvent, { type: "stage" }> => e.type === "stage")
    .map((e) => `${e.name}:${e.state}`);

describe.skipIf(!posix)("installer runner (fake installer)", () => {
  it("a full run: manifest, every stage, marker with schema 1, complete", async () => {
    const h = harness(null);
    const outcome = await h.runner.run();
    expect(outcome.ok).toBe(true);
    expect(h.events[0]).toEqual({ type: "manifest", stages: [] });
    const manifest = h.events.filter((e) => e.type === "manifest").at(-1);
    expect(manifest).toMatchObject({
      type: "manifest",
      stages: [
        { name: "prerequisites", title: "Check prerequisites", needsUserInput: false },
        { name: "runtime" },
        { name: "config" },
        { name: "setup", needsUserInput: true },
      ],
    });
    expect(stageEvents(h.events)).toEqual([
      "prerequisites:running",
      "prerequisites:succeeded",
      "runtime:running",
      "runtime:succeeded",
      "config:running",
      "config:succeeded",
      "setup:running",
      "setup:skipped",
    ]);
    const marker = path.join(h.home, MARKER_FILE);
    expect(h.events.at(-1)).toEqual({ type: "complete", marker });
    const body = JSON.parse(fs.readFileSync(marker, "utf8")) as Record<string, unknown>;
    expect(body).toMatchObject({ schema: 1, app_version: "0.20.0" });
    expect(fs.statSync(marker).mode & 0o777).toBe(0o600);
    // stage log lines arrive tagged with their stage
    expect(h.events).toContainEqual({
      type: "log",
      stage: "runtime",
      line: "runtime: step 1",
      stream: "stdout",
    });
    // the stage runs carry the protocol flags
    expect(h.runs()[1]).toEqual([
      "--stage",
      "prerequisites",
      "--json",
      "--non-interactive",
      "--indus-home",
      h.home,
    ]);
  });

  it("host death without a result frame is retried 3 times, then the run fails", async () => {
    const h = harness([{ name: "runtime", mode: "die" }]);
    const outcome = await h.runner.run();
    expect(outcome).toMatchObject({ ok: false, stage: "runtime", cancelled: false });
    expect(h.runs().filter((argv) => argv.includes("--stage"))).toHaveLength(3);
    expect(stageEvents(h.events)).toEqual(["runtime:running", "runtime:failed"]);
    expect(h.events.at(-1)).toMatchObject({ type: "failed", stage: "runtime" });
    expect(fs.existsSync(path.join(h.home, MARKER_FILE))).toBe(false);
  });

  it("a host that dies once is retried and the run goes on", async () => {
    const h = harness([{ name: "runtime", mode: "die-once" }, { name: "config" }]);
    expect((await h.runner.run()).ok).toBe(true);
    expect(h.runs().filter((argv) => argv.includes("runtime"))).toHaveLength(2);
    expect(h.events.some((e) => e.type === "log" && e.line.includes("retrying (2/3)"))).toBe(true);
  });

  it("a frame saying ok:false is final (not retried)", async () => {
    const h = harness([{ name: "runtime", mode: "fail" }]);
    const outcome = await h.runner.run();
    expect(outcome).toMatchObject({ ok: false, stage: "runtime", error: "scripted failure" });
    expect(h.runs().filter((argv) => argv.includes("--stage"))).toHaveLength(1);
  });

  it("the last JSON line wins over an earlier frame-shaped line", async () => {
    const h = harness([{ name: "runtime", mode: "json-noise" }]);
    expect((await h.runner.run()).ok).toBe(true);
    expect(stageEvents(h.events)).toEqual(["runtime:running", "runtime:succeeded"]);
  });

  it("console decoding: CP1252 bytes, CRLF, colour escapes and progress redraws", async () => {
    const h = harness([
      { name: "legacy", mode: "cp1252" },
      { name: "pretty", mode: "crlf-ansi" },
    ]);
    expect((await h.runner.run()).ok).toBe(true);
    const lines = h.events.filter((e) => e.type === "log").map((e) => e.line);
    expect(lines).toContain("“quoted” café");
    expect(lines).toContain("green text");
    expect(lines).toContain("progress 100%");
    expect(lines.some((line) => line.includes("\r") || line.includes("\u001b"))).toBe(false);
  });

  it("a grandchild holding stdout: the run finishes once the 20 s grace runs out (fake clock)", async () => {
    const timers = new FakeTimers();
    const h = harness([{ name: "products", mode: "hold" }], { timers });
    const done = h.runner.run();
    // wait until the host exited and only the grace timer is left
    await vi.waitFor(
      () => {
        expect(fs.existsSync(path.join(h.state, "holder.pid"))).toBe(true);
        expect(timers.pending()).toBe(1);
      },
      { timeout: 10_000, interval: 25 },
    );
    holders.push(Number(fs.readFileSync(path.join(h.state, "holder.pid"), "utf8")));
    timers.advance(PIPE_DRAIN_GRACE_MS - 1);
    let settled = false;
    void done.then(() => {
      settled = true;
    });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(settled).toBe(false);
    timers.advance(1);
    const outcome = await done;
    expect(outcome.ok).toBe(true);
    expect(
      h.events.some((e) => e.type === "log" && e.line.includes("still holds its output")),
    ).toBe(true);
  });

  it("cancel kills the running host and fails the run as cancelled", async () => {
    const h = harness([{ name: "runtime", mode: "slow" }]);
    const done = h.runner.run();
    await vi.waitFor(
      () =>
        expect(h.events.some((e) => e.type === "log" && e.line === "working slowly")).toBe(true),
      { timeout: 10_000, interval: 25 },
    );
    expect(h.runner.cancel()).toBe(true);
    const outcome = await done;
    expect(outcome).toMatchObject({ ok: false, cancelled: true, error: "cancelled" });
    expect(h.events.at(-1)).toEqual({ type: "failed", stage: "runtime", error: "cancelled" });
    expect(h.runner.running()).toBe(false);
  });

  it("a manifest that fails or speaks another protocol runs no stage", async () => {
    const failing = harness([{ name: "runtime" }], {}, { manifestExit: 3 });
    expect(await failing.runner.run()).toMatchObject({ ok: false, cancelled: false });
    expect(failing.runs()).toHaveLength(1);
    const wrong = harness([{ name: "runtime" }], {}, { protocolVersion: 2 });
    const outcome = await wrong.runner.run();
    expect(outcome).toMatchObject({ ok: false });
    expect(outcome.ok === false && outcome.error).toContain("protocol 2");
    expect(wrong.runs()).toHaveLength(1);
  });

  it("the script is fetched fresh on every run; stage hosts are claimed and released", async () => {
    const claimed: number[] = [];
    const released: number[] = [];
    const h = harness([{ name: "runtime" }], {
      claim: (pid) => claimed.push(pid),
      release: (pid) => released.push(pid),
    });
    await h.runner.run();
    await h.runner.run();
    expect(h.fetches()).toBe(2);
    expect(claimed).toHaveLength(4);
    expect(released.sort()).toEqual(claimed.sort());
  });

  it("the log ring keeps the last 500 lines", async () => {
    const h = harness([{ name: "runtime" }]);
    await h.runner.run();
    const ring = h.runner.recentLog();
    expect(ring.length).toBeLessThanOrEqual(LOG_RING_LINES);
    expect(ring).toContain("runtime: step 2");
  });
});

describe("installer script source", () => {
  it("downloads this release's script fresh into the bootstrap cache", async () => {
    const home = tempDir();
    const urls: string[] = [];
    const download = async (url: string, dest: string) => {
      urls.push(url);
      fs.writeFileSync(dest, `# ${urls.length}\n`);
    };
    const dest = path.join(home, "bootstrap-cache", "install.sh");
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.writeFileSync(dest, "# stale copy\n");
    const script = await resolveInstallerScript({
      env: {},
      home,
      appVersion: "0.20.0",
      platform: "linux",
      download,
    });
    expect(script).toEqual({ path: dest, kind: "posix", source: "download" });
    expect(urls).toEqual([
      "https://github.com/indusagi/indus-agent/releases/download/v0.20.0/install.sh",
    ]);
    expect(fs.readFileSync(dest, "utf8")).toBe("# 1\n");
    const win = await resolveInstallerScript({
      env: { INDUS_DESKTOP_RELEASE_BASE: "https://example.test/rel/" },
      home,
      appVersion: "1.0.0",
      platform: "win32",
      download,
    });
    expect(win.kind).toBe("powershell");
    expect(urls.at(-1)).toBe("https://example.test/rel/v1.0.0/install.ps1");
  });

  it("a dev override is used as is; a dev build without a release refuses to download", async () => {
    const home = tempDir();
    const local = await resolveInstallerScript({
      env: { INDUS_DESKTOP_INSTALL_SCRIPT: FAKE },
      home,
      appVersion: "0.0.0-dev",
      platform: "linux",
    });
    expect(local).toEqual({ path: FAKE, kind: "node", source: "local" });
    await expect(
      resolveInstallerScript({ env: {}, home, appVersion: "dev", platform: "linux" }),
    ).rejects.toThrow(/no release/);
    await expect(
      resolveInstallerScript({
        env: { INDUS_DESKTOP_RELEASE_BASE: "http://plain.test" },
        home,
        appVersion: "1.0.0",
        platform: "linux",
        download: async () => {},
      }),
    ).rejects.toThrow(/https/);
  });
});

describe("stage protocol", () => {
  it("manifest: last manifest line, protocol 1, stage names checked", () => {
    const manifest = parseManifest(
      [
        "banner",
        '{"note": 1}',
        '{"protocol_version": 1, "stages": [{"name": "old", "title": "Old"}]}',
        '{"protocol_version": 1, "stages": [{"name": "runtime", "title": "Runtime", "category": "rt", "needs_user_input": false}]}',
      ].join("\n"),
    );
    expect(manifest).toEqual({
      stages: [{ name: "runtime", title: "Runtime", category: "rt", needsUserInput: false }],
    });
    expect(parseManifest("nothing")).toEqual({ error: "the installer printed no manifest" });
    expect(parseManifest('{"protocol_version": 1, "stages": [{"name": "../x"}]}')).toMatchObject({
      error: expect.stringContaining("cannot run"),
    });
  });

  it("stage frame: the last frame-shaped line", () => {
    expect(
      parseStageFrame(
        '{"ok": false, "stage": "a"}\nlog\n{"ok": true, "stage": "a", "skipped": true, "reason": "r"}\n',
      ),
    ).toEqual({ ok: true, stage: "a", skipped: true, reason: "r" });
    expect(parseStageFrame('{"ok": "yes", "stage": "a"}\n')).toBeNull();
    expect(parseStageFrame("")).toBeNull();
  });

  it("decoding and line splitting", () => {
    expect(decodeConsoleBytes(Buffer.from("café", "utf8"))).toBe("café");
    expect(decodeConsoleBytes(Buffer.from([0x80, 0x41, 0xe9]))).toBe("€Aé");
    const splitter = new LineSplitter();
    expect(splitter.push(Buffer.from("one\r\ntw"))).toEqual(["one"]);
    expect(splitter.push(Buffer.from("o\nthree"))).toEqual(["two"]);
    expect(splitter.flush()).toEqual(["three"]);
    expect(cleanLine("\u001b[1;31mred\u001b[0m")).toBe("red");
    expect(cleanLine("a\rb\rc")).toBe("c");
    expect(cleanLine("\u001b]0;title\u0007text")).toBe("text");
  });
});
