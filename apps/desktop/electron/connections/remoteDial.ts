// How main talks to a remote Indus for one registry entry (P24-29): the credential headers of
// each request, the single-use WebSocket ticket of each dial, and the two-leg connection test.
//
// The WebSocket dial (D-16.691): Electron cannot put a token main holds into the renderer's
// upgrade, so for every dial main asks the server for a ticket — `POST /api/auth/ws-ticket
// {purpose: "web-rpc", profile?}` with the connection's credential — and the window offers
// `indus-dashboard-v1` + `indus-dashboard-ticket.<ticket>`. The ticket is good once, for 30 s;
// it is the only credential that crosses the bridge (R08.4).
//
// Credentials by mode: `token` → `X-Indus-Session-Token`; `oauth` → `Authorization: Bearer
// <access>` from the native sign-in (refreshed near expiry); `basic` → main signs in with the
// stored user name and password (`POST /auth/password-login`) and keeps the session cookie in
// memory. Access-proxy headers (cleaned, `proxyHeaders.ts`) go on every request to that gateway.
// Re-authentication happens only on an explicit 401 or 403: OAuth refreshes once, basic signs in
// once; a timeout, a refused connection or a 5xx is connectivity and never asks for a credential.

import { SESSION_TOKEN_HEADER } from "../backend/localAttach.ts";
import { classifyStatus, type Leg, remoteWsUrl, WS_SETTLE_MS } from "../backend/remote.ts";
import { REMOTE_PROBE_TIMEOUT_MS } from "../backend/remoteLiveness.ts";
import { DASHBOARD_SUBPROTOCOL, DASHBOARD_TICKET_PREFIX } from "../backend/tickets.ts";
import { OAuthError, type OAuthSession } from "./nativeOauth.ts";
import { proxyHeadersFor } from "./proxyHeaders.ts";
import type { AuthMode } from "./registry.ts";

export const DIAL_PURPOSE = "web-rpc";
export const TICKET_ROUTE = "/api/auth/ws-ticket";

export type DialErrorKind = "unauthorized" | "unreachable" | "old-server" | "busy" | "error";

export class DialError extends Error {
  readonly kind: DialErrorKind;
  readonly status: number | null;

  constructor(kind: DialErrorKind, message: string, status: number | null = null) {
    super(message);
    this.kind = kind;
    this.status = status;
  }
}

export interface RemoteCredentials {
  mode: AuthMode;
  token?: string | null;
  oauth?: OAuthSession | null;
  basic?: { username: string; password: string } | null;
  /** Access-proxy headers as stored (cleaned again before use). */
  headers?: Record<string, string>;
}

export interface DialDeps {
  fetch?: typeof fetch;
  timeoutMs?: number;
}

function joinUrl(base: string, route: string): string {
  const url = new URL(base);
  url.pathname = `${url.pathname.replace(/\/+$/, "")}${route}`;
  return url.toString();
}

/** Everything main needs to call one remote with its credential. */
export class RemoteAuth {
  readonly base: string;
  private readonly credentials: RemoteCredentials;
  private readonly deps: DialDeps;
  private cookie: string | null = null;

  constructor(base: string, credentials: RemoteCredentials, deps: DialDeps = {}) {
    this.base = base;
    this.credentials = credentials;
    this.deps = deps;
  }

  get mode(): AuthMode {
    return this.credentials.mode;
  }

  private fetcher(): typeof fetch {
    return this.deps.fetch ?? fetch;
  }

  /** Access-proxy headers for `url` (only when it goes to this gateway). */
  proxyHeaders(url: string): Record<string, string> {
    return proxyHeadersFor(url, this.base, this.credentials.headers ?? {});
  }

  /** The credential headers of one request to this server. */
  async authHeaders(): Promise<Record<string, string>> {
    const { mode } = this.credentials;
    if (mode === "token") {
      const token = this.credentials.token ?? "";
      if (token === "") throw new DialError("unauthorized", "no token is saved for this server");
      return { [SESSION_TOKEN_HEADER]: token };
    }
    if (mode === "oauth") {
      const session = this.credentials.oauth;
      if (session === null || session === undefined) {
        throw new DialError("unauthorized", "sign in to this server first");
      }
      try {
        return { Authorization: `Bearer ${await session.accessToken()}` };
      } catch (error) {
        throw this.fromOAuth(error);
      }
    }
    if (this.cookie === null) await this.basicSignIn();
    return { Cookie: this.cookie as string };
  }

  private fromOAuth(error: unknown): DialError {
    if (error instanceof OAuthError && error.kind === "unreachable") {
      return new DialError("unreachable", error.message);
    }
    return new DialError(
      "unauthorized",
      error instanceof Error ? error.message : "sign in to this server again",
    );
  }

  private async basicSignIn(): Promise<void> {
    const basic = this.credentials.basic;
    if (basic === null || basic === undefined || basic.username === "") {
      throw new DialError("unauthorized", "no user name is saved for this server");
    }
    const url = joinUrl(this.base, "/auth/password-login");
    let response: Response;
    try {
      response = await this.fetcher()(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
          ...this.proxyHeaders(url),
        },
        body: JSON.stringify({ username: basic.username, password: basic.password }),
        redirect: "manual",
        signal: AbortSignal.timeout(this.deps.timeoutMs ?? REMOTE_PROBE_TIMEOUT_MS),
      });
    } catch {
      throw new DialError("unreachable", "the server did not answer");
    }
    await response.body?.cancel();
    if (response.status === 401 || response.status === 403) {
      throw new DialError(
        "unauthorized",
        "the server refused this user name or password",
        response.status,
      );
    }
    const cookies = response.headers
      .getSetCookie()
      .map((line) => line.split(";")[0]?.trim() ?? "")
      .filter((pair) => pair.includes("="));
    if (response.status >= 300 || cookies.length === 0) {
      throw new DialError(
        "error",
        `the server's sign-in answered ${response.status}`,
        response.status,
      );
    }
    this.cookie = cookies.join("; ");
  }

  /** After an explicit 401/403: one fresh credential (OAuth refresh, basic sign-in), or false. */
  async reauthenticate(): Promise<boolean> {
    if (this.credentials.mode === "oauth" && this.credentials.oauth) {
      try {
        await this.credentials.oauth.afterUnauthorized();
        return true;
      } catch {
        return false;
      }
    }
    if (this.credentials.mode === "basic") {
      this.cookie = null;
      try {
        await this.basicSignIn();
        return true;
      } catch {
        return false;
      }
    }
    return false;
  }

  /** One request with every header this server needs; a 401/403 re-authenticates once. */
  async request(route: string, init: { method?: string; body?: unknown } = {}): Promise<Response> {
    const url = joinUrl(this.base, route);
    const send = async () =>
      this.fetcher()(url, {
        method: init.method ?? "GET",
        headers: {
          Accept: "application/json",
          ...(init.body === undefined ? {} : { "Content-Type": "application/json" }),
          ...this.proxyHeaders(url),
          ...(await this.authHeaders()),
        },
        ...(init.body === undefined ? {} : { body: JSON.stringify(init.body) }),
        redirect: "manual",
        signal: AbortSignal.timeout(this.deps.timeoutMs ?? REMOTE_PROBE_TIMEOUT_MS),
      });
    let response: Response;
    try {
      response = await send();
    } catch (error) {
      if (error instanceof DialError) throw error;
      throw new DialError("unreachable", "the server did not answer");
    }
    if ((response.status === 401 || response.status === 403) && (await this.reauthenticate())) {
      await response.body?.cancel();
      try {
        response = await send();
      } catch (error) {
        if (error instanceof DialError) throw error;
        throw new DialError("unreachable", "the server did not answer");
      }
    }
    return response;
  }
}

/** A single-use dashboard ticket for one WebSocket dial. */
export async function mintRemoteTicket(
  auth: RemoteAuth,
  options: { purpose?: string; profile?: string | null } = {},
): Promise<{ ticket: string; expiresInMs: number }> {
  const body: Record<string, string> = { purpose: options.purpose ?? DIAL_PURPOSE };
  if (options.profile) body.profile = options.profile;
  const response = await auth.request(TICKET_ROUTE, { method: "POST", body });
  const json = (await response.json().catch(() => null)) as {
    ticket?: unknown;
    expires_in?: unknown;
    error?: unknown;
  } | null;
  if (response.status === 401 || response.status === 403) {
    throw new DialError(
      "unauthorized",
      "the server refused this connection's credential",
      response.status,
    );
  }
  if (response.status === 409) {
    throw new DialError(
      "old-server",
      "this server is too old to give the app a socket ticket; update Indus on it",
      409,
    );
  }
  if (response.status === 429) {
    throw new DialError("busy", "too many sockets are opening; try again", 429);
  }
  if (response.status !== 200 || typeof json?.ticket !== "string" || json.ticket === "") {
    throw new DialError(
      "error",
      `the server's ticket answer was ${response.status}`,
      response.status,
    );
  }
  const seconds = typeof json.expires_in === "number" ? json.expires_in : 30;
  return { ticket: json.ticket, expiresInMs: Math.round(seconds * 1000) };
}

export function dialProtocols(ticket: string): string[] {
  return [DASHBOARD_SUBPROTOCOL, `${DASHBOARD_TICKET_PREFIX}${ticket}`];
}

export interface WebSocketLike {
  addEventListener(
    type: "open" | "message" | "close" | "error",
    listener: (event: unknown) => void,
  ): void;
  close(): void;
}

export interface ConnectionTestDeps extends DialDeps {
  webSocket?: (url: string, protocols: string[], headers: Record<string, string>) => WebSocketLike;
  settleMs?: number;
}

export interface ConnectionTestResult {
  ok: boolean;
  http: Leg;
  ws: Leg;
  /** The server offers native sign-in (`auth_flows`). */
  nativeSignIn: boolean;
  message?: string;
}

function wsProbe(
  url: string,
  protocols: string[],
  headers: Record<string, string>,
  deps: ConnectionTestDeps,
): Promise<Leg> {
  const timeout = deps.timeoutMs ?? REMOTE_PROBE_TIMEOUT_MS;
  const settle = deps.settleMs ?? WS_SETTLE_MS;
  return new Promise((resolve) => {
    let opened = false;
    let done = false;
    let socket: WebSocketLike | null = null;
    let settled: ReturnType<typeof setTimeout> | undefined;
    const finish = (leg: Leg) => {
      if (done) return;
      done = true;
      clearTimeout(deadline);
      clearTimeout(settled);
      try {
        socket?.close();
      } catch {
        // already closed
      }
      resolve(leg);
    };
    const deadline = setTimeout(() => finish(opened ? "ok" : "unreachable"), timeout);
    try {
      socket = (
        deps.webSocket ??
        ((u, p, h) =>
          new WebSocket(u, { protocols: p, headers: h } as unknown as string[]) as WebSocketLike)
      )(url, protocols, headers);
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

function describe(http: Leg, ws: Leg): string {
  if (http === "unauthorized" || ws === "unauthorized") return "the server refused this credential";
  if (http === "unreachable" || ws === "unreachable") return "the server could not be reached";
  if (http === "ok") return "the server answered over HTTP, but its WebSocket did not work";
  return "the server answered with an error";
}

function legOf(error: unknown): Leg {
  if (error instanceof DialError) {
    if (error.kind === "unauthorized") return "unauthorized";
    if (error.kind === "unreachable") return "unreachable";
  }
  return "error";
}

/** Both legs with the real credential (R08.5): HTTP `GET /api/config`, then a ticketed socket. */
export async function testRemoteConnection(
  auth: RemoteAuth,
  deps: ConnectionTestDeps = {},
): Promise<ConnectionTestResult> {
  let nativeSignIn = false;
  try {
    const status = await (deps.fetch ?? fetch)(joinUrl(auth.base, "/api/status"), {
      headers: {
        Accept: "application/json",
        ...auth.proxyHeaders(joinUrl(auth.base, "/api/status")),
      },
      redirect: "manual",
      signal: AbortSignal.timeout(deps.timeoutMs ?? REMOTE_PROBE_TIMEOUT_MS),
    });
    const body = (await status.json().catch(() => null)) as { auth_flows?: unknown } | null;
    nativeSignIn = Array.isArray(body?.auth_flows) && body.auth_flows.includes("native_pkce");
  } catch {
    return {
      ok: false,
      http: "unreachable",
      ws: "error",
      nativeSignIn,
      message: "the server did not answer",
    };
  }
  let http: Leg;
  try {
    const response = await auth.request("/api/config");
    await response.body?.cancel();
    http = classifyStatus(response.status);
  } catch (error) {
    http = legOf(error);
  }
  let ws: Leg;
  try {
    const { ticket } = await mintRemoteTicket(auth);
    const url = remoteWsUrl(auth.base);
    ws = await wsProbe(url, dialProtocols(ticket), auth.proxyHeaders(url), deps);
  } catch (error) {
    ws = legOf(error);
  }
  const ok = http === "ok" && ws === "ok";
  const result: ConnectionTestResult = { ok, http, ws, nativeSignIn };
  if (!ok) result.message = describe(http, ws);
  return result;
}
