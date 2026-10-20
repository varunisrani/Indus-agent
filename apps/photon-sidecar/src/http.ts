// The sidecar's loopback HTTP API (routes and error codes in docs/notes/P16/photon.md).
//
// Every request is checked in this order: the Host header names a loopback host (no DNS rebinding
// from a browser page), the bearer token matches (constant time), then the route. Bodies are JSON,
// at most 64 KiB, refused before parsing when larger. `GET /events` is a long-lived NDJSON stream:
// the kept events after the caller's cursor, then live ones, with an empty heartbeat line.

import { createHash, timingSafeEqual } from "node:crypto";
import { lstat } from "node:fs/promises";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { isAbsolute } from "node:path";
import type { LinkErrorCode, SpectrumLink } from "./link.ts";
import { LinkError } from "./link.ts";
import { MAX_MEDIA_BYTES, type Relay, type SidecarEvent } from "./relay.ts";

export const LOOPBACK_HOSTS: ReadonlySet<string> = new Set(["127.0.0.1", "::1", "localhost"]);
export const MAX_BODY_BYTES = 64 * 1024;
export const HEARTBEAT_MS = 15_000;
const MAX_SPACE = 256;
const ROUTES: ReadonlySet<string> = new Set([
  "/health",
  "/events",
  "/media",
  "/send",
  "/send-link",
  "/send-file",
  "/typing",
  "/react",
  "/unreact",
  "/read",
]);

const STATUS: Record<LinkErrorCode, number> = {
  bad_request: 400,
  forbidden: 403,
  target_not_allowed: 403,
  auth_failed: 403,
  space_not_found: 404,
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
  relay: Relay;
  /** The provider's link once it exists (null while connecting). */
  link: () => SpectrumLink | null;
  version?: string;
  compare?: (given: string, expected: string) => boolean;
  heartbeatMs?: number;
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
  if (raw.startsWith("[")) {
    const end = raw.indexOf("]");
    return raw.slice(1, end < 0 ? undefined : end);
  }
  const colon = raw.lastIndexOf(":");
  return colon < 0 ? raw : raw.slice(0, colon);
}

function reply(
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

function field(body: Body, name: string, { required = true } = {}): string {
  const value = body[name];
  if (value === undefined || value === null || value === "") {
    if (required) throw new HttpError(400, "bad_request");
    return "";
  }
  if (typeof value !== "string") throw new HttpError(400, "bad_request");
  return value;
}

function spaceOf(body: Body): string {
  const space = field(body, "space");
  // biome-ignore lint/suspicious/noControlCharactersInRegex: control characters are what we refuse
  if (space.length > MAX_SPACE || /[\u0000-\u001f\u007f]/.test(space)) {
    throw new HttpError(400, "bad_request");
  }
  return space;
}

function httpUrl(text: string): string {
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    throw new HttpError(400, "bad_request");
  }
  if ((url.protocol !== "http:" && url.protocol !== "https:") || !url.hostname) {
    throw new HttpError(400, "bad_request");
  }
  return text;
}

/** A file we may send: absolute, a regular file (never a symlink), at most 100 MiB. */
async function sendablePath(path: string): Promise<string> {
  if (!isAbsolute(path)) throw new HttpError(400, "bad_request");
  let info: Awaited<ReturnType<typeof lstat>>;
  try {
    info = await lstat(path);
  } catch {
    throw new HttpError(400, "bad_request");
  }
  if (!info.isFile()) throw new HttpError(403, "forbidden");
  if (info.size > MAX_MEDIA_BYTES) throw new HttpError(413, "too_large");
  return path;
}

export function createSidecarServer(options: ServerOptions): Server {
  const compare = options.compare ?? tokenMatches;
  const heartbeat = options.heartbeatMs ?? HEARTBEAT_MS;
  const relay = options.relay;

  const ready = (): SpectrumLink => {
    const link = options.link();
    if (relay.status === "auth_failed") throw new LinkError("auth_failed");
    if (link === null || (relay.status !== "open" && relay.status !== "degraded")) {
      throw new LinkError("not_connected");
    }
    return link;
  };

  const stream = (res: ServerResponse, url: URL): void => {
    const after = Number(url.searchParams.get("after") ?? "0");
    const backlog = relay.after(
      Number.isInteger(after) && after >= 0 ? after : 0,
      url.searchParams.get("epoch") ?? "",
    );
    res.writeHead(200, {
      "content-type": "application/x-ndjson; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
      "x-photon-epoch": relay.epoch,
    });
    const write = (event: SidecarEvent): void => {
      res.write(`${JSON.stringify(event)}\n`);
    };
    for (const event of backlog) write(event);
    const last = backlog.at(-1)?.seq ?? 0;
    const stop = relay.subscribe((event) => {
      if (event.seq > last) write(event);
    });
    const beat = setInterval(() => res.write("\n"), heartbeat);
    res.on("close", () => {
      clearInterval(beat);
      stop();
    });
  };

  const route = async (req: IncomingMessage, res: ServerResponse, url: URL): Promise<void> => {
    switch (`${req.method ?? ""} ${url.pathname}`) {
      case "GET /health":
        return reply(res, 200, {
          status: relay.status,
          profile: options.profile,
          epoch: relay.epoch,
          version: options.version ?? "0",
          kept: relay.kept,
          link: { state: relay.status, since: relay.since, detail: relay.detail },
        });
      case "GET /events":
        return stream(res, url);
      case "GET /media": {
        const ref = url.searchParams.get("ref") ?? "";
        if (!/^[0-9a-f]{24}$/.test(ref)) throw new HttpError(400, "bad_request");
        const data = await relay.readMedia(ref);
        res.writeHead(200, {
          "content-type": "application/octet-stream",
          "content-length": data.byteLength,
          "cache-control": "no-store",
          "x-content-type-options": "nosniff",
        });
        res.end(data);
        return;
      }
      case "POST /send": {
        const body = await readJson(req);
        const space = spaceOf(body);
        const text = field(body, "text");
        const format = field(body, "format", { required: false }) || "markdown";
        if (format !== "markdown" && format !== "plain") throw new HttpError(400, "bad_request");
        return reply(res, 200, { id: await ready().sendText(space, text, format) });
      }
      case "POST /send-link": {
        const body = await readJson(req);
        const space = spaceOf(body);
        const url = httpUrl(field(body, "url"));
        return reply(res, 200, { id: await ready().sendLink(space, url) });
      }
      case "POST /send-file": {
        const body = await readJson(req);
        const space = spaceOf(body);
        const path = await sendablePath(field(body, "path"));
        const name = field(body, "name", { required: false });
        const mime = field(body, "mime", { required: false });
        const id = await ready().sendFile(space, {
          path,
          voice: body.voice === true,
          ...(name ? { name } : {}),
          ...(mime ? { mime } : {}),
        });
        return reply(res, 200, { id });
      }
      case "POST /typing": {
        const body = await readJson(req);
        await ready().typing(spaceOf(body), body.on !== false);
        return reply(res, 200, { ok: true });
      }
      case "POST /react": {
        const body = await readJson(req);
        const emoji = field(body, "emoji");
        if (emoji.length > 16) throw new HttpError(400, "bad_request");
        await ready().react(spaceOf(body), field(body, "id"), emoji);
        return reply(res, 200, { ok: true });
      }
      case "POST /unreact": {
        const body = await readJson(req);
        await ready().unreact(spaceOf(body), field(body, "id"));
        return reply(res, 200, { ok: true });
      }
      case "POST /read": {
        const body = await readJson(req);
        await ready().markRead(spaceOf(body), field(body, "id"));
        return reply(res, 200, { ok: true });
      }
      default:
        throw ROUTES.has(url.pathname)
          ? new HttpError(405, "method_not_allowed")
          : new HttpError(404, "not_found");
    }
  };

  return createServer((req, res) => {
    const reject = (error: HttpError): void => {
      req.resume();
      reply(res, error.status, { error: error.code }, error.headers);
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
      const failure = error instanceof LinkError ? error : new LinkError("failed");
      const headers: Record<string, string> = {};
      if (failure.code === "rate_limited") {
        headers["retry-after"] = String(Math.ceil(failure.retryAfter ?? 30));
      }
      if (
        failure.code === "not_connected" &&
        (relay.status === "connecting" || relay.status === "starting")
      ) {
        headers["retry-after"] = "5";
      }
      reject(new HttpError(STATUS[failure.code], failure.code, headers));
    });
  });
}

/** Bind on a loopback host only; anything else is refused before a socket is opened. */
export function listen(server: Server, host: string, port: number): Promise<number> {
  if (!isLoopbackHost(host)) {
    return Promise.reject(new Error("the sidecar binds loopback addresses only"));
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
