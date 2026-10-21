import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, describe, expect, it } from "vitest";
import {
  baseOptions,
  CONTROL_PERSIST_S,
  classifySshError,
  controlPath,
  KEEPALIVE_MS,
  LISTENER_SUFFIX_BYTES,
  rewritePreviewUrl,
  type SpawnLike,
  SSH_CONNECT_TIMEOUT_MS,
  SSH_EXEC_TIMEOUT_MS,
  SSH_FORWARD_TIMEOUT_MS,
  SSH_PROBE_WATCHDOG_S,
  SshConnection,
  type SshDeps,
  SshError,
  SUN_PATH_MAX,
  TUNNEL_RESTART_DELAY_MS,
  TUNNEL_RESTART_LIMIT,
  validateTarget,
  withWatchdog,
} from "./ssh.ts";

const FAKE_SSH = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../../tests/fakes/ssh_bin/ssh",
);
const posix = process.platform !== "win32";
const dirs: string[] = [];
afterAll(() => {
  for (const dir of dirs) fs.rmSync(dir, { recursive: true, force: true });
});

function temp(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "indus-ssh-"));
  dirs.push(dir);
  return dir;
}

/** The fake `ssh` through `/bin/sh`, so the test does not depend on the file's exec bit. */
const viaSh: SpawnLike = (_command, args, options) =>
  spawn("/bin/sh", [FAKE_SSH, ...args], options);

function rig(env: Record<string, string> = {}, extra: Partial<SshDeps> = {}) {
  const base = temp();
  const log = path.join(base, "calls.log");
  const lines: string[] = [];
  const deps: SshDeps = {
    spawn: viaSh,
    env: { ...process.env, INDUS_FAKE_SSH_LOG: log, HOME: base, ...env },
    controlDir: path.join(base, "ctl"),
    fallbackDir: path.join(base, "f"),
    log: (line) => lines.push(line),
    ...extra,
  };
  const calls = () =>
    fs.existsSync(log)
      ? fs
          .readFileSync(log, "utf8")
          .split("\n")
          .filter((l) => l !== "")
          .map((l) => l.split("\x1f").filter((a) => a !== ""))
      : [];
  return { deps, calls, lines, base };
}

describe("arguments and limits", () => {
  it("constants match the row", () => {
    expect([SSH_CONNECT_TIMEOUT_MS, SSH_EXEC_TIMEOUT_MS, SSH_FORWARD_TIMEOUT_MS]).toEqual([
      15_000, 20_000, 15_000,
    ]);
    expect(SSH_PROBE_WATCHDOG_S).toBe(15);
    expect([TUNNEL_RESTART_LIMIT, TUNNEL_RESTART_DELAY_MS]).toEqual([5, 1_000]);
    expect(CONTROL_PERSIST_S).toBe(300);
    expect(KEEPALIVE_MS).toBe(60_000);
  });

  it("every call is BatchMode with trust-on-first-use", () => {
    const options = baseOptions("/tmp/x.sock");
    const pairs = options.filter((_, i) => options[i - 1] === "-o");
    expect(pairs).toEqual(
      expect.arrayContaining([
        "ControlPath=/tmp/x.sock",
        "ControlMaster=auto",
        "ControlPersist=300",
        "BatchMode=yes",
        "StrictHostKeyChecking=accept-new",
        "ConnectTimeout=15",
      ]),
    );
    expect(pairs.some((p) => p.startsWith("StrictHostKeyChecking=no"))).toBe(false);
    expect(baseOptions(null).some((o) => o.startsWith("ControlPath"))).toBe(false);
  });

  it("control path length stays within 104 bytes", () => {
    const target = { host: "build.example.internal", user: "developer", port: 2222 };
    const deep = path.join("/home", "someone", "x".repeat(120), "Indus", "ssh");
    const chosen = controlPath(target, deep, "/tmp/indus-ssh-1000");
    expect(chosen.startsWith("/tmp/indus-ssh-1000/")).toBe(true);
    expect(Buffer.byteLength(chosen) + LISTENER_SUFFIX_BYTES).toBeLessThanOrEqual(SUN_PATH_MAX);
    const short = controlPath(target, "/u/ssh", "/tmp/f");
    expect(short).toMatch(/^\/u\/ssh\/[0-9a-f]{16}\.sock$/);
    expect(controlPath(target, "/u/ssh", "/tmp/f")).toBe(short); // stable per target
    expect(controlPath({ ...target, port: 22 }, "/u/ssh", "/tmp/f")).not.toBe(short);
    expect(() => controlPath(target, deep, deep)).toThrow(SshError);
  });

  it("refuses targets that could be read as options", () => {
    for (const target of [
      { host: "-oProxyCommand=evil" },
      { host: "ok", user: "-l" },
      { host: "bad host" },
      { host: "ok", port: 0 },
      { host: "ok", keyPath: "-iX" },
    ]) {
      expect(() => validateTarget(target)).toThrow(SshError);
    }
  });

  it("classifies OpenSSH errors, host-key change first", () => {
    expect(classifySshError("Host key verification failed.\nPermission denied")).toBe(
      "host-key-changed",
    );
    expect(classifySshError("dev@h: Permission denied (publickey).")).toBe("auth-failed");
    expect(classifySshError("ssh: connect to host h port 22: Connection refused")).toBe(
      "unreachable",
    );
    expect(classifySshError("something else")).toBe("unknown");
    expect(withWatchdog("indus --version")).toContain("sleep 15");
    expect(rewritePreviewUrl("http://localhost:5173/app?x=1", 40123)).toBe(
      "http://127.0.0.1:40123/app?x=1",
    );
    expect(rewritePreviewUrl("https://example.com/", 1)).toBe("https://example.com/");
  });
});

describe.skipIf(!posix)("against the fake ssh binary", () => {
  it("connects through one master and runs commands", async () => {
    const r = rig();
    const conn = new SshConnection({ host: "build.example", user: "dev" }, r.deps);
    await conn.connect();
    const out = await conn.exec("printf hello");
    expect(out).toMatchObject({ code: 0, stdout: "hello" });
    expect(await conn.check()).toBe(true);
    await conn.close();
    const calls = r.calls();
    for (const call of calls) {
      expect(call).toContain("BatchMode=yes");
      expect(call).toContain("StrictHostKeyChecking=accept-new");
      expect(call).toContain(`ControlPath=${conn.controlSocket()}`);
      expect(call[call.indexOf("--") + 1]).toBe("dev@build.example");
    }
    expect(calls.at(-1)).toEqual(expect.arrayContaining(["-O", "exit"]));
    expect(fs.statSync(path.dirname(conn.controlSocket() as string)).mode & 0o777).toBe(0o700);
  });

  it("a changed host key surfaces the OpenSSH error and refuses", async () => {
    const r = rig({ INDUS_FAKE_SSH_SCENARIO: "hostkey" });
    const conn = new SshConnection({ host: "build.example" }, r.deps);
    const failure = await conn.connect().catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(SshError);
    const error = failure as SshError;
    expect(error.kind).toBe("host-key-changed");
    expect(error.message).toContain("ssh-keygen -R build.example");
    expect(error.message).toContain("REMOTE HOST IDENTIFICATION HAS CHANGED");
    expect(error.detail).toContain("Host key verification failed.");
    expect(r.calls()).toHaveLength(1); // never retried, never "accept anyway"
  });

  it("a key that needs a prompt fails as auth, pointing at the agent", async () => {
    const r = rig({ INDUS_FAKE_SSH_SCENARIO: "auth" });
    const conn = new SshConnection({ host: "h", user: "dev" }, r.deps);
    await expect(conn.connect()).rejects.toMatchObject({ kind: "auth-failed" });
    await expect(conn.connect()).rejects.toThrow(/ssh-add/);
  });

  it("an exec past its timeout kills the child and marks the master dead", async () => {
    const r = rig({ INDUS_FAKE_SSH_SCENARIO: "hang" });
    const conn = new SshConnection({ host: "h" }, r.deps);
    const dead: string[] = [];
    conn.onDead((why) => dead.push(why));
    await expect(conn.exec("true", { timeoutMs: 200 })).rejects.toMatchObject({ kind: "timeout" });
    expect(conn.isDead()).toBe(true);
    expect(dead).toHaveLength(1);
  });

  it("the serve token goes over stdin, never argv", async () => {
    const r = rig();
    const bin = path.join(r.base, "bin");
    fs.mkdirSync(bin);
    const seen = path.join(r.base, "seen-token");
    fs.writeFileSync(
      path.join(bin, "indus"),
      `#!/bin/sh\nprintf '%s' "$INDUS_DASHBOARD_SESSION_TOKEN" > ${JSON.stringify(seen)}\necho '{"url": "http://127.0.0.1:48123", "pid": 7}'\n`,
      { mode: 0o755 },
    );
    r.deps.env = { ...r.deps.env, PATH: `${bin}:${process.env.PATH ?? ""}` };
    const conn = new SshConnection({ host: "h" }, r.deps);
    const token = ["ssh", "serve", "token"].join("-");
    const serve = await conn.ensureServe(token, "research");
    expect(serve.remotePort).toBe(48123);
    expect(fs.readFileSync(seen, "utf8")).toBe(token);
    expect(JSON.stringify(r.calls())).not.toContain(token);
    expect(JSON.stringify(r.calls())).toContain("-p 'research' serve ensure --json");
  });

  it("client-only files reach the remote as bytes, never as a client path (R08.23)", async () => {
    const r = rig();
    const conn = new SshConnection({ host: "h" }, r.deps);
    const local = path.join(r.base, "secret-plans", "notes.txt");
    const remote = await conn.uploadBytes(Buffer.from("the bytes"), local);
    expect(fs.readFileSync(remote, "utf8")).toBe("the bytes");
    expect(path.basename(remote)).toBe("notes.txt");
    expect(JSON.stringify(r.calls())).not.toContain("secret-plans");
  });

  it("tunnel restarts are capped at 5", async () => {
    const r = rig({ INDUS_FAKE_SSH_TUNNEL: "die" }, { restartDelayMs: 5 });
    const conn = new SshConnection({ host: "h" }, r.deps);
    const gaveUp = new Promise<void>((resolve) => {
      conn.forward(41000, 9119, resolve);
    });
    await gaveUp;
    const forwards = r.calls().filter((call) => call.includes("-N"));
    expect(forwards).toHaveLength(1 + TUNNEL_RESTART_LIMIT);
    expect(forwards[0]).toEqual(expect.arrayContaining(["-L", "127.0.0.1:41000:127.0.0.1:9119"]));
    expect(r.lines.some((line) => line.includes("giving up"))).toBe(true);
  });

  it("a forward that stays up is not restarted and closes cleanly", async () => {
    const r = rig();
    const conn = new SshConnection({ host: "h" }, r.deps);
    const tunnel = conn.forward(41001, 9119);
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(tunnel.alive()).toBe(true);
    expect(tunnel.restarts()).toBe(0);
    tunnel.close();
    expect(tunnel.alive()).toBe(false);
  });
});
