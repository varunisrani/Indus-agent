// One saved remote connection (P20-11): a URL and a token for an Indus server running elsewhere.
// Since P24-29 the connections registry (`connections/registry.ts`) decides which remote a launch
// dials (`RemoteDeps.registry`); this file's test/save/forget keep serving the first-run chooser.
// Saving needs both legs to pass with the real credential (R08.5): an HTTP call and a WebSocket.
// Only a confirmed 401/403 (or a 4401/4403 close) means "unauthorized"; timeouts, refused
// connections and 5xx are connectivity and never ask for a new token (R08.4). The URL lives in
// `<userData>/remote-connection.json`; the token in the secrets store (P20-07), never in a file in
// plaintext when the keychain works, never in a log line, never across the bridge.

import fs from "node:fs";
import path from "node:path";
import type { RemoteAuth } from "../connections/remoteDial.ts";
import { readJsonFile, writeFileAtomic } from "../fsSecure.ts";
import { SESSION_TOKEN_HEADER } from "./localAttach.ts";
import { REMOTE_PROBE_TIMEOUT_MS } from "./remoteLiveness.ts";
import { DASHBOARD_SUBPROTOCOL, DASHBOARD_TOKEN_PREFIX } from "./tickets.ts";

export const REMOTE_FILE = "remote-connection.json";
export const REMOTE_TOKEN_SECRET = "remote-connection-token";
/** How long an opened test socket must stay open (or answer) to count as accepted. */
export const WS_SETTLE_MS = 1_500;

export type Leg = "ok" | "unauthorized" | "unreachable" | "error";

export interface RemoteTestResult {
  ok: boolean;
  http: Leg;
  ws: Leg;
  message?: string;
}

export class RemoteUrlError extends Error {}

/** Trimmed, scheme added to a bare `host:port`, http/https only, no credentials, query or fragment. */
export function normalizeRemoteUrl(raw: string): string {
  let value = raw.trim();
  if (value === "") throw new RemoteUrlError("enter the server's address");
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(value)) value = `http://${value}`;
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new RemoteUrlError("that is not a web address");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new RemoteUrlError("the address must start with http:// or https://");
  }
  if (url.username !== "" || url.password !== "") {
    throw new RemoteUrlError("put the token in the token field, not in the address");
  }
  if (url.search !== "" || url.hash !== "") {
    throw new RemoteUrlError("the address must not carry a query or a fragment");
  }
  return `${url.origin}${url.pathname.replace(/\/+$/, "")}`;
}

/** The server's WebSocket endpoint for a base URL (a path prefix is kept). */
export function remoteWsUrl(base: string): string {
  const url = new URL(base);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = `${url.pathname.replace(/\/+$/, "")}/api/ws`;
  return url.toString();
}

export function classifyStatus(status: number): Leg {
  if (status === 401 || status === 403) return "unauthorized";
  if (status >= 200 && status < 300) return "ok";
  // the gate answers 401 before routing, so a 404 behind it means the credential passed
  if (status === 404) return "ok";
  return "error";
}

export function isReauth(leg: Leg): boolean {
  return leg === "unauthorized";
}

export interface WebSocketLike {
  addEventListener(
    type: "open" | "message" | "close" | "error",
    listener: (event: unknown) => void,
  ): void;
  close(): void;
}

export interface RemoteDeps {
  userData: string;
  secrets: {
    get(name: string): string | null;
    put(name: string, value: string): void;
    delete(name: string): void;
  };
  fetch?: typeof fetch;
  webSocket?: (url: string, protocols: string[]) => WebSocketLike;
  log: (line: string) => void;
  timeoutMs?: number;
  settleMs?: number;
  /** The connections registry (P24-29): when given, it decides what `saved()` answers. */
  registry?: { launchRemote(): SavedRemote | null };
}

export interface SavedRemote {
  url: string;
  token: string;
  /** The connections-registry entry (P24-29); absent for a dial built from the P20 file alone. */
  connectionId?: string;
  /** Main's credential view of this remote (token, OAuth or basic + access-proxy headers). */
  auth?: RemoteAuth;
  /** An SSH entry (P24-30): open the tunnel and the remote serve, then dial what it answers. */
  prepare?: () => Promise<{ url: string; auth: RemoteAuth }>;
}

export class RemoteStore {
  private readonly deps: RemoteDeps;

  constructor(deps: RemoteDeps) {
    this.deps = deps;
  }

  private file(): string {
    return path.join(this.deps.userData, REMOTE_FILE);
  }

  private savedUrl(): string | null {
    const data = readJsonFile(this.file()) as { url?: unknown } | null;
    return data !== null && typeof data.url === "string" ? data.url : null;
  }

  get(): { url: string | null; hasToken: boolean } {
    return { url: this.savedUrl(), hasToken: this.deps.secrets.get(REMOTE_TOKEN_SECRET) !== null };
  }

  /** The remote this launch dials (main only), or null for this computer. */
  saved(): SavedRemote | null {
    if (this.deps.registry !== undefined) return this.deps.registry.launchRemote();
    const url = this.savedUrl();
    const token = this.deps.secrets.get(REMOTE_TOKEN_SECRET);
    return url === null || token === null ? null : { url, token };
  }

  async test(input: { url: string; token: string }): Promise<RemoteTestResult> {
    const base = normalizeRemoteUrl(input.url);
    const timeout = this.deps.timeoutMs ?? REMOTE_PROBE_TIMEOUT_MS;
    const get = (route: string, headers: Record<string, string> = {}) =>
      (this.deps.fetch ?? fetch)(`${base}${route}`, {
        headers: { Accept: "application/json", ...headers },
        signal: AbortSignal.timeout(timeout),
        redirect: "error",
      });

    // the public status says how this server authenticates
    let status: Response;
    try {
      status = await get("/api/status");
    } catch {
      return { ok: false, http: "unreachable", ws: "error", message: "the server did not answer" };
    }
    if (status.status !== 200) {
      await status.body?.cancel();
      return {
        ok: false,
        http: classifyStatus(status.status) === "ok" ? "error" : classifyStatus(status.status),
        ws: "error",
        message: `the server's status page answered ${status.status}`,
      };
    }
    const body = (await status.json().catch(() => null)) as { auth_required?: unknown } | null;
    if (body?.auth_required === true) {
      return {
        ok: false,
        http: "unauthorized",
        ws: "error",
        message: "this server asks for a sign-in, which this version of the app cannot do yet",
      };
    }

    let http: Leg;
    try {
      const response = await get("/api/config", { [SESSION_TOKEN_HEADER]: input.token });
      await response.body?.cancel();
      http = classifyStatus(response.status);
    } catch {
      http = "unreachable";
    }
    const ws = await this.wsLeg(base, input.token, timeout);
    const ok = http === "ok" && ws === "ok";
    const result: RemoteTestResult = { ok, http, ws };
    if (!ok) result.message = describe(http, ws);
    return result;
  }

  private wsLeg(base: string, token: string, timeout: number): Promise<Leg> {
    const settle = this.deps.settleMs ?? WS_SETTLE_MS;
    return new Promise((resolve) => {
      let opened = false;
      let done = false;
      let socket: WebSocketLike;
      const finish = (leg: Leg) => {
        if (done) return;
        done = true;
        clearTimeout(deadline);
        clearTimeout(settled);
        try {
          socket.close();
        } catch {
          // already closed
        }
        resolve(leg);
      };
      let settled: ReturnType<typeof setTimeout> | undefined;
      const deadline = setTimeout(() => finish(opened ? "ok" : "unreachable"), timeout);
      try {
        socket = (this.deps.webSocket ?? ((url, protocols) => new WebSocket(url, protocols)))(
          remoteWsUrl(base),
          [DASHBOARD_SUBPROTOCOL, `${DASHBOARD_TOKEN_PREFIX}${token}`],
        );
      } catch {
        finish("error");
        return;
      }
      socket.addEventListener("open", () => {
        opened = true;
        settled = setTimeout(() => finish("ok"), settle);
      });
      socket.addEventListener("message", () => finish("ok"));
      socket.addEventListener("close", (event) => {
        const code = (event as { code?: number }).code ?? 0;
        if (code === 4401 || code === 4403) finish("unauthorized");
        else finish(opened && code === 1000 ? "ok" : opened ? "error" : "unreachable");
      });
      socket.addEventListener("error", () => {
        if (!opened) finish("unreachable");
      });
    });
  }

  /** Test, and save only when both legs pass. */
  async save(input: {
    url: string;
    token: string;
  }): Promise<RemoteTestResult & { saved: boolean }> {
    const result = await this.test(input);
    if (!result.ok) return { ...result, saved: false };
    const url = normalizeRemoteUrl(input.url);
    this.deps.secrets.put(REMOTE_TOKEN_SECRET, input.token);
    fs.mkdirSync(this.deps.userData, { recursive: true });
    writeFileAtomic(this.file(), `${JSON.stringify({ v: 1, url })}\n`, { mode: 0o600 });
    this.deps.log(`[remote] saved the remote connection to ${new URL(url).host}`);
    return { ...result, saved: true };
  }

  forget(): void {
    this.deps.secrets.delete(REMOTE_TOKEN_SECRET);
    fs.rmSync(this.file(), { force: true });
    this.deps.log("[remote] forgot the remote connection");
  }
}

function describe(http: Leg, ws: Leg): string {
  if (http === "unauthorized" || ws === "unauthorized") return "the server refused this token";
  if (http === "unreachable" || ws === "unreachable") return "the server could not be reached";
  if (http === "ok") return "the server answered over HTTP, but its WebSocket did not work";
  return "the server answered with an error";
}
