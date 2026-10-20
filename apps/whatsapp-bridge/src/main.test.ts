import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { alive, readConfig, run } from "./main.ts";
import { FakeWa } from "./testing/fakeSocket.ts";

const TOKEN = "k".repeat(40);
const HERE = fileURLToPath(new URL(".", import.meta.url));

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "wa-main-"));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

function env(extra: Record<string, string> = {}): Record<string, string> {
  return {
    INDUS_WA_TOKEN: TOKEN,
    INDUS_WA_PORT: "0",
    INDUS_WA_AUTH_DIR: join(dir, "auth"),
    INDUS_WA_PROFILE: "work",
    ...extra,
  };
}

/** Gone, or a zombie nobody reaped yet (an orphan's parent in a container may never reap). */
function finished(pid: number): boolean {
  if (!alive(pid)) return true;
  try {
    const stat = readFileSync(`/proc/${pid}/stat`, "utf8");
    return stat.slice(stat.lastIndexOf(")") + 2).startsWith("Z");
  } catch {
    return true;
  }
}

describe("settings", () => {
  it("refuses a short token, a bad port, a relative auth folder and a non-loopback host", () => {
    expect(readConfig(env({ INDUS_WA_TOKEN: "short" }))).toEqual({ error: "token" });
    expect(readConfig(env({ INDUS_WA_PORT: "70000" }))).toEqual({ error: "port" });
    expect(readConfig(env({ INDUS_WA_AUTH_DIR: "auth" }))).toEqual({ error: "auth_dir" });
    expect(readConfig(env({ INDUS_WA_HOST: "0.0.0.0" }))).toEqual({ error: "host" });
    expect(readConfig(env({ INDUS_WA_MODE: "other" }))).toEqual({ error: "mode" });
    expect(readConfig(env({ INDUS_WA_PAIR_PHONE: "+1 555-000" }))).toMatchObject({
      pairPhone: "1555000",
    });
  });

  it("exits 2 on a non-loopback host before binding anything", async () => {
    const lines: string[] = [];
    const code = await run(env({ INDUS_WA_HOST: "192.0.2.1" }), {
      out: (l) => lines.push(l),
      provider: new FakeWa().provider,
    });
    expect(code).toBe(2);
    expect(lines).toEqual([JSON.stringify({ event: "error", error: "bad_host" })]);
  });

  it("fails closed with a clear error when no provider is configured", async () => {
    const lines: string[] = [];
    expect(await run(env(), { out: (l) => lines.push(l) })).toBe(2);
    expect(lines).toEqual([JSON.stringify({ event: "error", error: "no_provider" })]);
    const other: string[] = [];
    const missing = join(dir, "missing-provider.mjs");
    expect(await run(env({ INDUS_WA_PROVIDER: missing }), { out: (l) => other.push(l) })).toBe(2);
    expect(other).toEqual([JSON.stringify({ event: "error", error: "provider" })]);
  });

  it("creates the auth folder owner-only and stops on its stop signal", async () => {
    let stop: () => void = () => {};
    const lines: string[] = [];
    const running = run(env(), {
      out: (l) => {
        lines.push(l);
        if (l.includes("connected")) stop();
      },
      provider: new FakeWa().provider,
      stopSignal: new Promise((resolve) => {
        stop = resolve;
      }),
    });
    expect(await running).toBe(0);
    expect(statSync(join(dir, "auth")).mode & 0o777).toBe(0o700);
    expect(lines.map((l) => (JSON.parse(l) as { event: string }).event)).toEqual([
      "listening",
      "connected",
    ]);
  });
});

describe("parent watch", () => {
  it("exits within 2 s of its parent's death", async () => {
    const main = join(HERE, "main.ts");
    const fake = join(HERE, "testing", "fakeSocket.ts");
    // A parent process that starts the bridge (pointing it at itself) and then just waits.
    const script = [
      "const { spawn } = require('node:child_process');",
      `const child = spawn(process.execPath, [${JSON.stringify(main)}], {`,
      "  stdio: ['ignore', 'inherit', 'inherit'],",
      `  env: { ...JSON.parse(process.env.BRIDGE_ENV), INDUS_WA_PARENT_PID: String(process.pid), INDUS_WA_PROVIDER: ${JSON.stringify(fake)} },`,
      "});",
      "process.stdout.write(JSON.stringify({ child: child.pid }) + '\\n');",
      "setInterval(() => {}, 1000);",
    ].join("\n");
    const parent = spawn(process.execPath, ["-e", script], {
      stdio: ["ignore", "pipe", "inherit"],
      env: { PATH: process.env.PATH ?? "", BRIDGE_ENV: JSON.stringify(env()) },
    });
    let childPid = 0;
    let output = "";
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`bridge did not start: ${output}`)), 15_000);
      parent.stdout?.on("data", (chunk: Buffer) => {
        output += chunk.toString("utf8");
        const found = /"child":(\d+)/.exec(output);
        if (found) childPid = Number(found[1]);
        if (childPid && output.includes('"connected"')) {
          clearTimeout(timer);
          resolve();
        }
      });
    });
    expect(alive(childPid)).toBe(true);
    parent.kill("SIGKILL");
    const killedAt = Date.now();
    while (!finished(childPid) && Date.now() - killedAt < 5000) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    const took = Date.now() - killedAt;
    if (!finished(childPid)) process.kill(childPid, "SIGKILL");
    expect(took).toBeLessThan(2000);
  }, 30_000);
});
