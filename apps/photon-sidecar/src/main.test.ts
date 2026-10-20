import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { request } from "node:http";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { LinkError } from "./link.ts";
import { alive, DEFAULT_PORT, readConfig, run } from "./main.ts";
import { FakePhoton } from "./testing/fakeLink.ts";

const TOKEN = "q".repeat(40);
const HERE = fileURLToPath(new URL(".", import.meta.url));
// Built at run time: never a credential-shaped literal in the tree.
const SECRET = ["fake", "project", "secret"].join("-");

function env(extra: Record<string, string> = {}): Record<string, string> {
  return {
    INDUS_PHOTON_TOKEN: TOKEN,
    INDUS_PHOTON_PORT: "0",
    INDUS_PHOTON_PROFILE: "work",
    PHOTON_PROJECT_ID: "project-1",
    PHOTON_PROJECT_SECRET: SECRET,
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

function health(port: number): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    const req = request(
      { host: "127.0.0.1", port, path: "/health", headers: { authorization: `Bearer ${TOKEN}` } },
      (res) => {
        let text = "";
        res.setEncoding("utf8");
        res.on("data", (c: string) => {
          text += c;
        });
        res.on("end", () => resolve(JSON.parse(text) as Record<string, unknown>));
      },
    );
    req.on("error", reject);
    req.end();
  });
}

const events = (lines: string[]): unknown[] => lines.map((l) => JSON.parse(l) as unknown);

describe("settings", () => {
  it("refuses a short token, a bad port and a non-loopback host; defaults to 8789", () => {
    expect(readConfig(env({ INDUS_PHOTON_TOKEN: "short" }))).toEqual({ error: "token" });
    expect(readConfig(env({ INDUS_PHOTON_PORT: "70000" }))).toEqual({ error: "port" });
    expect(readConfig(env({ INDUS_PHOTON_HOST: "0.0.0.0" }))).toEqual({ error: "host" });
    const { INDUS_PHOTON_PORT: _, ...rest } = env();
    expect(readConfig(rest)).toMatchObject({ port: DEFAULT_PORT, host: "127.0.0.1" });
  });

  it("exits 2 on a non-loopback host before binding anything", async () => {
    const lines: string[] = [];
    const code = await run(env({ INDUS_PHOTON_HOST: "192.0.2.1" }), {
      out: (l) => lines.push(l),
      provider: new FakePhoton().provider,
    });
    expect(code).toBe(2);
    expect(events(lines)).toEqual([{ event: "error", error: "bad_host" }]);
  });

  it("fails closed when no provider is configured or it cannot be loaded", async () => {
    const lines: string[] = [];
    expect(await run(env(), { out: (l) => lines.push(l) })).toBe(2);
    expect(events(lines)).toEqual([{ event: "error", error: "no_provider" }]);
    const other: string[] = [];
    const missing = `${HERE}missing-provider.mjs`;
    expect(await run(env({ INDUS_PHOTON_PROVIDER: missing }), { out: (l) => other.push(l) })).toBe(
      2,
    );
    expect(events(other)).toEqual([{ event: "error", error: "provider" }]);
  });

  it("exits 2 without the project credentials and never prints the secret", async () => {
    const lines: string[] = [];
    const code = await run(env({ PHOTON_PROJECT_ID: "" }), {
      out: (l) => lines.push(l),
      provider: new FakePhoton().provider,
    });
    expect(code).toBe(2);
    expect(events(lines)).toEqual([{ event: "error", error: "credentials" }]);
    expect(lines.join("\n")).not.toContain(SECRET);
  });
});

describe("running", () => {
  it("serves, opens the link with the credentials and stops on its stop signal", async () => {
    const photon = new FakePhoton();
    let stop: () => void = () => {};
    const lines: string[] = [];
    let port = 0;
    let seen: Record<string, unknown> = {};
    const running = run(env(), {
      out: (l) => {
        lines.push(l);
        const event = JSON.parse(l) as { event: string; port?: number; state?: string };
        if (event.event === "listening") port = event.port ?? 0;
        if (event.state === "open") {
          void health(port).then((body) => {
            seen = body;
            stop();
          });
        }
      },
      provider: photon.provider,
      stopSignal: new Promise((resolve) => {
        stop = resolve;
      }),
    });
    expect(await running).toBe(0);
    expect(seen).toMatchObject({ status: "open", profile: "work" });
    expect(photon.opened.map((o) => [o.projectId, o.projectSecret])).toEqual([
      ["project-1", SECRET],
    ]);
    expect(photon.closes).toBe(1);
    expect(events(lines)).toEqual([
      { event: "listening", port },
      { event: "link", state: "connecting" },
      { event: "link", state: "open" },
    ]);
    expect(lines.join("\n")).not.toContain(SECRET);
    expect(lines.join("\n")).not.toContain(TOKEN);
  });

  it("retries a failing link with a growing wait and stops retrying refused credentials", async () => {
    const photon = new FakePhoton();
    photon.refusals.push(new Error("down"), new Error("down"), new LinkError("auth_failed"));
    let stop: () => void = () => {};
    const states: string[] = [];
    const running = run(env(), {
      out: (l) => {
        const event = JSON.parse(l) as { state?: string };
        if (event.state) states.push(event.state);
        if (event.state === "auth_failed") setTimeout(() => stop(), 60);
      },
      provider: photon.provider,
      retryFirstMs: 5,
      stopSignal: new Promise((resolve) => {
        stop = resolve;
      }),
    });
    expect(await running).toBe(0);
    expect(photon.opened).toHaveLength(3);
    expect(states).toEqual([
      "connecting",
      "closed",
      "connecting",
      "closed",
      "connecting",
      "auth_failed",
    ]);
  });
});

describe("parent watch", () => {
  it("exits within 2 s of its parent's death", async () => {
    const main = `${HERE}main.ts`;
    const fake = `${HERE}testing/fakeLink.ts`;
    // A parent process that starts the sidecar (pointing it at itself) and then just waits.
    const script = [
      "const { spawn } = require('node:child_process');",
      `const child = spawn(process.execPath, [${JSON.stringify(main)}], {`,
      "  stdio: ['ignore', 'inherit', 'inherit'],",
      `  env: { ...JSON.parse(process.env.SIDECAR_ENV), INDUS_PHOTON_PARENT_PID: String(process.pid), INDUS_PHOTON_PROVIDER: ${JSON.stringify(fake)} },`,
      "});",
      "process.stdout.write(JSON.stringify({ child: child.pid }) + '\\n');",
      "setInterval(() => {}, 1000);",
    ].join("\n");
    const parent = spawn(process.execPath, ["-e", script], {
      stdio: ["ignore", "pipe", "inherit"],
      env: { PATH: process.env.PATH ?? "", SIDECAR_ENV: JSON.stringify(env()) },
    });
    let childPid = 0;
    let output = "";
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`sidecar did not start: ${output}`)), 15_000);
      parent.stdout?.on("data", (chunk: Buffer) => {
        output += chunk.toString("utf8");
        const found = /"child":(\d+)/.exec(output);
        if (found) childPid = Number(found[1]);
        if (childPid && output.includes('"open"')) {
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
    expect(output).not.toContain(SECRET);
  }, 30_000);
});
