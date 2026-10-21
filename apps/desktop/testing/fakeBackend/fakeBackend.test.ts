import { execFile } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, describe, expect, it } from "vitest";
// @ts-expect-error -- plain JS module without types
import { controlQuery } from "./control.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ENSURE = path.join(HERE, "ensure.mjs");
const posix = process.platform !== "win32";
const homes: string[] = [];

type Answer = { ok: boolean; result?: Record<string, unknown>; error?: string };
const query = controlQuery as (home: string, verb: string, params?: object) => Promise<Answer>;

function run(
  home: string,
  args: string[],
  env: Record<string, string> = {},
): Promise<{ code: number; stdout: string }> {
  return new Promise((resolve) => {
    execFile(
      process.execPath,
      [ENSURE, ...args],
      { env: { ...process.env, INDUS_HOME: home, ...env } },
      (error, stdout) => {
        resolve({
          code: error === null ? 0 : Number((error as { code?: number }).code ?? 1),
          stdout: String(stdout),
        });
      },
    );
  });
}

function newHome(): string {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "indus-fake-"));
  homes.push(home);
  return home;
}

afterAll(async () => {
  for (const home of homes) {
    for (const role of ["daemon", "serve"]) {
      try {
        const state = JSON.parse(fs.readFileSync(path.join(home, `fake-${role}.json`), "utf8")) as {
          pid: number;
        };
        process.kill(state.pid, "SIGKILL");
      } catch {
        // not running
      }
    }
    fs.rmSync(home, { recursive: true, force: true });
  }
});

/** Open a WebSocket offering `protocols`; resolves the first message or the close code. */
function dial(
  url: string,
  protocols: string[],
): Promise<{
  opened: boolean;
  protocol: string;
  first?: unknown;
  closeCode?: number;
  ws: WebSocket;
}> {
  return new Promise((resolve) => {
    const ws = new WebSocket(url, protocols);
    let opened = false;
    ws.addEventListener("open", () => {
      opened = true;
    });
    ws.addEventListener("message", (event) =>
      resolve({ opened, protocol: ws.protocol, first: JSON.parse(String(event.data)), ws }),
    );
    ws.addEventListener("close", (event) =>
      resolve({ opened, protocol: ws.protocol, closeCode: event.code, ws }),
    );
  });
}

function rpc(ws: WebSocket, method: string, params: object = {}): Promise<Record<string, unknown>> {
  const id = Math.floor(Math.random() * 1e9);
  return new Promise((resolve) => {
    const onMessage = (event: MessageEvent) => {
      const frame = JSON.parse(String(event.data)) as Record<string, unknown>;
      if (frame.id === id) {
        ws.removeEventListener("message", onMessage);
        resolve(frame);
      }
    };
    ws.addEventListener("message", onMessage);
    ws.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
  });
}

describe.skipIf(!posix)("fake backend", () => {
  it("--version answers like the real CLI; a scripted failure exits non-zero", async () => {
    const home = newHome();
    expect(await run(home, ["--version"])).toEqual({ code: 0, stdout: "indus 0.20.0\n" });
    expect(
      (await run(home, ["--version"], { INDUS_FAKE_SCENARIO: "version-probe-fails" })).code,
    ).toBe(3);
  });

  it("ensure → descriptor; tickets are single use; JSON-RPC answers; warm ensure reuses the daemon", async () => {
    const home = newHome();
    const first = await run(home, ["gateway", "ensure", "--json"]);
    expect(first.code).toBe(0);
    const descriptor = JSON.parse(first.stdout) as Record<string, string>;
    expect(Object.keys(descriptor).sort()).toEqual([
      "code_version",
      "instance_id",
      "profile_id",
      "ws_url",
    ]);
    expect(descriptor.ws_url).toMatch(/^ws:\/\/127\.0\.0\.1:\d+\/ws$/);

    const minted = await query(home, "session-ticket", { purpose: "ws-interactive" });
    expect(minted.ok).toBe(true);
    const ticket = String(minted.result?.ticket);
    expect(ticket).toMatch(/^[A-Za-z0-9_-]{43}$/);
    const protocols = ["indus-gateway-v1", `indus-gateway-ticket.${ticket}`];
    const good = await dial(String(descriptor.ws_url), protocols);
    expect(good.opened).toBe(true);
    expect(good.protocol).toBe("indus-gateway-v1");
    expect(good.first).toMatchObject({ method: "gateway.ready" });
    expect(await rpc(good.ws, "session.create")).toMatchObject({
      result: { session_id: "s-fake-1" },
    });
    expect(await rpc(good.ws, "nope")).toMatchObject({ error: { code: -32601 } });
    good.ws.close();

    const reused = await dial(String(descriptor.ws_url), protocols);
    expect(reused.closeCode).toBe(4401);
    const none = await dial(String(descriptor.ws_url), ["indus-gateway-v1"]);
    expect(none.closeCode).toBe(4401);

    const warm = await run(home, ["gateway", "ensure", "--json"]);
    expect(JSON.parse(warm.stdout)).toEqual(descriptor);
    expect((await run(home, ["gateway", "stop"])).code).toBe(0);
    expect(
      JSON.parse((await run(home, ["gateway", "ensure", "--json", "--no-start"])).stdout),
    ).toMatchObject({ state: "absent" });
  });

  it("an Origin the daemon does not allow is closed 4403", async () => {
    const home = newHome();
    const descriptor = JSON.parse((await run(home, ["gateway", "ensure", "--json"])).stdout) as {
      ws_url: string;
    };
    const minted = await query(home, "session-ticket", { purpose: "ws-interactive" });
    const closed = await new Promise<number>((resolve) => {
      // Node's WebSocket sends no Origin; send one by hand through a raw upgrade.
      const url = new URL(descriptor.ws_url);
      const socket = net.connect(Number(url.port), "127.0.0.1", () => {
        socket.write(
          [
            "GET /ws HTTP/1.1",
            `Host: ${url.host}`,
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Version: 13",
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
            `Sec-WebSocket-Protocol: indus-gateway-v1, indus-gateway-ticket.${String(minted.result?.ticket)}`,
            "Origin: file://",
            "",
            "",
          ].join("\r\n"),
        );
      });
      let data = Buffer.alloc(0);
      socket.on("data", (chunk: Buffer) => {
        data = Buffer.concat([data, chunk]);
        const end = data.indexOf("\r\n\r\n");
        if (end >= 0 && data.length >= end + 6) {
          resolve(data.readUInt16BE(end + 4 + 2));
          socket.destroy();
        }
      });
    });
    expect(closed).toBe(4403);
  });

  it("stale-socket scenario: the first mint fails, the next succeeds", async () => {
    const home = newHome();
    await run(home, ["gateway", "ensure", "--json"], { INDUS_FAKE_SCENARIO: "stale-socket" });
    expect(await query(home, "session-ticket", { purpose: "ws-interactive" })).toMatchObject({
      ok: false,
      error: "stale_socket",
    });
    expect((await query(home, "session-ticket", { purpose: "ws-interactive" })).ok).toBe(true);
    expect((await query(home, "session-ticket", { purpose: "native-http" })).ok).toBe(false);
  });

  it("version skew: the old daemon is restarted once and the descriptor carries the CLI's version", async () => {
    const home = newHome();
    const env = { INDUS_FAKE_SCENARIO: "version-skew" };
    // The first daemon reports 0.19.0; the next ensure sees the skew and restarts it once.
    const descriptor = JSON.parse(
      (await run(home, ["gateway", "ensure", "--json"], env)).stdout,
    ) as { code_version: string };
    expect(descriptor.code_version).toBe("0.19.0");
    const second = JSON.parse((await run(home, ["gateway", "ensure", "--json"], env)).stdout) as {
      code_version: string;
    };
    expect(second.code_version).toBe("0.20.0");
  });

  it("ensure failure exits 1 with the verdict", async () => {
    const home = newHome();
    const result = await run(home, ["gateway", "ensure", "--json"], {
      INDUS_FAKE_SCENARIO: "ensure-fails",
    });
    expect(result.code).toBe(1);
    expect(JSON.parse(result.stdout)).toEqual({
      state: "exited",
      reason: "bad_config",
      detail: "the daemon exited with code 78",
    });
  });

  it("serve ensure: REST gated on the session token; 404 for unknown routes; profile homes are separate", async () => {
    const home = newHome();
    const token = ["serve", "session", String(Date.now())].join("-");
    const served = JSON.parse(
      (
        await run(home, ["-p", "work", "serve", "ensure", "--json"], {
          INDUS_DASHBOARD_SESSION_TOKEN: token,
        })
      ).stdout,
    ) as {
      url: string;
    };
    expect(fs.existsSync(path.join(home, "profiles", "work", "fake-serve.json"))).toBe(true);
    const status = await fetch(`${served.url}/api/status`);
    expect(status.status).toBe(200);
    expect((await fetch(`${served.url}/api/sessions`)).status).toBe(401);
    const ok = await fetch(`${served.url}/api/sessions`, {
      headers: { "X-Indus-Session-Token": token },
    });
    expect(await ok.json()).toEqual({ sessions: [] });
    const bearer = await fetch(`${served.url}/api/config`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(bearer.status).toBe(200);
    expect(
      (await fetch(`${served.url}/api/nope`, { headers: { "X-Indus-Session-Token": token } }))
        .status,
    ).toBe(404);
    expect((await run(home, ["-p", "work", "serve", "stop"])).code).toBe(0);
  });
});
