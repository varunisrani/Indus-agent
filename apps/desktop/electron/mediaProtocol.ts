// The `indus-media://` scheme (P20-34): media for the app's own pages, never arbitrary files.
//
//   indus-media://stream/<percent-encoded absolute path>
//
// Local connection: the file is read from disk when its real path (symlinks resolved) lies inside
// an allowed root, is not a secret file (the guard the REST proxy and fs IPC share) and is a type
// we serve (images except SVG, audio, video). `Range` is honoured: 206 with `Content-Range`, 416
// when it cannot be satisfied, 200 without one; HEAD answers headers only. Remote connection: the
// path lives on the remote host (R08.23); main mints a single-use download ticket with its own
// credential and streams the remote file, forwarding only range and cache headers — the page sees
// neither the token nor the ticket.

import { createReadStream, promises as fsp } from "node:fs";
import path from "node:path";
import { Readable } from "node:stream";
import { buildApiUrl } from "./api/proxy.ts";
import { SESSION_TOKEN_HEADER } from "./backend/localAttach.ts";
import { MEDIA_SCHEME } from "./security/csp.ts";
import { isSensitivePath } from "./security/sensitiveFiles.ts";

export const MEDIA_HOST = "stream";

const MEDIA_TYPES: Readonly<Record<string, string>> = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".avif": "image/avif",
  ".bmp": "image/bmp",
  ".ico": "image/x-icon",
  ".mp3": "audio/mpeg",
  ".m4a": "audio/mp4",
  ".aac": "audio/aac",
  ".wav": "audio/wav",
  ".flac": "audio/flac",
  ".ogg": "audio/ogg",
  ".oga": "audio/ogg",
  ".opus": "audio/ogg",
  ".mp4": "video/mp4",
  ".m4v": "video/mp4",
  ".webm": "video/webm",
  ".mov": "video/quicktime",
  ".ogv": "video/ogg",
  ".mkv": "video/x-matroska",
};

/** The content type we serve `file` as, or null for a type the scheme refuses (SVG included). */
export function mediaTypeFor(file: string): string | null {
  return MEDIA_TYPES[path.extname(file).toLowerCase()] ?? null;
}

/** `indus-media://stream/<path>` for `file` (what a page puts in `src`). */
export function mediaUrl(file: string): string {
  return `${MEDIA_SCHEME}://${MEDIA_HOST}/${encodeURIComponent(file)}`;
}

/** The absolute path a media URL names, or null. */
export function parseMediaUrl(raw: string): string | null {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.protocol !== `${MEDIA_SCHEME}:` || url.hostname.toLowerCase() !== MEDIA_HOST) return null;
  let file: string;
  try {
    file = decodeURIComponent(url.pathname.replace(/^\/+/, ""));
  } catch {
    return null;
  }
  if (file === "" || file.includes("\0")) return null;
  const absolute = path.isAbsolute(file) || path.win32.isAbsolute(file);
  return absolute ? file : null;
}

export interface ByteRange {
  start: number;
  /** Inclusive. */
  end: number;
}

/**
 * One `bytes=` range: `a-b`, `a-` or `-n` (the last n bytes). No header, a malformed one or a
 * multi-range request → null (the whole file); a range past the end → "unsatisfiable".
 */
export function parseByteRange(
  header: string | null | undefined,
  size: number,
): ByteRange | null | "unsatisfiable" {
  if (typeof header !== "string") return null;
  const compact = header.replace(/\s+/g, "").toLowerCase();
  if (!compact.startsWith("bytes=") || compact.includes(",")) return null;
  const spec = compact.slice("bytes=".length);
  const dash = spec.indexOf("-");
  if (dash < 0 || spec.indexOf("-", dash + 1) >= 0) return null;
  const from = spec.slice(0, dash);
  const to = spec.slice(dash + 1);
  if (!/^\d*$/.test(from) || !/^\d*$/.test(to) || (from === "" && to === "")) return null;
  if (size <= 0) return "unsatisfiable";
  if (from === "") {
    const last = Math.min(Number(to), size);
    if (!Number.isSafeInteger(last) || last <= 0) return "unsatisfiable";
    return { start: size - last, end: size - 1 };
  }
  const start = Number(from);
  const end = to === "" ? size - 1 : Math.min(Number(to), size - 1);
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end)) return "unsatisfiable";
  if (start >= size || start > end) return "unsatisfiable";
  return { start, end };
}

function within(child: string, root: string, caseInsensitive: boolean): boolean {
  const a = caseInsensitive ? child.toLowerCase() : child;
  const b = caseInsensitive ? root.toLowerCase() : root;
  if (a === b) return true;
  const rel = path.relative(b, a);
  return rel !== "" && !rel.startsWith("..") && !path.isAbsolute(rel);
}

/** Folders the scheme may read from (their real paths are compared, so links cannot escape). */
export class MediaRoots {
  private readonly roots = new Set<string>();
  private readonly caseInsensitive: boolean;

  constructor(initial: readonly string[] = [], platform: string = process.platform) {
    this.caseInsensitive = platform === "win32" || platform === "darwin";
    for (const root of initial) this.add(root);
  }

  add(root: string): void {
    if (root.trim() !== "" && path.isAbsolute(root)) this.roots.add(path.resolve(root));
  }

  remove(root: string): void {
    this.roots.delete(path.resolve(root));
  }

  list(): string[] {
    return [...this.roots];
  }

  /**
   * True when `file` lies inside a root, compared with the root's real path (and its own path:
   * a real path can never sit under a root that is itself a link, so that adds nothing unsafe).
   */
  async allows(file: string): Promise<boolean> {
    for (const root of this.roots) {
      if (within(file, root, this.caseInsensitive)) return true;
      let real: string;
      try {
        real = await fsp.realpath(root);
      } catch {
        continue;
      }
      if (within(file, real, this.caseInsensitive)) return true;
    }
    return false;
  }
}

/** Where media comes from right now (null while nothing is connected). */
export type MediaTarget =
  | { mode: "local" }
  | { mode: "remote"; origin: string; token: string; profile: string | null };

export interface MediaDeps {
  roots: MediaRoots;
  target(): MediaTarget | null;
  fetch?: typeof fetch;
  log?(line: string): void;
}

const FORWARDED_REQUEST_HEADERS = [
  "range",
  "if-range",
  "if-none-match",
  "if-modified-since",
  "accept",
] as const;
const FORWARDED_RESPONSE_HEADERS = [
  "content-type",
  "content-length",
  "content-range",
  "accept-ranges",
  "last-modified",
  "etag",
] as const;

function plain(status: number, text: string, extra: Record<string, string> = {}): Response {
  return new Response(text, {
    status,
    headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store", ...extra },
  });
}

/** The 200 / 206 / 416 answer for a file already allowed; HEAD gets headers only. */
export async function localMediaResponse(
  file: string,
  type: string,
  method: "GET" | "HEAD",
  rangeHeader: string | null,
): Promise<Response> {
  let handle: fsp.FileHandle;
  try {
    handle = await fsp.open(file, "r");
  } catch {
    return plain(404, "media not found");
  }
  let size: number;
  let modified: Date;
  try {
    const stat = await handle.stat();
    if (!stat.isFile()) {
      await handle.close();
      return plain(404, "media not found");
    }
    size = stat.size;
    modified = stat.mtime;
  } catch {
    await handle.close();
    return plain(404, "media not found");
  }
  const base: Record<string, string> = {
    "Accept-Ranges": "bytes",
    "Cache-Control": "no-store",
    "Content-Type": type,
    "Last-Modified": modified.toUTCString(),
    "X-Content-Type-Options": "nosniff",
  };
  const range = parseByteRange(rangeHeader, size);
  if (range === "unsatisfiable") {
    await handle.close();
    return new Response(null, {
      status: 416,
      headers: { ...base, "Content-Range": `bytes */${size}` },
    });
  }
  // The stream owns the handle and closes it; sizes come from the same open file.
  const body = (start: number, end: number) =>
    Readable.toWeb(
      createReadStream(file, { fd: handle, start, end, autoClose: true }),
    ) as unknown as ReadableStream<Uint8Array>;
  if (range === null) {
    const headers = { ...base, "Content-Length": String(size) };
    if (method === "HEAD" || size === 0) {
      await handle.close();
      return new Response(null, { status: 200, headers });
    }
    return new Response(body(0, size - 1), { status: 200, headers });
  }
  const headers = {
    ...base,
    "Content-Length": String(range.end - range.start + 1),
    "Content-Range": `bytes ${range.start}-${range.end}/${size}`,
  };
  if (method === "HEAD") {
    await handle.close();
    return new Response(null, { status: 206, headers });
  }
  return new Response(body(range.start, range.end), { status: 206, headers });
}

async function remoteMedia(
  target: Extract<MediaTarget, { mode: "remote" }>,
  file: string,
  method: "GET" | "HEAD",
  request: Request,
  deps: MediaDeps,
): Promise<Response> {
  const doFetch = deps.fetch ?? fetch;
  let ticket: string;
  try {
    const minted = await doFetch(buildApiUrl(target.origin, "/api/files/download-ticket", null), {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        [SESSION_TOKEN_HEADER]: target.token,
      },
      body: JSON.stringify({ path: file }),
      redirect: "manual",
    });
    if (minted.status === 401 || minted.status === 403) return plain(minted.status, "not allowed");
    if (minted.status === 404) return plain(404, "media not found");
    if (!minted.ok) return plain(502, "the remote could not serve this media");
    const answer = (await minted.json()) as { ticket?: unknown };
    if (typeof answer.ticket !== "string" || answer.ticket === "") {
      return plain(502, "the remote could not serve this media");
    }
    ticket = answer.ticket;
  } catch {
    return plain(502, "the remote could not serve this media");
  }
  const url = buildApiUrl(target.origin, "/api/files/download", null);
  url.searchParams.set("path", file);
  url.searchParams.set("ticket", ticket);
  const headers = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value !== null) headers.set(name, value);
  }
  let upstream: Response;
  try {
    upstream = await doFetch(url, { method, headers, redirect: "manual" });
  } catch {
    return plain(502, "the remote could not serve this media");
  }
  if (upstream.status === 401 || upstream.status === 403 || upstream.status === 404) {
    await upstream.body?.cancel();
    return plain(upstream.status === 404 ? 404 : upstream.status, "media not available");
  }
  if (upstream.status !== 200 && upstream.status !== 206 && upstream.status !== 416) {
    await upstream.body?.cancel();
    return plain(502, "the remote could not serve this media");
  }
  const out = new Headers({ "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" });
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value !== null) out.set(name, value);
  }
  const type = mediaTypeFor(file);
  if (type !== null) out.set("Content-Type", type);
  return new Response(method === "HEAD" ? null : upstream.body, {
    status: upstream.status,
    headers: out,
  });
}

/** The scheme's request handler (pass it to `protocol.handle`). */
export function createMediaHandler(deps: MediaDeps): (request: Request) => Promise<Response> {
  return async (request) => {
    const method = request.method.toUpperCase();
    if (method !== "GET" && method !== "HEAD") {
      return plain(405, "method not allowed", { Allow: "GET, HEAD" });
    }
    const file = parseMediaUrl(request.url);
    if (file === null) return plain(404, "media not found");
    if (isSensitivePath(file)) return plain(403, "not allowed");
    const type = mediaTypeFor(file);
    if (type === null) return plain(415, "unsupported media type");
    const target = deps.target();
    if (target === null) return plain(503, "Indus is not connected yet");
    if (target.mode === "remote") return remoteMedia(target, file, method, request, deps);

    let real: string;
    try {
      real = await fsp.realpath(path.resolve(file));
    } catch {
      // Say "not found" only inside the allowed folders; outside them nothing is told.
      return (await deps.roots.allows(path.resolve(file)))
        ? plain(404, "media not found")
        : plain(403, "not allowed");
    }
    if (isSensitivePath(real) || mediaTypeFor(real) === null) return plain(403, "not allowed");
    if (!(await deps.roots.allows(real))) {
      deps.log?.("[media] refused a path outside the allowed folders");
      return plain(403, "not allowed");
    }
    return localMediaResponse(real, type, method, request.headers.get("range"));
  };
}

/** The part of Electron's `protocol` the scheme uses. */
export interface ProtocolLike {
  handle(scheme: string, handler: (request: Request) => Promise<Response>): void;
}

export function installMediaProtocol(protocol: ProtocolLike, deps: MediaDeps): void {
  protocol.handle(MEDIA_SCHEME, createMediaHandler(deps));
}
