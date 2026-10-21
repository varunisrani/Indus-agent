// The REST proxy in main (P20-14). The renderer asks `api.request({method, path, body?, profile?,
// timeoutMs?})`; main sends it to the current connection's REST origin with the credential only
// main holds (the launch's `indus serve` session token locally, the saved token for a remote) and
// answers `{kind: "response", status, body}` or, for a 404, the value `{kind: "not-found", ...}`.
// See docs/notes/P20/rest-proxy.md.

import { SESSION_TOKEN_HEADER } from "../backend/localAttach.ts";
import { IpcError } from "../ipc/envelope.ts";
import { isSensitivePath } from "../security/sensitiveFiles.ts";

export const DEFAULT_TIMEOUT_MS = 30_000;
export const STARTUP_TIMEOUT_MS = 60_000;
export const MAX_BODY_BYTES = 256 * 1024 * 1024;
export const STARTUP_PATHS: ReadonlySet<string> = new Set([
  "/api/status",
  "/api/health",
  "/api/config",
  "/api/config/schema",
  "/api/profiles",
]);
/** Routes whose answer would put a credential in the renderer's hands. */
export const REFUSED_PATHS: ReadonlySet<string> = new Set([
  "/api/files/download-ticket",
  "/api/auth/ws-ticket",
]);

export interface ApiRequest {
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  path: string;
  body?: unknown;
  profile?: string | undefined;
  timeoutMs?: number | undefined;
}

export type ApiResult =
  | { kind: "response"; status: number; body: unknown }
  | { kind: "not-found"; status: 404; body: unknown };

export interface ProxyTarget {
  /** `http://127.0.0.1:PORT` or the remote base (a path prefix allowed). */
  origin: string;
  token: string;
  /** The connection's profile, used when the request names none. */
  profile: string | null;
}

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface ProxyDeps {
  target(): ProxyTarget | null;
  fetch?: typeof fetch;
  timers?: Timers;
  log(line: string): void;
}

const realTimers: Timers = {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

/**
 * The URL for `rawPath` on `base`: only a path under `/api/` (no scheme, no `//`, no backslash,
 * no `..` or `.` segment, no fragment), and the result keeps the base's origin.
 */
export function buildApiUrl(base: string, rawPath: string, profile: string | null): URL {
  if (!rawPath.startsWith("/api/") || rawPath.startsWith("//") || /[\\#\s]/.test(rawPath)) {
    throw new IpcError("bad-request", "only paths under /api/ on the current connection");
  }
  const cut = rawPath.indexOf("?");
  const pathname = cut < 0 ? rawPath : rawPath.slice(0, cut);
  const query = cut < 0 ? "" : rawPath.slice(cut + 1);
  let decoded: string;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    throw new IpcError("bad-request", "the path is not well encoded");
  }
  if (
    decoded.split("/").some((segment) => segment === ".." || segment === ".") ||
    decoded.includes("//")
  ) {
    throw new IpcError("bad-request", "the path may not walk out of /api/");
  }
  const root = new URL(base);
  const prefix = root.pathname.replace(/\/+$/, "");
  const url = new URL(`${root.origin}${prefix}${pathname}${query === "" ? "" : `?${query}`}`);
  if (url.origin !== root.origin || !url.pathname.startsWith(`${prefix}/api/`)) {
    throw new IpcError("bad-request", "only paths under /api/ on the current connection");
  }
  if (profile !== null && profile !== "" && !url.searchParams.has("profile")) {
    url.searchParams.set("profile", profile);
  }
  return url;
}

function pathsIn(value: unknown): string[] {
  if (value === null || typeof value !== "object") return [];
  const record = value as Record<string, unknown>;
  const found: string[] = [];
  for (const key of ["path", "src", "dest", "target", "source"]) {
    if (typeof record[key] === "string") found.push(record[key] as string);
  }
  if (Array.isArray(record.paths))
    for (const p of record.paths) if (typeof p === "string") found.push(p);
  return found;
}

export class ApiProxy {
  private readonly deps: ProxyDeps;
  private readonly timers: Timers;

  constructor(deps: ProxyDeps) {
    this.deps = deps;
    this.timers = deps.timers ?? realTimers;
  }

  async request(request: ApiRequest): Promise<ApiResult> {
    const target = this.deps.target();
    if (target === null) throw new IpcError("not-connected", "Indus is not connected yet");
    const url = buildApiUrl(target.origin, request.path, request.profile ?? target.profile);
    const route = url.pathname.slice(new URL(target.origin).pathname.replace(/\/+$/, "").length);
    if (REFUSED_PATHS.has(route)) {
      throw new IpcError("forbidden", "that route is not available to the app window");
    }
    if (route.startsWith("/api/files")) {
      const named = [...url.searchParams.getAll("path"), ...pathsIn(request.body)];
      if (named.some(isSensitivePath)) {
        throw new IpcError("forbidden", "files that hold secrets are not available here");
      }
    }
    let body: string | undefined;
    if (request.body !== undefined && request.method !== "GET") {
      body = JSON.stringify(request.body);
      if (Buffer.byteLength(body) > MAX_BODY_BYTES) {
        throw new IpcError("too-large", "the request is larger than 256 MiB");
      }
    }
    const timeoutMs =
      request.timeoutMs ?? (STARTUP_PATHS.has(route) ? STARTUP_TIMEOUT_MS : DEFAULT_TIMEOUT_MS);
    const controller = new AbortController();
    const timer = this.timers.setTimeout(() => controller.abort(), timeoutMs);
    let response: Response;
    try {
      response = await (this.deps.fetch ?? fetch)(url, {
        method: request.method,
        headers: {
          Accept: "application/json",
          [SESSION_TOKEN_HEADER]: target.token,
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        },
        ...(body === undefined ? {} : { body }),
        signal: controller.signal,
        redirect: "manual",
      });
    } catch {
      this.timers.clearTimeout(timer);
      if (controller.signal.aborted) {
        throw new IpcError(
          "timeout",
          `the request took longer than ${Math.round(timeoutMs / 1000)} s`,
        );
      }
      throw new IpcError("unreachable", "the Indus server did not answer");
    }
    let text: string;
    try {
      text = await response.text();
    } catch {
      this.timers.clearTimeout(timer);
      if (controller.signal.aborted) {
        throw new IpcError(
          "timeout",
          `the request took longer than ${Math.round(timeoutMs / 1000)} s`,
        );
      }
      throw new IpcError("unreachable", "the answer was cut off");
    }
    this.timers.clearTimeout(timer);
    let parsed: unknown = text;
    if (text !== "") {
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = text;
      }
    } else parsed = null;
    if (response.status === 404) return { kind: "not-found", status: 404, body: parsed };
    return { kind: "response", status: response.status, body: parsed };
  }
}
