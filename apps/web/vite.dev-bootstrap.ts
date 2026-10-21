// Development only (P18-29): `pnpm --filter @indus/web dev` serves its own index.html, which has
// none of the runtime globals the dashboard backend writes into the page it serves (session token,
// base path, flags, profiles; see src/lib/bootstrap.ts). This plugin fetches the running backend's
// index.html on every dev page load, reads only the known `window.__INDUS_*__` assignments from its
// bootstrap script, re-encodes them and injects them into the dev page. It never runs in a build
// and never logs a value. The `/api` proxy (WebSocket upgrades included, so the subprotocol
// credential reaches the backend untouched) is built here too, for vite.config.ts.

import type { HtmlTagDescriptor, Logger, Plugin, ProxyOptions } from "vite";

export const DEFAULT_BACKEND = "http://127.0.0.1:9119";
/** Point the dev server at a backend on another address (`indus dashboard --port …`). */
export const BACKEND_ENV = "INDUS_WEB_DEV_BACKEND";
export const FETCH_TIMEOUT_MS = 3_000;

/** The globals the backend's bootstrap script sets, and the only ones copied. */
export const BOOTSTRAP_KEYS = [
  "__INDUS_SESSION_TOKEN__",
  "__INDUS_BASE_PATH__",
  "__INDUS_AUTH_REQUIRED__",
  "__INDUS_DASHBOARD_EMBEDDED_CHAT__",
  "__INDUS_INITIAL_PROFILE__",
  "__INDUS_DASHBOARD_PROFILE__",
] as const;

export type BootstrapKey = (typeof BOOTSTRAP_KEYS)[number];
export type BootstrapValues = Partial<Record<BootstrapKey, string | boolean>>;

const KNOWN = new Set<string>(BOOTSTRAP_KEYS);
// One assignment: `window.__INDUS_X__=<JSON string | true | false>;`. The backend escapes `<`, `>`,
// `&` and the two line separators inside strings, so a value never ends the script early.
const ASSIGNMENT = /window\.(__INDUS_[A-Z0-9_]+__)\s*=\s*("(?:[^"\\\n]|\\.)*"|true|false)\s*;/g;

/** The backend to proxy to: `INDUS_WEB_DEV_BACKEND` when it is an http(s) URL, else the default. */
export function backendUrl(env: Record<string, string | undefined> = process.env): string {
  const raw = env[BACKEND_ENV]?.trim();
  if (!raw) return DEFAULT_BACKEND;
  try {
    const url = new URL(raw);
    if (url.protocol !== "http:" && url.protocol !== "https:") return DEFAULT_BACKEND;
    return url.origin;
  } catch {
    return DEFAULT_BACKEND;
  }
}

/** The dev server's proxy table: every `/api` request, WebSocket upgrades included. */
export function apiProxy(backend: string): Record<string, ProxyOptions> {
  // Host stays the dev server's loopback address, which the backend's Host guard accepts.
  return { "/api": { target: backend, changeOrigin: false, ws: true } };
}

/** The known bootstrap values in a backend index.html (anything else is ignored). */
export function parseBootstrap(html: string): BootstrapValues {
  const values: BootstrapValues = {};
  for (const match of html.matchAll(ASSIGNMENT)) {
    const key = match[1];
    const literal = match[2];
    if (key === undefined || literal === undefined || !KNOWN.has(key)) continue;
    let value: unknown;
    try {
      value = JSON.parse(literal);
    } catch {
      continue;
    }
    if (typeof value === "string" || typeof value === "boolean") {
      values[key as BootstrapKey] = value;
    }
  }
  return values;
}

function encode(value: string | boolean): string {
  return JSON.stringify(value)
    .replace(/</g, "\\u003c")
    .replace(/>/g, "\\u003e")
    .replace(/&/g, "\\u0026")
    .replace(/\u2028/g, "\\u2028")
    .replace(/\u2029/g, "\\u2029");
}

/** The inline script body for `values` (keys in their fixed order). */
export function bootstrapScript(values: BootstrapValues): string {
  return BOOTSTRAP_KEYS.filter((key) => values[key] !== undefined)
    .map((key) => `window.${key}=${encode(values[key] as string | boolean)};`)
    .join("");
}

export interface DevBootstrapOptions {
  backend?: string;
  fetch?: typeof fetch;
  timeoutMs?: number;
  /** Where warnings go (default: the Vite logger). Never given a value from the page. */
  warn?: (message: string) => void;
}

/** Fetch the backend's index.html and build the tag to inject, or undefined (with a warning). */
export async function loadBootstrapTag(
  options: DevBootstrapOptions & { backend: string; warn: (message: string) => void },
): Promise<HtmlTagDescriptor | undefined> {
  const { backend, warn } = options;
  const get = options.fetch ?? fetch;
  let html: string;
  try {
    const response = await get(`${backend}/`, {
      headers: { accept: "text/html" },
      redirect: "manual",
      signal: AbortSignal.timeout(options.timeoutMs ?? FETCH_TIMEOUT_MS),
    });
    if (response.status !== 200) {
      warn(
        `[indus] the dashboard backend at ${backend} answered ${response.status} for its page ` +
          "(not built yet, or it asks for a sign-in); /api calls from this page may be refused.",
      );
      return undefined;
    }
    html = await response.text();
  } catch {
    warn(
      `[indus] no dashboard backend answered at ${backend}; start \`indus dashboard\` ` +
        `(or set ${BACKEND_ENV}). /api calls from this page will fail.`,
    );
    return undefined;
  }
  const values = parseBootstrap(html);
  if (Object.keys(values).length === 0) {
    warn(`[indus] the page from ${backend} carried no bootstrap values; is it the dashboard?`);
    return undefined;
  }
  return { tag: "script", injectTo: "head-prepend", children: bootstrapScript(values) };
}

/** The dev-server plugin (see the file notes). */
export function devBootstrap(options: DevBootstrapOptions = {}): Plugin {
  const backend = options.backend ?? backendUrl();
  let logger: Logger | undefined;
  const warn = options.warn ?? ((message: string) => logger?.warn(message));
  return {
    name: "indus:dev-bootstrap",
    apply: "serve",
    configResolved(config) {
      logger = config.logger;
    },
    async transformIndexHtml() {
      const tag = await loadBootstrapTag({ ...options, backend, warn });
      return tag ? [tag] : [];
    },
  };
}
