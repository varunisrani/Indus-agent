import { createHash } from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import type net from "node:net";
import path from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { cleanupFakes, tempDir } from "../../testing/fakeIndus.ts";
import {
  classifyStatus,
  normalizeRemoteUrl,
  REMOTE_FILE,
  REMOTE_TOKEN_SECRET,
  RemoteStore,
  RemoteUrlError,
  remoteWsUrl,
} from "./remote.ts";

const servers: http.Server[] = [];
afterAll(() => {
  for (const server of servers) server.close();
  cleanupFakes();
});

const TOKEN = ["remote", "test", "credential", "value"].join("-");

interface ServerOptions {
  authRequired?: boolean;
  configStatus?: number;
  /** close code for the WS leg, or 0 to send a ready frame */
  wsClose?: number;
  wsAcceptToken?: string;
}

function frame(opcode: number, payload: Buffer): Buffer {
  return Buffer.concat([Buffer.from([0x80 | opcode, payload.length]), payload]);
}

async function server(options: ServerOptions = {}): Promise<{ url: string; seen: string[] }> {
  const seen: string[] = [];
  const srv = http.createServer((request, response) => {
    seen.push(
      `${request.method} ${request.url} token=${String(request.headers["x-indus-session-token"] ?? "")}`,
    );
    response.setHeader("content-type", "application/json");
    if (request.url === "/api/status") {
      response.end(JSON.stringify({ status: "ok", auth_required: options.authRequired === true }));
      return;
    }
    if (request.headers["x-indus-session-token"] !== TOKEN) {
      response.writeHead(401).end('{"error": "unauthenticated"}');
      return;
    }
    response.writeHead(options.configStatus ?? 200).end("{}");
  });
  srv.on("upgrade", (request, socket: net.Socket) => {
    const offered = String(request.headers["sec-websocket-protocol"] ?? "")
      .split(",")
      .map((p) => p.trim());
    seen.push(`WS ${request.url} ${offered.join(" ")}`);
    const accept = createHash("sha1")
      .update(`${String(request.headers["sec-websocket-key"])}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`)
      .digest("base64");
    socket.write(
      `HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\nSec-WebSocket-Protocol: indus-dashboard-v1\r\n\r\n`,
    );
    const good = offered.includes(`indus-dashboard-token.${options.wsAcceptToken ?? TOKEN}`);
    const code = options.wsClose ?? (good ? 0 : 4401);
    if (code === 0) socket.write(frame(0x1, Buffer.from('{"jsonrpc":"2.0","method":"ready"}')));
    else {
      const payload = Buffer.alloc(2);
      payload.writeUInt16BE(code, 0);
      socket.end(frame(0x8, payload));
    }
    socket.on("error", () => {});
  });
  await new Promise<void>((resolve) => srv.listen(0, "127.0.0.1", resolve));
  servers.push(srv);
  return { url: `http://127.0.0.1:${(srv.address() as net.AddressInfo).port}`, seen };
}

function store() {
  const userData = tempDir();
  const secrets = new Map<string, string>();
  const lines: string[] = [];
  const remote = new RemoteStore({
    userData,
    secrets: {
      get: (name) => secrets.get(name) ?? null,
      put: (name, value) => secrets.set(name, value),
      delete: (name) => secrets.delete(name),
    },
    log: (line) => lines.push(line),
    settleMs: 100,
    timeoutMs: 3_000,
  });
  return { remote, secrets, userData, lines };
}

describe("remote connection", () => {
  it("both legs pass → saved; the token goes to the secrets store, not the file", async () => {
    const s = await server();
    const { remote, secrets, userData, lines } = store();
    const result = await remote.save({ url: `${s.url}/`, token: TOKEN });
    expect(result).toEqual({ ok: true, http: "ok", ws: "ok", saved: true });
    expect(secrets.get(REMOTE_TOKEN_SECRET)).toBe(TOKEN);
    const file = fs.readFileSync(path.join(userData, REMOTE_FILE), "utf8");
    expect(file).not.toContain(TOKEN);
    expect(JSON.parse(file)).toEqual({ v: 1, url: s.url });
    expect(fs.statSync(path.join(userData, REMOTE_FILE)).mode & 0o777).toBe(0o600);
    expect(remote.get()).toEqual({ url: s.url, hasToken: true });
    expect(remote.saved()).toEqual({ url: s.url, token: TOKEN });
    expect(lines.join("\n")).not.toContain(TOKEN);
    // the WS leg offered the dashboard subprotocols with the credential, the URL carried none
    expect(s.seen).toContain(`WS /api/ws indus-dashboard-v1 indus-dashboard-token.${TOKEN}`);
  });

  it("HTTP ok + WS 4401 → not saved, error shown", async () => {
    const s = await server({ wsAcceptToken: "something-else" });
    const { remote, secrets, userData } = store();
    const result = await remote.save({ url: s.url, token: TOKEN });
    expect(result).toMatchObject({ ok: false, http: "ok", ws: "unauthorized", saved: false });
    expect(result.message).toBe("the server refused this token");
    expect(secrets.size).toBe(0);
    expect(fs.existsSync(path.join(userData, REMOTE_FILE))).toBe(false);
  });

  it("a wrong token is unauthorized on both legs", async () => {
    const s = await server();
    const { remote } = store();
    expect(await remote.test({ url: s.url, token: "wrong-token-value" })).toMatchObject({
      ok: false,
      http: "unauthorized",
      ws: "unauthorized",
    });
  });

  it("503 is connectivity, not reauth", async () => {
    const s = await server({ configStatus: 503 });
    const { remote } = store();
    const result = await remote.test({ url: s.url, token: TOKEN });
    expect(result).toMatchObject({ ok: false, http: "error", ws: "ok" });
    expect(classifyStatus(503)).toBe("error");
    expect(classifyStatus(401)).toBe("unauthorized");
    expect(classifyStatus(403)).toBe("unauthorized");
    expect(classifyStatus(404)).toBe("ok");
  });

  it("an unreachable server is unreachable, not unauthorized", async () => {
    const { remote } = store();
    expect(await remote.test({ url: "http://127.0.0.1:9", token: TOKEN })).toMatchObject({
      ok: false,
      http: "unreachable",
    });
  });

  it("a server that wants a sign-in is reported, not saved", async () => {
    const s = await server({ authRequired: true });
    const { remote } = store();
    const result = await remote.save({ url: s.url, token: TOKEN });
    expect(result.saved).toBe(false);
    expect(result.message).toContain("sign-in");
  });

  it("forget clears the URL and the token", async () => {
    const s = await server();
    const { remote, secrets } = store();
    await remote.save({ url: s.url, token: TOKEN });
    remote.forget();
    expect(remote.get()).toEqual({ url: null, hasToken: false });
    expect(secrets.size).toBe(0);
  });

  it("normalises URLs", () => {
    expect(normalizeRemoteUrl(" 100.64.0.1:9119 ")).toBe("http://100.64.0.1:9119");
    expect(normalizeRemoteUrl("https://agent.example.test/indus/")).toBe(
      "https://agent.example.test/indus",
    );
    for (const bad of ["", "ftp://x", "http://u:p@x", "http://x/?a=1", "http://x/#f"]) {
      expect(() => normalizeRemoteUrl(bad)).toThrow(RemoteUrlError);
    }
    expect(remoteWsUrl("https://agent.example.test/indus")).toBe(
      "wss://agent.example.test/indus/api/ws",
    );
    expect(remoteWsUrl("http://127.0.0.1:9119")).toBe("ws://127.0.0.1:9119/api/ws");
  });
});
