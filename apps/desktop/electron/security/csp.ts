// The renderer's Content-Security-Policy (D-16.19). One builder, applied twice: as a meta tag in
// the built HTML (`injectCspMeta`, used by the renderer build) and as a response header for the
// app's own URLs (`installCspHeaders`). No `unsafe-eval` and no inline script anywhere; the only
// relaxation is inline styles (the Tailwind runtime). A dev build adds the Vite dev server to
// script and connect sources and nothing else.

import { type AppOrigin, appUrlPatterns, isAppUrl } from "../appOrigin.ts";

export const MEDIA_SCHEME = "indus-media";

export interface CspInput {
  dev: boolean;
  /** `http://localhost:5173` in a dev build. */
  devServerOrigin?: string;
  /** Daemon / serve origins computed at boot (`http://127.0.0.1:PORT`, `ws://127.0.0.1:PORT`). */
  connectOrigins?: readonly string[];
}

export class CspOriginError extends Error {}

const ORIGIN_SCHEMES = new Set(["http:", "https:", "ws:", "wss:"]);

/** Reduce `raw` to a bare origin; throws `CspOriginError` for anything with more than that. */
export function cspOrigin(raw: string): string {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new CspOriginError(`not an origin: ${JSON.stringify(raw)}`);
  }
  if (!ORIGIN_SCHEMES.has(url.protocol))
    throw new CspOriginError(`scheme not allowed: ${url.protocol}`);
  if (url.username || url.password) throw new CspOriginError("an origin carries no credentials");
  if ((url.pathname !== "/" && url.pathname !== "") || url.search || url.hash) {
    throw new CspOriginError("an origin has no path, query or fragment");
  }
  if (raw.includes("*")) throw new CspOriginError("wildcards are not allowed");
  return `${url.protocol}//${url.host}`;
}

function unique(values: readonly string[]): string[] {
  return [...new Set(values)];
}

/** The policy string. Directive order is fixed (snapshot-tested). */
export function buildCsp(input: CspInput): string {
  const dev =
    input.dev && input.devServerOrigin !== undefined ? cspOrigin(input.devServerOrigin) : null;
  const devWs = dev === null ? [] : [dev.replace(/^http/, "ws")];
  const connect = unique([
    "'self'",
    ...(input.connectOrigins ?? []).map(cspOrigin),
    ...(dev === null ? [] : [dev, ...devWs]),
  ]);
  const directives: Array<[string, string[]]> = [
    ["default-src", ["'self'"]],
    ["script-src", dev === null ? ["'self'"] : ["'self'", dev]],
    ["style-src", ["'self'", "'unsafe-inline'"]],
    ["img-src", ["'self'", "data:", "blob:", `${MEDIA_SCHEME}:`]],
    ["media-src", ["'self'", "blob:", `${MEDIA_SCHEME}:`]],
    ["font-src", ["'self'", "data:"]],
    ["connect-src", connect],
    ["frame-src", ["'none'"]],
    ["object-src", ["'none'"]],
    ["base-uri", ["'none'"]],
    ["form-action", ["'none'"]],
  ];
  return directives.map(([name, sources]) => `${name} ${sources.join(" ")}`).join("; ");
}

function escapeAttribute(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

const META = /<meta\s+http-equiv=["']Content-Security-Policy["'][^>]*>\s*/gi;

/** `html` with exactly one CSP meta tag, first thing in `<head>`. */
export function injectCspMeta(html: string, csp: string): string {
  const tag = `<meta http-equiv="Content-Security-Policy" content="${escapeAttribute(csp)}">`;
  const cleaned = html.replace(META, "");
  const head = /<head(\s[^>]*)?>/i.exec(cleaned);
  if (head === null) throw new Error("the page has no <head>");
  const at = head.index + head[0].length;
  return `${cleaned.slice(0, at)}\n    ${tag}${cleaned.slice(at)}`;
}

export interface HeadersDetails {
  url: string;
  responseHeaders?: Record<string, string[] | string>;
}

export interface HeaderSession {
  webRequest: {
    onHeadersReceived(
      filter: { urls: string[] },
      listener: (
        details: HeadersDetails,
        callback: (response: { responseHeaders?: Record<string, string[] | string> }) => void,
      ) => void,
    ): void;
  };
}

/** Headers for `details` with our policy as the only CSP (app URLs), or untouched (others). */
export function withCspHeader(
  details: HeadersDetails,
  csp: string,
  appOrigin: AppOrigin,
): Record<string, string[] | string> | undefined {
  if (!isAppUrl(details.url, appOrigin)) return details.responseHeaders;
  const headers: Record<string, string[] | string> = {};
  for (const [name, value] of Object.entries(details.responseHeaders ?? {})) {
    const lower = name.toLowerCase();
    if (lower === "content-security-policy" || lower === "content-security-policy-report-only")
      continue;
    headers[name] = value;
  }
  headers["Content-Security-Policy"] = [csp];
  return headers;
}

/** Add the policy header to every app page the session loads. `csp` is read per response. */
export function installCspHeaders(
  session: HeaderSession,
  appOrigin: AppOrigin,
  csp: () => string,
): void {
  session.webRequest.onHeadersReceived({ urls: appUrlPatterns(appOrigin) }, (details, callback) => {
    const responseHeaders = withCspHeader(details, csp(), appOrigin);
    callback(responseHeaders === undefined ? {} : { responseHeaders });
  });
}

/**
 * Where a renderer may connect before main knows the backend: the daemon and the serve REST port
 * are on loopback with ports chosen at run time, and a remote connection is any TLS host. A loaded
 * page keeps the policy it was loaded with, and the main window loads before the boot finds the
 * backend, so both the meta tag and main's header carry these.
 */
export const RUNTIME_CONNECT_SOURCES = [
  "ws://127.0.0.1:*",
  "http://127.0.0.1:*",
  "ws://localhost:*",
  "http://localhost:*",
  "wss:",
  "https:",
] as const;

/** `policy` with `RUNTIME_CONNECT_SOURCES` appended to its `connect-src`. */
export function withRuntimeConnect(policy: string): string {
  return policy.replace(
    /(^|; )connect-src ([^;]*)/,
    (_whole, lead: string, sources: string) =>
      `${lead}connect-src ${sources} ${RUNTIME_CONNECT_SOURCES.join(" ")}`,
  );
}

/**
 * The live policy (main's header on app URLs): fixed at boot except for `connect-src`, which
 * grows as the backend origins become known (P20-10 adds the daemon's WS origin and the serve
 * REST origin), on top of `RUNTIME_CONNECT_SOURCES`.
 */
export class CspState {
  private readonly input: CspInput;
  private readonly origins = new Set<string>();
  private cached: string | null = null;

  constructor(input: Omit<CspInput, "connectOrigins">) {
    this.input = input;
  }

  /** Allow the renderer to connect to `origin` (validated; throws `CspOriginError`). */
  addConnectOrigin(origin: string): void {
    const clean = cspOrigin(origin);
    if (this.origins.has(clean)) return;
    this.origins.add(clean);
    this.cached = null;
  }

  current(): string {
    this.cached ??= withRuntimeConnect(
      buildCsp({ ...this.input, connectOrigins: [...this.origins] }),
    );
    return this.cached;
  }
}
