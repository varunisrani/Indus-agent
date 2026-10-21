import path from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { cleanupFakes, posix, tempDir, writeFakeIndus } from "../../testing/fakeIndus.ts";
import {
  AttachError,
  ENSURE_CLIENT_GRACE_MS,
  LocalAttach,
  type LocalAttachDeps,
  newSessionToken,
  profileHome,
  SESSION_TOKEN_HEADER,
} from "./localAttach.ts";
import { type CommandRunner, type RunResult, runCommand } from "./process.ts";
import type { IndusCommand } from "./resolver.ts";

afterAll(cleanupFakes);

interface Setup {
  home: string;
  backend: IndusCommand;
  calls: string[][];
  run: CommandRunner;
  attach: (over?: Partial<LocalAttachDeps>, token?: string) => LocalAttach;
}

function setup(scenario = "healthy"): Setup {
  const base = tempDir();
  const home = path.join(base, "h");
  const program = writeFakeIndus(path.join(base, "bin", "indus"));
  const backend: IndusCommand = {
    command: program,
    args: [],
    label: "fake",
    rung: "command",
    root: null,
  };
  const calls: string[][] = [];
  const run: CommandRunner = (spec, args, options) => {
    calls.push([...args]);
    return runCommand(spec, args, options);
  };
  const attach = (over: Partial<LocalAttachDeps> = {}, token?: string) =>
    new LocalAttach(
      {
        backend,
        version: "0.20.0",
        profile: null,
        home,
        env: { ...process.env, INDUS_FAKE_SCENARIO: scenario },
        run,
        log: () => {},
        ...over,
      },
      token,
    );
  return { home, backend, calls, run, attach };
}

const verbs = (calls: string[][], verb: string) =>
  calls.filter((args) => args.join(" ").includes(verb)).length;

describe.skipIf(!posix)("local attach (fake backend)", () => {
  it("gateway ensure → a loopback descriptor; the first attach knows it started the daemon", async () => {
    const s = setup();
    const first = s.attach();
    const descriptor = await first.attachGateway();
    expect(descriptor.wsUrl).toMatch(/^ws:\/\/127\.0\.0\.1:\d+\/ws$/);
    expect(descriptor.codeVersion).toBe("0.20.0");
    expect(first.startedByApp).toBe(true);
    const second = s.attach();
    expect(await second.attachGateway()).toEqual(descriptor);
    expect(second.startedByApp).toBe(false);
    expect(s.calls[0]).toEqual(["gateway", "ensure", "--json", "--timeout", "60"]);
  });

  it("a named profile runs with -p and its own home", async () => {
    const s = setup();
    const attach = s.attach({ profile: "work" });
    await attach.attachGateway();
    expect(s.calls[0]?.slice(0, 2)).toEqual(["-p", "work"]);
    expect(attach.home()).toBe(path.join(s.home, "profiles", "work"));
    expect(profileHome(s.home, "default")).toBe(s.home);
  });

  it("serve ensure: the token goes in the environment only and opens the REST API", async () => {
    const s = setup();
    const seen: Array<{ args: string[]; token: string | undefined }> = [];
    const attach = s.attach({
      run: (spec, args, options) => {
        seen.push({ args: [...args], token: options.env?.INDUS_DASHBOARD_SESSION_TOKEN });
        return runCommand(spec, args, options);
      },
    });
    const serve = await attach.ensureServe();
    expect(serve.url).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
    for (const call of seen) expect(call.args.join(" ")).not.toContain(attach.sessionToken);
    expect(seen.find((c) => c.args.includes("serve"))?.token).toBe(attach.sessionToken);
    const ok = await fetch(`${serve.url}/api/sessions`, {
      headers: { [SESSION_TOKEN_HEADER]: attach.sessionToken },
    });
    expect(ok.status).toBe(200);
    // the gateway ensure never sees the token
    await attach.attachGateway();
    expect(seen.find((c) => c.args.includes("gateway"))?.token).toBeUndefined();
  });

  it("a serve from an earlier launch holding another token is replaced once", async () => {
    const s = setup();
    const killed: number[] = [];
    let ensures = 0;
    const stale: RunResult = {
      code: 0,
      signal: null,
      stdout:
        '{"url": "http://127.0.0.1:9", "port": 9, "pid": 424242, "profile_id": "p", "code_version": "0.20.0"}\n',
      stderr: "",
      timedOut: false,
      spawnError: null,
    };
    const attach = s.attach({
      run: async (spec, args, options) => {
        if (!args.includes("serve")) return runCommand(spec, args, options);
        ensures += 1;
        return ensures === 1 ? stale : runCommand(spec, args, options);
      },
      fetch: (async (url: string | URL | Request, init?: RequestInit) =>
        String(url).startsWith("http://127.0.0.1:9/")
          ? new Response("{}", { status: 401 })
          : fetch(url, init)) as typeof fetch,
      terminate: (pid) => killed.push(pid),
    });
    const serve = await attach.ensureServe();
    expect(killed).toEqual([424242]);
    expect(ensures).toBe(2);
    expect(serve.pid).not.toBe(424242);
  });

  it("ensure failure names the verdict's reason", async () => {
    const s = setup("ensure-fails");
    const error = await s
      .attach()
      .attachGateway()
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(AttachError);
    expect((error as AttachError).reason).toBe("bad_config");
    expect((error as Error).message).toContain("the daemon exited with code 78");
  });

  it("a CLI that prints no JSON is diagnosed with its stderr", async () => {
    const s = setup();
    const attach = s.attach({
      run: async () => ({
        code: 2,
        signal: null,
        stdout: "",
        stderr: "Usage: indus\nError: No such command 'ensure'.\n",
        timedOut: false,
        spawnError: null,
      }),
    });
    await expect(attach.attachGateway()).rejects.toThrow(
      "gateway ensure printed no result (exit 2): Error: No such command 'ensure'.",
    );
  });

  it("the ensure deadline is 60 s plus the 15 s client grace", async () => {
    const s = setup();
    const deadlines: number[] = [];
    const attach = s.attach({
      run: (spec, args, options) => {
        deadlines.push(options.timeoutMs);
        return runCommand(spec, args, options);
      },
    });
    await attach.attachGateway();
    expect(deadlines[0]).toBe(60_000 + ENSURE_CLIENT_GRACE_MS);
    const timedOut = s.attach({
      run: async () => ({
        code: null,
        signal: "SIGTERM",
        stdout: "",
        stderr: "",
        timedOut: true,
        spawnError: null,
      }),
    });
    await expect(timedOut.attachGateway()).rejects.toMatchObject({ reason: "timeout" });
  });

  it("stale-socket on a ticket mint: one restart, then the mint succeeds", async () => {
    const s = setup("stale-socket");
    const attach = s.attach();
    await attach.attachGateway();
    const minted = await attach.mint();
    expect(minted.ok).toBe(true);
    expect(verbs(s.calls, "gateway restart")).toBe(1);
    expect(verbs(s.calls, "gateway ensure")).toBe(2);
  });

  it("version skew: the stale daemon is restarted once, then the descriptor matches", async () => {
    const s = setup("version-skew");
    const attach = s.attach();
    const descriptor = await attach.attachGateway();
    expect(descriptor.codeVersion).toBe("0.20.0");
    expect(verbs(s.calls, "gateway restart")).toBe(1);
    // a later attach in the same launch does not restart again
    await attach.attachGateway();
    expect(verbs(s.calls, "gateway restart")).toBe(1);
  });

  it("a gone daemon is re-ensured on the next mint (not stopped)", async () => {
    const s = setup();
    const attach = s.attach();
    const before = await attach.attachGateway();
    await runCommand(s.backend, ["gateway", "stop"], {
      env: { ...process.env, INDUS_HOME: s.home },
      timeoutMs: 10_000,
    });
    const minted = await attach.mint();
    expect(minted.ok).toBe(true);
    expect(minted.ok && minted.instanceId).not.toBe(before.instanceId);
    expect(verbs(s.calls, "gateway restart")).toBe(0);
  });

  it("session tokens are 43 URL-safe characters and differ", () => {
    const a = newSessionToken();
    expect(a).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(newSessionToken()).not.toBe(a);
  });
});
