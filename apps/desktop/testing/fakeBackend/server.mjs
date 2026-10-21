// The fake Indus backend's long-lived half (P20-01 fixture, used by P20-01..17 tests). One process
// per role, started detached by ensure.mjs:
//
//   --role daemon  the profile daemon: an owner-only control socket `<home>/gateway.sock` speaking
//                  the P15-27 wire (one JSON line in, one out: identify, describe, session-ticket,
//                  stop, plus fake-* verbs for tests), and a loopback WebSocket `/ws` that accepts
//                  a ticket exactly once (offered as `indus-gateway-ticket.<t>` beside
//                  `indus-gateway-v1`; anything else is closed 4401, a disallowed Origin 4403) and
//                  answers JSON-RPC through @indus/client's FakeServer, one per connection.
//   --role serve   the headless `indus serve`: REST `/api/*` gated on the session token from
//                  INDUS_DASHBOARD_SESSION_TOKEN (X-Indus-Session-Token or Authorization: Bearer);
//                  `/api/status` is public.
//
// Behaviour comes from a scenario JSON (see scenarios/); state files live in the home:
// `fake-daemon.json` / `fake-serve.json` (written once listening), logs in `<home>/logs/`.
// POSIX only (Unix control socket), like the real daemon (D-16.55).

import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import path from "node:path";
import { FakeServer } from "@indus/client/testing";
import { socketPathFits } from "./control.mjs";

const GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
const SUBPROTOCOL = "indus-gateway-v1";
const TICKET_PREFIX = "indus-gateway-ticket.";
const MAX_FRAME = 16 * 1024 * 1024;

function arg(name, fallback = "") {
  const at = process.argv.indexOf(`--${name}`);
  return at >= 0 ? (process.argv[at + 1] ?? fallback) : fallback;
}

const role = arg("role", "daemon");
const home = path.resolve(arg("home"));
const scenarioPath = arg("scenario");
const scenario = JSON.parse(fs.readFileSync(scenarioPath, "utf8"));
const startIndex = Number(arg("start", "0"));
const profileId = createHash("sha256").update(home).digest("hex").slice(0, 16);
const instanceId = randomBytes(8).toString("hex");
const versions = scenario.daemonVersions ?? [scenario.version ?? "0.20.0"];
const codeVersion = versions[Math.min(startIndex, versions.length - 1)];

function writeState(file, value) {
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, `${JSON.stringify(value)}\n`, { mode: 0o600 });
  fs.renameSync(tmp, file);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// -- tickets -------------------------------------------------------------------------------------

const tickets = new Map(); // sha256 → { expiresAt, used }
let mints = 0;

function digest(ticket) {
  return createHash("sha256").update(ticket).digest("hex");
}

function mintTicket(purpose) {
  if (purpose !== "ws-interactive")
    return { ok: false, error: "bad_purpose", detail: "unknown ticket purpose" };
  // Counted per home, across daemon starts: "the first N mints for this profile fail" (a restart
  // does not re-arm the failure).
  const counter = path.join(home, "fake-ticket-mints.json");
  try {
    mints = JSON.parse(fs.readFileSync(counter, "utf8")).count ?? mints;
  } catch {
    // first mint for this home
  }
  mints += 1;
  writeState(counter, { count: mints });
  const failFirst = scenario.tickets?.failFirst ?? 0;
  if (mints <= failFirst) {
    return {
      ok: false,
      error: scenario.tickets?.failError ?? "stale_socket",
      detail: "scripted failure",
    };
  }
  const ticket = randomBytes(32).toString("base64url");
  const ttl = scenario.tickets?.ttlMs ?? 30_000;
  tickets.set(digest(ticket), { expiresAt: Date.now() + ttl, used: false });
  return {
    ok: true,
    result: {
      ticket,
      ws_url: wsUrl(),
      profile_id: profileId,
      instance_id: instanceId,
      expires_in: ttl / 1000,
    },
  };
}

/** "ok", or why the offered ticket is refused; the ticket is spent either way. */
function redeem(ticket) {
  const entry = tickets.get(digest(ticket));
  if (entry === undefined) return "unknown";
  if (entry.used) return "used";
  entry.used = true;
  if (Date.now() >= entry.expiresAt) return "expired";
  return "ok";
}

// -- minimal RFC 6455 server side ----------------------------------------------------------------

function encodeFrame(opcode, payload) {
  const body = Buffer.isBuffer(payload) ? payload : Buffer.from(payload);
  let header;
  if (body.length < 126) {
    header = Buffer.from([0x80 | opcode, body.length]);
  } else if (body.length < 65_536) {
    header = Buffer.alloc(4);
    header[0] = 0x80 | opcode;
    header[1] = 126;
    header.writeUInt16BE(body.length, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x80 | opcode;
    header[1] = 127;
    header.writeBigUInt64BE(BigInt(body.length), 2);
  }
  return Buffer.concat([header, body]);
}

function closePayload(code, reason) {
  const out = Buffer.alloc(2 + Buffer.byteLength(reason));
  out.writeUInt16BE(code, 0);
  out.write(reason, 2);
  return out;
}

class Peer {
  constructor(socket, onText, onGone) {
    this.socket = socket;
    this.onText = onText;
    this.onGone = onGone;
    this.buffer = Buffer.alloc(0);
    this.fragments = [];
    this.closed = false;
    socket.on("data", (chunk) => this.read(chunk));
    socket.on("close", () => this.gone());
    socket.on("error", () => this.gone());
  }

  sendText(text) {
    if (!this.closed) this.socket.write(encodeFrame(0x1, text));
  }

  close(code = 1000, reason = "") {
    if (this.closed) return;
    this.closed = true;
    this.socket.end(encodeFrame(0x8, closePayload(code, reason)));
    setTimeout(() => this.socket.destroy(), 200).unref();
    this.onGone?.();
    this.onGone = null;
  }

  gone() {
    this.closed = true;
    this.onGone?.();
    this.onGone = null;
  }

  read(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    for (;;) {
      if (this.buffer.length < 2) return;
      const first = this.buffer[0];
      const second = this.buffer[1];
      const masked = (second & 0x80) !== 0;
      let length = second & 0x7f;
      let offset = 2;
      if (length === 126) {
        if (this.buffer.length < 4) return;
        length = this.buffer.readUInt16BE(2);
        offset = 4;
      } else if (length === 127) {
        if (this.buffer.length < 10) return;
        length = Number(this.buffer.readBigUInt64BE(2));
        offset = 10;
      }
      if (length > MAX_FRAME) {
        this.close(1009, "message too big");
        return;
      }
      const maskAt = offset;
      if (masked) offset += 4;
      if (this.buffer.length < offset + length) return;
      let payload = this.buffer.subarray(offset, offset + length);
      if (masked) {
        const mask = this.buffer.subarray(maskAt, maskAt + 4);
        payload = Buffer.from(payload.map((byte, i) => byte ^ (mask[i % 4] ?? 0)));
      }
      this.buffer = this.buffer.subarray(offset + length);
      const opcode = first & 0x0f;
      const fin = (first & 0x80) !== 0;
      if (opcode === 0x8) {
        this.close(1000, "");
        return;
      }
      if (opcode === 0x9) {
        this.socket.write(encodeFrame(0xa, payload));
        continue;
      }
      if (opcode === 0xa) continue;
      if (opcode === 0x1 || opcode === 0x0) {
        this.fragments.push(payload);
        if (fin) {
          const text = Buffer.concat(this.fragments).toString("utf8");
          this.fragments = [];
          this.onText(text);
        }
      }
    }
  }
}

// -- daemon role ---------------------------------------------------------------------------------

let wsPort = 0;
const peers = new Set();
const received = [];

function wsUrl() {
  return `ws://127.0.0.1:${wsPort}/ws`;
}

function rpcServer() {
  const fake = new FakeServer();
  for (const [method, answer] of Object.entries(scenario.rpc?.methods ?? {})) {
    fake.handle(method, async (params) => {
      if (answer.delayMs) await sleep(answer.delayMs);
      if (answer.error) {
        const { FakeRpcError } = await import("@indus/client/testing");
        throw new FakeRpcError(answer.error.code, answer.error.message);
      }
      return answer.result ?? { ok: true, echo: params };
    });
  }
  return fake;
}

function acceptWebSocket(request, socket) {
  const offered = String(request.headers["sec-websocket-protocol"] ?? "")
    .split(",")
    .map((p) => p.trim())
    .filter(Boolean);
  const key = String(request.headers["sec-websocket-key"] ?? "");
  const url = new URL(request.url ?? "/", "http://127.0.0.1");
  if (url.pathname !== "/ws" || key === "") {
    socket.end("HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n");
    return;
  }
  const accept = createHash("sha1")
    .update(key + GUID)
    .digest("base64");
  const echo = offered.includes(SUBPROTOCOL) ? `Sec-WebSocket-Protocol: ${SUBPROTOCOL}\r\n` : "";
  socket.write(
    `HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n${echo}\r\n`,
  );
  const ticketOffers = offered.filter((p) => p.startsWith(TICKET_PREFIX));
  const origin = request.headers.origin;
  const allowedOrigins = scenario.ws?.allowedOrigins ?? [];
  let refusal = null;
  if (origin !== undefined && !allowedOrigins.includes(origin))
    refusal = [4403, "origin not allowed"];
  for (const offer of ticketOffers) {
    const verdict = redeem(offer.slice(TICKET_PREFIX.length));
    if (verdict !== "ok" && refusal === null) refusal = [4401, `ticket ${verdict}`];
  }
  if (
    refusal === null &&
    (ticketOffers.length !== 1 || !offered.includes(SUBPROTOCOL) || url.search !== "")
  ) {
    refusal = [4401, "ticket missing"];
  }
  const peer = new Peer(socket, () => {}, null);
  if (refusal !== null) {
    peer.close(refusal[0], refusal[1]);
    return;
  }
  const fake = rpcServer();
  const transport = fake.connect();
  const record = { fake, peer };
  peers.add(record);
  transport.onFrame((frame) => peer.sendText(JSON.stringify(frame)));
  transport.onClose(() => peer.close(1001, "going away"));
  peer.onText = (text) => {
    try {
      const frame = JSON.parse(text);
      received.push(frame);
      transport.send(frame);
    } catch {
      peer.close(1003, "not JSON");
    }
  };
  peer.onGone = () => {
    peers.delete(record);
    transport.close();
  };
  if (scenario.rpc?.readyOnConnect !== false) fake.ready({ protocol_version: 1, heartbeat: false });
}

function controlAnswer(request) {
  const id = request?.id ?? null;
  const reply = (body) => ({ protocol: 1, id, ...body });
  const descriptor = {
    ws_url: wsUrl(),
    profile_id: profileId,
    instance_id: instanceId,
    code_version: codeVersion,
  };
  switch (request?.verb) {
    case "identify":
      return reply({ ok: true, result: { ...descriptor, pid: process.pid, home } });
    case "describe":
      return reply({ ok: true, result: descriptor });
    case "session-ticket":
      return reply(mintTicket(request.params?.purpose));
    case "stop":
      setTimeout(() => shutdown(0), 10);
      return reply({ ok: true, result: { stopping: true } });
    case "fake-emit":
      for (const { fake } of peers) fake.emit(request.params?.event ?? { type: "status.update" });
      return reply({ ok: true, result: { delivered: peers.size } });
    case "fake-drop":
      for (const { fake } of [...peers]) fake.die("dropped by test");
      return reply({ ok: true, result: { dropped: true } });
    case "fake-received":
      return reply({ ok: true, result: { frames: received } });
    default:
      return reply({ ok: false, error: "unknown_verb", detail: String(request?.verb) });
  }
}

let controlServer = null;
let httpServer = null;
const stateFile = path.join(home, role === "daemon" ? "fake-daemon.json" : "fake-serve.json");

function shutdown(code) {
  try {
    fs.rmSync(stateFile, { force: true });
    if (role === "daemon") fs.rmSync(path.join(home, "gateway.sock"), { force: true });
  } catch {
    // best effort
  }
  for (const { peer } of peers) peer.close(1001, "daemon stopping");
  controlServer?.close();
  httpServer?.close();
  setTimeout(() => process.exit(code), 20).unref();
}

process.on("SIGTERM", () => shutdown(0));
process.on("SIGINT", () => shutdown(0));

async function startDaemon() {
  httpServer = http.createServer((_req, res) => {
    res.writeHead(404).end();
  });
  httpServer.on("upgrade", (request, socket) => acceptWebSocket(request, socket));
  await new Promise((resolve) => httpServer.listen(0, "127.0.0.1", resolve));
  wsPort = httpServer.address().port;

  const sock = path.join(home, "gateway.sock");
  fs.rmSync(sock, { force: true });
  // A Unix socket path holds about 100 bytes. The real daemon then binds in a short private
  // directory and leaves a pointer file; the fake binds by a relative name from the home instead
  // (the process runs with the home as cwd), so even a long test home keeps every file inside it.
  const bindPath = socketPathFits(sock) ? sock : "gateway.sock";
  controlServer = net.createServer((conn) => {
    let data = "";
    conn.setEncoding("utf8");
    conn.on("data", (chunk) => {
      data += chunk;
      const newline = data.indexOf("\n");
      if (newline < 0) return;
      let answer;
      try {
        answer = controlAnswer(JSON.parse(data.slice(0, newline)));
      } catch {
        answer = { ok: false, protocol: 1, id: null, error: "bad_request", detail: "not JSON" };
      }
      conn.end(`${JSON.stringify(answer)}\n`);
    });
    conn.on("error", () => {});
  });
  await new Promise((resolve) => controlServer.listen(bindPath, resolve));
  fs.chmodSync(sock, 0o600);
  writeState(stateFile, {
    pid: process.pid,
    ws_url: wsUrl(),
    profile_id: profileId,
    instance_id: instanceId,
    code_version: codeVersion,
    control_socket: sock,
  });
}

// -- serve role ----------------------------------------------------------------------------------

const sessionToken = process.env.INDUS_DASHBOARD_SESSION_TOKEN ?? "";

function tokenOk(request) {
  if (sessionToken === "") return false;
  const header = request.headers["x-indus-session-token"];
  const bearer = /^Bearer\s+(.+)$/i.exec(String(request.headers.authorization ?? ""))?.[1];
  const offered = typeof header === "string" ? header : (bearer ?? "");
  const a = Buffer.from(offered);
  const b = Buffer.from(sessionToken);
  return a.length === b.length && timingSafeEqual(a, b);
}

const routeHits = new Map();

async function serveRequest(request, response) {
  const url = new URL(request.url ?? "/", "http://127.0.0.1");
  const send = (status, body) => {
    response.writeHead(status, { "content-type": "application/json" });
    response.end(JSON.stringify(body));
  };
  if (request.method === "GET" && url.pathname === "/api/status") {
    send(200, { status: "ok", version: codeVersion, profile_id: profileId, auth: "session-token" });
    return;
  }
  if (!url.pathname.startsWith("/api/")) {
    send(404, { detail: "not found" });
    return;
  }
  if (!tokenOk(request)) {
    send(401, { detail: "session token required" });
    return;
  }
  const key = `${request.method} ${url.pathname}`;
  // every authorised call is written down for the end-to-end suite (P20-40)
  try {
    fs.appendFileSync(path.join(home, "fake-rest.log"), `${key}${url.search}\n`);
  } catch {
    // the home went away: nothing to record
  }
  const route = scenario.rest?.routes?.[key];
  if (route === undefined) {
    send(404, { detail: "not found" });
    return;
  }
  const hits = (routeHits.get(key) ?? 0) + 1;
  routeHits.set(key, hits);
  if (route.delayMs) await sleep(route.delayMs);
  if (route.failTimes && hits <= route.failTimes) {
    send(route.failStatus ?? 503, { detail: "scripted failure" });
    return;
  }
  send(route.status ?? 200, route.body ?? {});
}

async function startServe() {
  httpServer = http.createServer((request, response) => {
    serveRequest(request, response).catch(() => {
      response.writeHead(500).end();
    });
  });
  await new Promise((resolve) => httpServer.listen(0, "127.0.0.1", resolve));
  const port = httpServer.address().port;
  writeState(stateFile, {
    pid: process.pid,
    url: `http://127.0.0.1:${port}`,
    port,
    profile_id: profileId,
    code_version: codeVersion,
    token_sha256: createHash("sha256").update(sessionToken).digest("hex"),
  });
}

if (scenario.startupDelayMs) await sleep(scenario.startupDelayMs);
await (role === "serve" ? startServe() : startDaemon());
