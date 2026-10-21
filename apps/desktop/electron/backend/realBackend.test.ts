// One integration run against the real runtime (P17 daemon + P18 `indus serve`) in a temp home:
// the resolver finds the repo checkout, `gateway ensure` / `serve ensure` answer the shapes the
// desktop parses, a ticket minted over the real control socket (a private pointer file when the
// home is deep) opens the daemon's WebSocket exactly once, the REST proxy reaches the serve with
// the launch token, and the remote test passes both legs against the serve. Skipped (with the
// reason) when the checkout has no `.venv` (run `uv sync`) or INDUS_DESKTOP_SKIP_REAL=1.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, describe, expect, it } from "vitest";
import { cleanupFakes, tempDir } from "../../testing/fakeIndus.ts";
import { ApiProxy } from "../api/proxy.ts";
import { LocalAttach } from "./localAttach.ts";
import { runCommand } from "./process.ts";
import { RemoteStore } from "./remote.ts";
import { resolveBackend } from "./resolver.ts";
import { DialReservations } from "./tickets.ts";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "..");
const VENV_INDUS = path.join(REPO, ".venv", "bin", "indus");
const available =
  process.platform !== "win32" &&
  process.env.INDUS_DESKTOP_SKIP_REAL !== "1" &&
  fs.existsSync(VENV_INDUS) &&
  fs.existsSync(path.join(REPO, "src", "indus", "__init__.py"));

const cleanups: Array<() => Promise<void> | void> = [];
afterAll(async () => {
  for (const cleanup of cleanups.reverse()) await cleanup();
  cleanupFakes();
});

describe.skipIf(!available)("the real Indus runtime (temp home)", () => {
  it("resolve → ensure → ticket → dial once → REST → remote test", async () => {
    const base = tempDir("real-");
    const home = path.join(base, "h");
    fs.mkdirSync(home, { mode: 0o700 });
    fs.writeFileSync(
      path.join(home, "config.yaml"),
      "model:\n  default: fake-model\n  provider: custom\n  base_url: http://127.0.0.1:9/v1\n",
    );
    const env: Record<string, string | undefined> = {
      PATH: "/usr/bin:/bin",
      HOME: path.join(base, "user-home"),
      INDUS_HOME: home,
      INDUS_GATEWAY_LOCK_DIR: path.join(base, "locks"),
      TMPDIR: process.env.TMPDIR,
      LANG: "C.UTF-8",
    };

    const resolution = await resolveBackend({
      env: { ...env, INDUS_DESKTOP_INDUS_ROOT: REPO },
      argv: [],
      platform: process.platform,
      packaged: true,
      home,
      homeDir: path.join(base, "user-home"),
      devCheckoutRoot: null,
      appExecPath: process.execPath,
      isWsl: false,
    });
    expect(resolution.kind).toBe("found");
    if (resolution.kind !== "found") return;
    expect(resolution.backend.rung).toBe("source-root");

    const lines: string[] = [];
    const attach = new LocalAttach({
      backend: resolution.backend,
      version: resolution.version,
      profile: null,
      home,
      env,
      log: (line) => lines.push(line),
    });
    cleanups.push(async () => {
      await runCommand(resolution.backend, ["gateway", "stop"], { env, timeoutMs: 30_000 });
    });
    const gateway = await attach.attachGateway();
    expect(gateway.wsUrl).toMatch(/^ws:\/\/127\.0\.0\.1:\d+\//);
    expect(gateway.codeVersion).toBe(resolution.version);
    expect(attach.startedByApp).toBe(true);

    const serve = await attach.ensureServe();
    cleanups.push(() => {
      try {
        process.kill(serve.pid, "SIGTERM");
      } catch {
        // gone
      }
    });
    expect(serve.url).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);

    // a ticket over the real control socket opens the WebSocket once
    const minted = await attach.mint();
    expect(minted.ok).toBe(true);
    if (!minted.ok) return;
    const answer = new DialReservations().reserveLocal(minted.wsUrl, minted.ticket, 1);
    const dial = () =>
      new Promise<{ opened: boolean; code: number }>((resolve) => {
        let opened = false;
        const ws = new WebSocket(answer.wsUrl, answer.protocols);
        ws.addEventListener("open", () => {
          opened = true;
          setTimeout(() => ws.close(1000), 300);
        });
        ws.addEventListener("close", (event) => resolve({ opened, code: event.code }));
      });
    const first = await dial();
    expect(first.opened).toBe(true);
    expect(first.code).toBe(1000);
    const reused = await dial();
    expect(reused.code).toBe(4401);

    // REST through the proxy with the launch token
    const proxy = new ApiProxy({
      target: () => ({ origin: serve.url, token: attach.sessionToken, profile: null }),
      log: () => {},
    });
    const status = await proxy.request({ method: "GET", path: "/api/status" });
    expect(status).toMatchObject({ kind: "response", status: 200 });
    const config = await proxy.request({ method: "GET", path: "/api/config" });
    expect(config.kind === "response" && config.status).toBe(200);
    const missing = await proxy.request({ method: "GET", path: "/api/no-such-route" });
    expect(missing.kind).toBe("not-found");

    // the same serve as a "remote": both legs pass with the token, fail without it
    const secrets = new Map<string, string>();
    const remote = new RemoteStore({
      userData: path.join(base, "ud"),
      secrets: {
        get: (name) => secrets.get(name) ?? null,
        put: (name, value) => secrets.set(name, value),
        delete: (name) => secrets.delete(name),
      },
      log: () => {},
    });
    expect(await remote.test({ url: serve.url, token: attach.sessionToken })).toEqual({
      ok: true,
      http: "ok",
      ws: "ok",
    });
    expect(await remote.test({ url: serve.url, token: "x".repeat(43) })).toMatchObject({
      ok: false,
      http: "unauthorized",
      ws: "unauthorized",
    });

    expect(lines.join("\n")).not.toContain(attach.sessionToken);
  }, 120_000);
});
