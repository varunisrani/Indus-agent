// Per-connection access-proxy headers (P24-29): a remote behind Cloudflare Access or a similar
// gateway may need extra headers on every request. They are a secret (keychain entry
// `connection-<id>-headers`), sent only to the connection's own gateway (same scheme family, host
// and path prefix; ws/wss count as http/https), never to anything else.
//
// A configured set can never carry a credential or a header that changes the request's framing or
// origin: `Authorization`, `Cookie`, `Host`, `Origin`, `Referer` and our session-token header are
// always dropped, as are `Connection`, `Content-Length`, `Content-Type`, `Transfer-Encoding`,
// `Upgrade`, `Proxy-*` and `Sec-*`. Names must be HTTP tokens; values lose CR and LF.

import { SESSION_TOKEN_HEADER } from "../backend/localAttach.ts";

const NAME_RE = /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/;
export const DROPPED_HEADERS: ReadonlySet<string> = new Set([
  "authorization",
  "cookie",
  "host",
  "origin",
  "referer",
  SESSION_TOKEN_HEADER.toLowerCase(),
  "connection",
  "content-length",
  "content-type",
  "transfer-encoding",
  "upgrade",
  "te",
  "trailer",
  "keep-alive",
]);
const DROPPED_PREFIXES = ["proxy-", "sec-"];
export const MAX_HEADERS = 16;
export const MAX_VALUE_CHARS = 4096;

export interface CleanHeaders {
  headers: Record<string, string>;
  /** Names refused (said back to the person by name, never by value). */
  dropped: string[];
}

export function isDroppedHeader(name: string): boolean {
  const lower = name.toLowerCase();
  return DROPPED_HEADERS.has(lower) || DROPPED_PREFIXES.some((prefix) => lower.startsWith(prefix));
}

/** The usable part of a configured header set. */
export function cleanHeaders(raw: unknown): CleanHeaders {
  const headers: Record<string, string> = {};
  const dropped: string[] = [];
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return { headers, dropped };
  for (const [rawName, rawValue] of Object.entries(raw as Record<string, unknown>)) {
    const name = rawName.trim();
    if (name === "") continue;
    if (!NAME_RE.test(name) || isDroppedHeader(name) || typeof rawValue !== "string") {
      dropped.push(name.slice(0, 64));
      continue;
    }
    const value = rawValue.replace(/[\r\n]+/g, "").trim();
    if (value === "" || value.length > MAX_VALUE_CHARS) {
      dropped.push(name);
      continue;
    }
    if (Object.keys(headers).length >= MAX_HEADERS) {
      dropped.push(name);
      continue;
    }
    headers[name] = value;
  }
  return { headers, dropped };
}

function family(protocol: string): string {
  if (protocol === "ws:") return "http:";
  if (protocol === "wss:") return "https:";
  return protocol;
}

/** Does `requestUrl` go to the gateway at `baseUrl` (scheme family, host, path prefix)? */
export function matchesGateway(requestUrl: string, baseUrl: string): boolean {
  let request: URL;
  let base: URL;
  try {
    request = new URL(requestUrl);
    base = new URL(baseUrl);
  } catch {
    return false;
  }
  if (family(request.protocol) !== family(base.protocol) || request.host !== base.host) {
    return false;
  }
  const prefix = base.pathname.replace(/\/+$/, "");
  return prefix === "" || request.pathname === prefix || request.pathname.startsWith(`${prefix}/`);
}

/** The headers to add to `requestUrl` for a connection at `baseUrl` (empty when it is elsewhere). */
export function proxyHeadersFor(
  requestUrl: string,
  baseUrl: string,
  configured: unknown,
): Record<string, string> {
  if (!matchesGateway(requestUrl, baseUrl)) return {};
  return cleanHeaders(configured).headers;
}

/** The stored form of a header set (JSON of the clean headers) and the names refused. */
export function encodeHeaders(raw: unknown): { stored: string; dropped: string[] } {
  const { headers, dropped } = cleanHeaders(raw);
  return { stored: Object.keys(headers).length === 0 ? "" : JSON.stringify(headers), dropped };
}

export function decodeHeaders(stored: string | null): Record<string, string> {
  if (stored === null || stored === "") return {};
  try {
    return cleanHeaders(JSON.parse(stored)).headers;
  } catch {
    return {};
  }
}
