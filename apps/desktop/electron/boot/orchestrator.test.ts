import { describe, expect, it, vi } from "vitest";
import type { SavedRemote } from "../backend/remote.ts";
import type { Resolution } from "../backend/resolver.ts";
import type { InstallOutcome } from "../installer/runner.ts";
import {
  BootError,
  BootOrchestrator,
  bootHooks,
  type Connection,
  FirstRunReset,
  MAX_SOFT_REPAIRS,
  type OrchestratorDeps,
  RemoteConnectError,
} from "./orchestrator.ts";
import type { BootProgress } from "./progress.ts";

const found: Extract<Resolution, { kind: "found" }> = {
  kind: "found",
  backend: { command: "/x/indus", args: [], label: "managed", rung: "managed", root: "/x" },
  version: "0.20.0",
};
const sentinel: Resolution = {
  kind: "bootstrap-needed",
  hints: { installDir: "/h/indus-agent", platform: "linux", packaged: true },
};

function connection(mode: "local" | "remote", profile: string | null, tag = "c"): Connection {
  return {
    descriptor: {
      mode,
      profile,
      profileId: `pid-${tag}`,
      instanceId: `inst-${tag}`,
      codeVersion: "0.20.0",
      wsUrl: "ws://127.0.0.1:1/ws",
    },
    rest: { origin: "http://127.0.0.1:2", token: "t" },
    ticket: async () => ({
      ticket: "x",
      wsUrl: "ws://127.0.0.1:1/ws",
      expiresInMs: 25_000,
      protocols: [],
    }),
  };
}

/** An attach the test settles by hand. */
class PendingAttach {
  private settle: { ok: (c: Connection) => void; fail: (e: unknown) => void } | null = null;
  readonly promise = new Promise<Connection>((ok, fail) => {
    this.settle = { ok, fail };
  });

  resolve(connection: Connection): void {
    this.settle?.ok(connection);
  }

  reject(error: unknown): void {
    this.settle?.fail(error);
  }
}

interface Rig {
  orchestrator: BootOrchestrator;
  deps: OrchestratorDeps;
  progress: BootProgress[];
  published: Array<Connection | null>;
  install: ReturnType<typeof vi.fn>;
  attach: ReturnType<typeof vi.fn>;
  remote: { saved: SavedRemote | null };
}

function rig(over: Partial<OrchestratorDeps> = {}): Rig {
  const progress: BootProgress[] = [];
  const published: Array<Connection | null> = [];
  const remote = { saved: null as SavedRemote | null };
  const install = vi.fn(async (): Promise<InstallOutcome> => ({ ok: true, marker: "/h/.m" }));
  const attach = vi.fn(async (_r: unknown, profile: string | null) => connection("local", profile));
  const deps: OrchestratorDeps = {
    resolve: async () => found,
    install,
    attachLocal: attach,
    savedRemote: () => remote.saved,
    connectRemote: async (_s, profile) => connection("remote", profile),
    waitForUpdate: async () => {},
    broadcast: (p) => progress.push(p),
    onConnection: (c) => published.push(c),
    sleep: async () => {},
    hooks: { fake: false, fakeError: null, stepMs: 0 },
    log: () => {},
    ...over,
  };
  return {
    orchestrator: new BootOrchestrator(deps),
    deps,
    progress,
    published,
    install,
    attach,
    remote,
  };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("boot orchestrator", () => {
  it("a local boot walks resolve → runtime → ready and publishes the connection", async () => {
    const r = rig();
    const c = await r.orchestrator.start(null);
    expect(c.descriptor.mode).toBe("local");
    expect(r.progress.map((p) => p.phase)).toEqual(["resolve", "runtime", "runtime", "ready"]);
    expect(r.orchestrator.progress()).toMatchObject({
      phase: "ready",
      progress: 94,
      running: true,
    });
    expect(r.published).toEqual([c]);
    // joining: a second start for the same profile returns the same connection
    expect(await r.orchestrator.start(null)).toBe(c);
    expect(r.attach).toHaveBeenCalledTimes(1);
  });

  it("concurrent starts for one profile share one attempt", async () => {
    const r = rig();
    const [a, b] = await Promise.all([r.orchestrator.start("p"), r.orchestrator.start("p")]);
    expect(a).toBe(b);
    expect(r.attach).toHaveBeenCalledTimes(1);
  });

  it("overlapping starts: the older finishing last publishes nothing and keeps the newer pin", async () => {
    const slow = new PendingAttach();
    const fast = new PendingAttach();
    const attach = vi.fn((_r: unknown, profile: string | null) =>
      profile === "old" ? slow.promise : fast.promise,
    );
    const r = rig({ attachLocal: attach as unknown as OrchestratorDeps["attachLocal"] });
    const older = r.orchestrator.start("old");
    await flush();
    const newer = r.orchestrator.start("new");
    await flush();
    expect(r.orchestrator.pinnedProfile).toBe("new");
    fast.resolve(connection("local", "new", "new"));
    const won = await newer;
    expect(won.descriptor.profile).toBe("new");
    slow.resolve(connection("local", "old", "old"));
    // the older caller follows the attempt that replaced it
    expect((await older).descriptor.profile).toBe("new");
    expect(r.published.map((c) => c?.descriptor.profile)).toEqual(["new"]);
    expect(r.orchestrator.current()?.descriptor.profile).toBe("new");
  });

  it("an older attempt failing late does not clear the newer pin or publish an error", async () => {
    const slow = new PendingAttach();
    const fast = new PendingAttach();
    const r = rig({
      attachLocal: ((_r: unknown, profile: string | null) =>
        profile === "old" ? slow.promise : fast.promise) as OrchestratorDeps["attachLocal"],
    });
    const older = r.orchestrator.start("old").catch((e: unknown) => e);
    await flush();
    const newer = r.orchestrator.start("new");
    await flush();
    slow.reject(new Error("old attach failed"));
    await flush();
    expect(r.orchestrator.pinnedProfile).toBe("new");
    expect(r.progress.some((p) => p.phase === "error")).toBe(false);
    fast.resolve(connection("local", "new"));
    await newer;
    expect((await older) as Connection).toMatchObject({ descriptor: { profile: "new" } });
  });

  it("a local failure latches: a retried start rethrows silently without running anything", async () => {
    const r = rig({
      attachLocal: vi.fn(async () => {
        throw new Error("gateway ensure failed (bad_config)");
      }),
    });
    await expect(r.orchestrator.start(null)).rejects.toThrow("bad_config");
    expect(r.orchestrator.progress()).toMatchObject({
      phase: "error",
      running: false,
      retryable: false,
      latched: true,
    });
    const count = r.progress.length;
    await expect(r.orchestrator.start(null)).rejects.toThrow("bad_config");
    expect(r.progress.length).toBe(count); // silent
    expect(r.deps.attachLocal).toHaveBeenCalledTimes(1);
  });

  it("an installer failure: Retry never re-runs the installer (spy) until Repair", async () => {
    let resolutions = 0;
    const r = rig({
      resolve: async () => {
        resolutions += 1;
        return resolutions <= 2 ? sentinel : found;
      },
    });
    r.install.mockResolvedValueOnce({
      ok: false,
      stage: "runtime",
      error: "uv failed",
      cancelled: false,
    });
    const first = r.orchestrator.start(null);
    await flush();
    expect(r.orchestrator.progress().phase).toBe("first-run");
    expect(r.orchestrator.choose("install-local")).toEqual({ accepted: true });
    await expect(first).rejects.toThrow("the installation failed: uv failed");
    expect(r.install).toHaveBeenCalledTimes(1);

    // Retry from the renderer: the installer latch holds; nothing re-runs
    expect(r.orchestrator.retry("retry")).toEqual({ accepted: true });
    await flush();
    await flush();
    expect(r.install).toHaveBeenCalledTimes(1);
    expect(r.orchestrator.progress().phase).toBe("error");
    await expect(r.orchestrator.start(null)).rejects.toThrow("uv failed");
    expect(r.install).toHaveBeenCalledTimes(1);

    // Repair: the installer runs again (no chooser this time) and the boot completes
    expect(r.orchestrator.retry("repair")).toEqual({ accepted: true });
    await vi.waitFor(() => expect(r.orchestrator.progress().phase).toBe("ready"));
    expect(r.install).toHaveBeenCalledTimes(2);
  });

  it("repair asks the resolver to skip installed runtimes, and is capped at 3 per process", async () => {
    const repairs: boolean[] = [];
    const r = rig({
      resolve: async ({ repair }) => {
        repairs.push(repair);
        return repair ? sentinel : found;
      },
    });
    await r.orchestrator.start(null);
    for (let i = 0; i < MAX_SOFT_REPAIRS; i += 1) {
      r.install.mockResolvedValueOnce({ ok: false, error: "broken", cancelled: false });
      expect(r.orchestrator.retry("repair")).toEqual({ accepted: true });
      await vi.waitFor(() => expect(r.orchestrator.progress().phase).toBe("error"));
    }
    expect(r.orchestrator.retry("repair")).toEqual({ accepted: false });
    expect(r.install).toHaveBeenCalledTimes(MAX_SOFT_REPAIRS);
    expect(repairs.filter(Boolean)).toHaveLength(MAX_SOFT_REPAIRS);
  });

  it("reauth is latched separately; a transient remote failure is retryable and not latched", async () => {
    const r = rig();
    r.remote.saved = { url: "https://agent.example.test", token: "t" };
    let mode: "reauth" | "down" | "ok" = "down";
    r.deps.connectRemote = async (_s, profile) => {
      if (mode === "reauth")
        throw new RemoteConnectError("the server refused the token", {
          reauth: true,
          statusCode: 401,
        });
      if (mode === "down")
        throw new RemoteConnectError("the server did not answer", { statusCode: 503 });
      return connection("remote", profile);
    };
    await expect(r.orchestrator.start(null)).rejects.toThrow("did not answer");
    expect(r.orchestrator.progress()).toMatchObject({
      phase: "error",
      retryable: true,
      statusCode: 503,
    });
    expect(r.orchestrator.progress().latched).toBeUndefined();
    mode = "reauth";
    await expect(r.orchestrator.start(null)).rejects.toThrow("refused");
    expect(r.orchestrator.progress()).toMatchObject({ retryable: false, statusCode: 401 });
    mode = "ok";
    // latched: retry does not clear a reauth rejection
    r.orchestrator.retry("retry");
    await flush();
    await expect(r.orchestrator.start(null)).rejects.toThrow("refused");
    // saving the remote again clears it
    r.orchestrator.applyConnectionChange();
    await vi.waitFor(() => expect(r.orchestrator.progress().phase).toBe("ready"));
    // a failing remote never started a local daemon
    expect(r.attach).not.toHaveBeenCalled();
  });

  it("progress never decreases except after an error", async () => {
    const r = rig();
    await r.orchestrator.start(null);
    const seen = r.progress.map((p) => p.progress);
    for (let i = 1; i < seen.length; i += 1)
      expect(seen[i]).toBeGreaterThanOrEqual(seen[i - 1] as number);
    const failing = rig({
      resolve: async () => {
        throw new Error("boom");
      },
    });
    await failing.orchestrator.start(null).catch(() => {});
    failing.deps.resolve = async () => found;
    failing.orchestrator.retry("retry");
    await vi.waitFor(() => expect(failing.orchestrator.progress().phase).toBe("ready"));
    const after = failing.progress.map((p) => `${p.phase}:${p.progress}`);
    expect(after).toEqual([
      "resolve:8",
      "runtime:28",
      "error:28",
      "resolve:8",
      "runtime:28",
      "runtime:28",
      "ready:94",
    ]);
  });

  it("first-run gate: sentinel and no remote → choice; connect-remote needs a saved remote; reset brings the chooser back", async () => {
    const r = rig({ resolve: async () => sentinel });
    const first = r.orchestrator.start(null).catch((e: unknown) => e);
    await flush();
    expect(r.orchestrator.progress()).toMatchObject({ phase: "first-run", running: false });
    expect(r.orchestrator.choose("connect-remote")).toEqual({ accepted: false });
    expect(r.orchestrator.choose("reset")).toEqual({ accepted: true });
    expect(await first).toBeInstanceOf(FirstRunReset);
    await vi.waitFor(() => expect(r.orchestrator.waitingForChoice()).toBe(true));
    r.remote.saved = { url: "https://agent.example.test", token: "t" };
    expect(r.orchestrator.choose("connect-remote")).toEqual({ accepted: true });
    await vi.waitFor(() => expect(r.orchestrator.current()?.descriptor.mode).toBe("remote"));
    expect(r.install).not.toHaveBeenCalled();
    expect(r.orchestrator.choose("install-local")).toEqual({ accepted: false });
  });

  it("boot fake hooks: steps wait, a fake error fails at resolve and latches", async () => {
    expect(bootHooks({})).toEqual({ fake: false, fakeError: null, stepMs: 650 });
    expect(
      bootHooks({
        INDUS_DESKTOP_BOOT_FAKE: "1",
        INDUS_DESKTOP_BOOT_FAKE_STEP_MS: "5",
        INDUS_DESKTOP_BOOT_FAKE_ERROR: "simulated",
      }),
    ).toEqual({ fake: true, fakeError: "simulated", stepMs: 5 });
    const sleeps: number[] = [];
    const r = rig({
      hooks: { fake: true, fakeError: null, stepMs: 5 },
      sleep: async (ms) => {
        sleeps.push(ms);
      },
    });
    await r.orchestrator.start(null);
    expect(sleeps).toEqual([5, 5, 5]);
    const failing = rig({ hooks: { fake: false, fakeError: "simulated", stepMs: 0 } });
    const error = await failing.orchestrator.start(null).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(BootError);
    expect(failing.orchestrator.progress()).toMatchObject({
      phase: "error",
      error: "simulated",
      latched: true,
    });
    expect(failing.attach).not.toHaveBeenCalled();
  });

  it("reconnect after a dead remote route drops the connection and boots again", async () => {
    const r = rig();
    r.remote.saved = { url: "https://agent.example.test", token: "t" };
    await r.orchestrator.start(null);
    r.orchestrator.reconnect();
    expect(r.published.at(-1)).toBeNull();
    await vi.waitFor(() => expect(r.orchestrator.current()).not.toBeNull());
    expect(r.published.filter((c) => c !== null)).toHaveLength(2);
  });
});
