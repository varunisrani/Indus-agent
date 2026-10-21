// A fake remote Indus serve for the connections tests (P24-29): `/api/status` (public,
// advertises native sign-in), `/api/config` and `POST /api/auth/ws-ticket` behind the session
// token (or a bearer), single-use tickets, and a WebSocket upgrade that accepts a minted ticket
// once (4401 otherwise). Every request is recorded in `seen` (method, URL and the access-proxy
// header's value only — never a credential).

import { createHash, randomBytes } from "node:crypto";
import http from "node:http";
import type net from "node:net";

export const SERVE_TOKEN = ["serve", "token", "value"].join("-");

const servers: http.Server[] = [];

export function closeFakeServes(): void {
  for (const server of servers.splice(0)) server.close();
}

export interface FakeServe {
  url: string;
  minted: string[];
  seen: string[];
}

export interface FakeServeOptions {
  /** the WS leg closes with this code even for a good ticket */
  wsClose?: number;
  ticketStatus?: number;
  configStatus?: number;
  /** accept `Authorization: Bearer <this>` instead of the token */
  bearer?: string;
}

function frame(opcode: number, payload: Buffer): Buffer {
  return Buffer.concat([Buffer.from([0x80 | opcode, payload.length]), payload]);
}

export async function fakeServe(options: FakeServeOptions = {}): Promise<FakeServe> {
  const minted: string[] = [];
  const live = new Set<string>();
  const seen: string[] = [];
  const authorized = (request: http.IncomingMessage) =>
    options.bearer !== undefined
      ? request.headers.authorization === `Bearer ${options.bearer}`
      : request.headers["x-indus-session-token"] === SERVE_TOKEN;
  const srv = http.createServer((request, response) => {
    seen.push(
      `${request.method} ${request.url} cf=${String(request.headers["cf-access-client-id"] ?? "")}`,
    );
    response.setHeader("content-type", "application/json");
    if (request.url === "/api/status") {
      response.end(
        JSON.stringify({ status: "ok", auth_flows: ["native_pkce"], version: "0.24.0" }),
      );
      return;
    }
    if (!authorized(request)) {
      response.writeHead(401).end('{"error":"unauthenticated"}');
      return;
    }
    if (request.url?.startsWith("/api/auth/ws-ticket") && request.method === "POST") {
      if (options.ticketStatus !== undefined) {
        response.writeHead(options.ticketStatus).end('{"error":"not_gated"}');
        return;
      }
      const ticket = randomBytes(32).toString("base64url");
      minted.push(ticket);
      live.add(ticket);
      response.end(JSON.stringify({ ticket, purpose: "web-rpc", expires_in: 30 }));
      return;
    }
    response.writeHead(options.configStatus ?? 200).end("{}");
  });
  srv.on("upgrade", (request, socket: net.Socket) => {
    const offered = String(request.headers["sec-websocket-protocol"] ?? "")
      .split(",")
      .map((p) => p.trim());
    seen.push(
      `WS ${request.url} ${offered.join(" ")} origin=${String(request.headers.origin ?? "")}`,
    );
    const accept = createHash("sha1")
      .update(`${String(request.headers["sec-websocket-key"])}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`)
      .digest("base64");
    socket.write(
      `HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\nSec-WebSocket-Protocol: indus-dashboard-v1\r\n\r\n`,
    );
    const ticket = offered.find((p) => p.startsWith("indus-dashboard-ticket."))?.slice(23) ?? "";
    const good = live.delete(ticket); // single use
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
  return { url: `http://127.0.0.1:${(srv.address() as net.AddressInfo).port}`, minted, seen };
}
