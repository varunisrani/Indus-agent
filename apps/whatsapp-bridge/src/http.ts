// The bridge's loopback HTTP API (routes and error codes in docs/notes/P16/whatsapp-bridge-sidecar.md).
//
// Every request is checked in this order: the Host header names a loopback host (a page in a
// browser cannot reach us through DNS rebinding), the bearer token matches (constant time), then
// the route. Bodies are JSON, at most 64 KiB, refused before parsing when larger.

import { createHash, timingSafeEqual } from "node:crypto";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { MessageRef, OutboundKind, WaErrorCode } from "./provider.ts";
import { WaError } from "./provider.ts";
import type { Session } from "./session.ts";

export const LOOPBACK_HOSTS: ReadonlySet<string> = new Set(["127.0.0.1", "::1", "localhost"]);
export const MAX_BODY_BYTES = 64 * 1024;
export const MAX_WAIT_MS = 25_000;
const OUTBOUND_KINDS: ReadonlySet<string> = new Set([
  "image",
  "video",
  "audio",
  "voice",
  "document",
]);

const STATUS: Record<WaErrorCode, number> = {
  bad_request: 400,
  forbidden: 403,
  chat_not_found: 404,
  message_not_found: 404,
  too_large: 413,
  rate_limited: 429,
  failed: 502,
  not_connected: 503,
  timeout: 504,
};

export function isLoopbackHost(host: string): boolean {
  return LOOPBACK_HOSTS.has(
    host
      .trim()
      .toLowerCase()
      .replace(/^\[|\]$/g, ""),
  );
}

/** Constant-time token check: both sides hashed first, so neither length nor content leaks. */
export function tokenMatches(given: string, expected: string): boolean {
  const a = createHash("sha256").update(given, "utf8").digest();
  const b = createHash("sha256").update(expected, "utf8").digest();
  return timingSafeEqual(a, b) && expected.length > 0;
}

export interface ServerOptions {
  token: string;
  profile: string;
  version?: string;
  compare?: (given: string, expected: string) => boolean;
  maxWaitMs?: number;
}

class HttpError extends Error {
  readonly status: number;
  readonly code: string;
  readonly headers: Record<string, string>;

  constructor(status: number, code: string, headers: Record<string, string> = {}) {
    super(code);
    this.status = status;
    this.code = code;
    this.headers = headers;
  }
}

type Body = Record<string, unknown>;

function hostOnly(header: string | undefined): string {
  const raw = (header ?? "").trim();
  if (raw.startsWith("[")) return raw.slice(1, raw.indexOf("]") < 0 ? undefined : raw.indexOf("]"));
  const colon = raw.lastIndexOf(":");
  return colon < 0 ? raw : raw.slice(0, colon);
}

function send(
  res: ServerResponse,
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): void {
  const text = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(text),
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    ...headers,
  });
  res.end(text);
}

async function readJson(req: IncomingMessage): Promise<Body> {
  const declared = Number(req.headers["content-length"] ?? "0");
  if (Number.isFinite(declared) && declared > MAX_BODY_BYTES) throw new HttpError(413, "too_large");
  const chunks: Buffer[] = [];
  let total = 0;
  for await (const chunk of req) {
    const piece = chunk as Buffer;
    total += piece.byteLength;
    if (total > MAX_BODY_BYTES) throw new HttpError(413, "too_large");
    chunks.push(piece);
  }
  try {
    const parsed: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error();
    return parsed as Body;
  } catch {
    throw new HttpError(400, "bad_request");
  }
}

function text(body: Body, name: string, { required = true } = {}): string {
  const value = body[name];
  if (value === undefined || value === null || value === "") {
    if (required) throw new HttpError(400, "bad_request");
    return "";
  }
  if (typeof value !== "string") throw new HttpError(400, "bad_request");
  return value;
}

function chatOf(body: Body): string {
  const chat = text(body, "chat");
  if (!chat.includes("@") || chat.length > 256) throw new HttpError(400, "bad_request");
  return chat;
}

function refOf(body: Body, idField: string): MessageRef {
  const id = text(body, idField);
  const participant = text(body, "participant", { required: false });
  return { id, fromMe: body.fromMe === true, participant: participant || null };
}

function replyRef(body: Body): MessageRef | undefined {
  const id = text(body, "replyTo", { required: false });
  if (!id) return undefined;
  const participant = text(body, "replyToParticipant", { required: false });
  return { id, fromMe: body.replyToFromMe === true, participant: participant || null };
}

export function createBridgeServer(session: Session, options: ServerOptions): Server {
  const compare = options.compare ?? tokenMatches;
  const maxWait = options.maxWaitMs ?? MAX_WAIT_MS;

  const route = async (req: IncomingMessage, res: ServerResponse, url: URL): Promise<void> => {
    const key = `${req.method ?? ""} ${url.pathname}`;
    switch (key) {
      case "GET /health":
        return send(res, 200, {
          status: session.status,
          profile: options.profile,
          epoch: session.epoch,
          self: session.self,
          queued: session.queued,
          version: options.version ?? "0",
        });
      case "GET /events": {
        const after = Number(url.searchParams.get("after") ?? "0");
        const wait = Number(url.searchParams.get("wait") ?? String(maxWait / 1000));
        const waitMs = Number.isFinite(wait)
          ? Math.min(Math.max(wait, 0) * 1000, maxWait)
          : maxWait;
        const aborted = new AbortController();
        res.on("close", () => aborted.abort());
        const page = await session.events(
          Number.isInteger(after) && after >= 0 ? after : 0,
          url.searchParams.get("epoch") ?? "",
          waitMs,
          aborted.signal,
        );
        if (!res.writableEnded && !res.destroyed) send(res, 200, page);
        return;
      }
      case "GET /media": {
        const ref = url.searchParams.get("ref") ?? "";
        if (!/^[0-9a-f]{24}$/.test(ref)) throw new HttpError(400, "bad_request");
        const found = await session.media(ref);
        res.writeHead(200, {
          "content-type": "application/octet-stream",
          "content-length": found.data.byteLength,
          "cache-control": "no-store",
          "x-content-type-options": "nosniff",
        });
        res.end(found.data);
        return;
      }
      case "POST /send": {
        const body = await readJson(req);
        const id = await session.sendText(chatOf(body), text(body, "text"), replyRef(body));
        return send(res, 200, { id });
      }
      case "POST /send-media": {
        const body = await readJson(req);
        const kind = text(body, "kind");
        if (!OUTBOUND_KINDS.has(kind)) throw new HttpError(400, "bad_request");
        const caption = text(body, "caption", { required: false });
        const fileName = text(body, "fileName", { required: false });
        const mime = text(body, "mime", { required: false });
        const id = await session.sendMedia(
          chatOf(body),
          {
            kind: kind as OutboundKind,
            path: text(body, "path"),
            ...(caption ? { caption } : {}),
            ...(fileName ? { fileName } : {}),
            ...(mime ? { mime } : {}),
          },
          replyRef(body),
        );
        return send(res, 200, { id });
      }
      case "POST /typing": {
        const body = await readJson(req);
        const state = text(body, "state", { required: false }) || "composing";
        if (state !== "composing" && state !== "paused") throw new HttpError(400, "bad_request");
        await session.typing(chatOf(body), state);
        return send(res, 200, { ok: true });
      }
      case "POST /react": {
        const body = await readJson(req);
        const emoji = typeof body.emoji === "string" ? body.emoji : "";
        if (emoji.length > 16) throw new HttpError(400, "bad_request");
        await session.react(chatOf(body), refOf(body, "id"), emoji);
        return send(res, 200, { ok: true });
      }
      case "POST /read": {
        const body = await readJson(req);
        await session.read(chatOf(body), refOf(body, "id"));
        return send(res, 200, { ok: true });
      }
      default: {
        const known = [
          "/health",
          "/events",
          "/media",
          "/send",
          "/send-media",
          "/typing",
          "/react",
          "/read",
        ];
        throw new HttpError(
          known.includes(url.pathname) ? 405 : 404,
          known.includes(url.pathname) ? "method_not_allowed" : "not_found",
        );
      }
    }
  };

  return createServer((req, res) => {
    const reject = (error: HttpError): void => {
      req.resume();
      send(res, error.status, { error: error.code }, error.headers);
    };
    if (!isLoopbackHost(hostOnly(req.headers.host))) {
      reject(new HttpError(400, "bad_host"));
      return;
    }
    const auth = req.headers.authorization ?? "";
    const given = auth.startsWith("Bearer ") ? auth.slice(7) : "";
    if (!compare(given, options.token)) {
      reject(new HttpError(401, "unauthorized", { "www-authenticate": "Bearer" }));
      return;
    }
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    route(req, res, url).catch((error: unknown) => {
      if (res.headersSent) {
        res.destroy();
        return;
      }
      if (error instanceof HttpError) {
        reject(error);
        return;
      }
      const wa = error instanceof WaError ? error : new WaError("failed");
      const headers: Record<string, string> = {};
      if (wa.code === "rate_limited")
        headers["retry-after"] = String(Math.ceil(wa.retryAfter ?? 30));
      if (wa.code === "not_connected" && session.status === "connecting")
        headers["retry-after"] = "5";
      reject(new HttpError(STATUS[wa.code], wa.code, headers));
    });
  });
}

/** Bind on a loopback host only; anything else is refused before a socket is opened. */
export function listen(server: Server, host: string, port: number): Promise<number> {
  if (!isLoopbackHost(host)) {
    return Promise.reject(new Error("the bridge binds loopback addresses only"));
  }
  return new Promise((resolve, reject) => {
    const onError = (error: Error): void => reject(error);
    server.once("error", onError);
    server.listen(port, host === "localhost" ? "127.0.0.1" : host, () => {
      server.off("error", onError);
      const address = server.address();
      resolve(typeof address === "object" && address !== null ? address.port : port);
    });
  });
}
