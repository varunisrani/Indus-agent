// Native sign-in to a remote Indus (P24-29), RFC 8252 (OAuth 2.0 for native apps: system browser,
// loopback redirect) with RFC 7636 PKCE (S256). The server is the authorization server for the
// desktop and brokers the upstream identity provider; the desktop gets its own access token and a
// rotating refresh token, kept only in the secrets store (keychain), never in a file in plaintext,
// a URL the renderer sees, a log line or an error message.
//
// Server interface (advertised by `GET /api/status` → `auth_flows: ["native_pkce", …]`):
//   GET  <base>/auth/native/authorize?code_challenge&code_challenge_method=S256&redirect_uri&state
//   POST <base>/auth/native/token   {grant_type: authorization_code, code, code_verifier, redirect_uri}
//   POST <base>/auth/native/refresh {grant_type: refresh_token, refresh_token}
// both answering {access_token, refresh_token?, expires_in? | expires_at?}. A server that does not
// advertise the flow is used in token mode.
//
// Rules: one loopback listener per sign-in on a random 127.0.0.1 port, path `/callback`; exactly
// one callback is accepted and only with the state we sent (a mismatch is rejected and its code is
// never redeemed); the listener gives up after 5 minutes. Tokens refresh 60 s before they expire
// (an unknown expiry refreshes before use); one refresh runs at a time and every caller waits for
// it; a rotated refresh token replaces the old one; a refresh answered 400 or 401 clears the set
// (sign in again), anything else keeps it.

import { createHash, randomBytes } from "node:crypto";
import http from "node:http";
import type { AddressInfo } from "node:net";

export const NATIVE_FLOW = "native_pkce";
export const CALLBACK_PATH = "/callback";
export const LISTENER_TIMEOUT_MS = 5 * 60_000;
export const REFRESH_SKEW_S = 60;
export const OAUTH_REQUEST_TIMEOUT_MS = 15_000;

export type OAuthErrorKind =
  | "state-mismatch"
  | "denied"
  | "missing-code"
  | "timeout"
  | "bad-answer"
  | "signed-out"
  | "unreachable";

export class OAuthError extends Error {
  readonly kind: OAuthErrorKind;

  constructor(kind: OAuthErrorKind, message: string) {
    super(message);
    this.kind = kind;
  }
}

export interface TokenSet {
  accessToken: string;
  refreshToken: string;
  /** Unix seconds; 0 = unknown. */
  expiresAt: number;
}

function b64url(bytes: Buffer): string {
  return bytes.toString("base64url");
}

export function pkcePair(random: (n: number) => Buffer = randomBytes): {
  verifier: string;
  challenge: string;
  method: "S256";
} {
  const verifier = b64url(random(32));
  const challenge = b64url(createHash("sha256").update(verifier, "ascii").digest());
  return { verifier, challenge, method: "S256" };
}

export function newState(random: (n: number) => Buffer = randomBytes): string {
  return b64url(random(24));
}

export function offersNativeSignIn(status: unknown): boolean {
  const flows = (status as { auth_flows?: unknown } | null)?.auth_flows;
  return Array.isArray(flows) && flows.includes(NATIVE_FLOW);
}

function endpoint(base: string, route: string): URL {
  const url = new URL(base);
  url.pathname = `${url.pathname.replace(/\/+$/, "")}${route}`;
  url.search = "";
  url.hash = "";
  return url;
}

export function authorizeUrl(
  base: string,
  params: { challenge: string; redirectUri: string; state: string },
): string {
  const url = endpoint(base, "/auth/native/authorize");
  url.searchParams.set("code_challenge", params.challenge);
  url.searchParams.set("code_challenge_method", "S256");
  url.searchParams.set("redirect_uri", params.redirectUri);
  url.searchParams.set("state", params.state);
  return url.toString();
}

/** The code of a loopback callback (`/callback?code&state` or `?error`), checked against `state`. */
export function parseCallback(requestUrl: string, expectedState: string): string {
  const url = new URL(requestUrl, "http://127.0.0.1");
  const error = url.searchParams.get("error");
  if (error !== null) {
    const text = (url.searchParams.get("error_description") ?? "").slice(0, 200);
    throw new OAuthError(
      "denied",
      `the server refused the sign-in: ${error}${text ? ` (${text})` : ""}`,
    );
  }
  const state = url.searchParams.get("state") ?? "";
  if (expectedState === "" || state !== expectedState) {
    throw new OAuthError("state-mismatch", "the sign-in answer did not match this sign-in");
  }
  const code = url.searchParams.get("code") ?? "";
  if (code === "") throw new OAuthError("missing-code", "the sign-in answer had no code");
  return code;
}

/** A token answer → a set (snake_case wire names); `previous` keeps a refresh token not rotated. */
export function parseTokens(body: unknown, nowS: number, previous?: TokenSet): TokenSet {
  const record = (body ?? {}) as Record<string, unknown>;
  const accessToken = typeof record.access_token === "string" ? record.access_token : "";
  if (accessToken === "") throw new OAuthError("bad-answer", "the server sent no access token");
  const refresh =
    typeof record.refresh_token === "string" && record.refresh_token !== ""
      ? record.refresh_token
      : (previous?.refreshToken ?? "");
  let expiresAt = 0;
  if (typeof record.expires_at === "number" && Number.isFinite(record.expires_at)) {
    expiresAt = record.expires_at;
  } else if (typeof record.expires_in === "number" && Number.isFinite(record.expires_in)) {
    expiresAt = nowS + record.expires_in;
  }
  return { accessToken, refreshToken: refresh, expiresAt };
}

export function needsRefresh(tokens: TokenSet, nowS: number, skewS = REFRESH_SKEW_S): boolean {
  if (!(tokens.expiresAt > 0)) return true;
  return nowS >= tokens.expiresAt - skewS;
}

export function encodeTokens(tokens: TokenSet): string {
  return JSON.stringify(tokens);
}

export function decodeTokens(stored: string | null): TokenSet | null {
  if (stored === null || stored === "") return null;
  try {
    const raw = JSON.parse(stored) as Partial<TokenSet>;
    if (typeof raw.accessToken !== "string" || raw.accessToken === "") return null;
    return {
      accessToken: raw.accessToken,
      refreshToken: typeof raw.refreshToken === "string" ? raw.refreshToken : "",
      expiresAt: typeof raw.expiresAt === "number" ? raw.expiresAt : 0,
    };
  } catch {
    return null;
  }
}

// -- the loopback listener ------------------------------------------------------------------------

export interface Loopback {
  redirectUri: string;
  /** The code of the one accepted callback. */
  code: Promise<string>;
  close(): void;
}

const DONE_PAGE =
  "<!doctype html><meta charset=utf-8><title>Indus</title><p>You can close this tab and return to Indus.</p>";

/** Listen on a random loopback port for exactly one callback carrying `state`. */
export function listenForCallback(
  state: string,
  options: { timeoutMs?: number | undefined; host?: string } = {},
): Promise<Loopback> {
  const host = options.host ?? "127.0.0.1";
  return new Promise((resolveListener, rejectListener) => {
    let settle: { resolve: (code: string) => void; reject: (error: Error) => void } | null = null;
    let finished = false;
    const code = new Promise<string>((resolve, reject) => {
      settle = { resolve, reject };
    });
    code.catch(() => undefined);
    const server = http.createServer((request, response) => {
      const url = new URL(request.url ?? "/", `http://${host}`);
      if (request.method !== "GET" || url.pathname !== CALLBACK_PATH) {
        response.writeHead(404).end();
        return;
      }
      if (finished) {
        response.writeHead(400, { "Content-Type": "text/plain" }).end("already handled");
        return;
      }
      try {
        const found = parseCallback(request.url ?? "", state);
        finish(() => settle?.resolve(found));
        response.writeHead(200, { "Content-Type": "text/html", "Cache-Control": "no-store" });
        response.end(DONE_PAGE);
      } catch (error) {
        finish(() => settle?.reject(error as Error));
        response.writeHead(400, { "Content-Type": "text/plain" }).end("sign-in refused");
      }
    });
    const timer = setTimeout(
      () => finish(() => settle?.reject(new OAuthError("timeout", "the sign-in took too long"))),
      options.timeoutMs ?? LISTENER_TIMEOUT_MS,
    );
    const finish = (then: () => void) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      then();
      setImmediate(() => server.close());
    };
    server.on("error", (error) => rejectListener(error));
    server.listen(0, host, () => {
      const port = (server.address() as AddressInfo).port;
      resolveListener({
        redirectUri: `http://${host}:${port}${CALLBACK_PATH}`,
        code,
        close: () =>
          finish(() => settle?.reject(new OAuthError("timeout", "the sign-in was cancelled"))),
      });
    });
  });
}

// -- flows ----------------------------------------------------------------------------------------

export interface OAuthDeps {
  fetch?: typeof fetch;
  nowS?: () => number;
  openBrowser: (url: string) => Promise<void> | void;
  random?: (n: number) => Buffer;
  timeoutMs?: number;
}

async function postJson(
  deps: OAuthDeps,
  url: URL,
  body: Record<string, string>,
): Promise<{ status: number; json: unknown }> {
  let response: Response;
  try {
    response = await (deps.fetch ?? fetch)(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
      redirect: "error",
      signal: AbortSignal.timeout(OAUTH_REQUEST_TIMEOUT_MS),
    });
  } catch {
    throw new OAuthError("unreachable", "the server did not answer");
  }
  const json: unknown = await response.json().catch(() => null);
  return { status: response.status, json };
}

/** The whole sign-in: listener, browser, callback, code exchange → a token set. */
export async function signIn(base: string, deps: OAuthDeps): Promise<TokenSet> {
  const pair = pkcePair(deps.random);
  const state = newState(deps.random);
  const loopback = await listenForCallback(state, { timeoutMs: deps.timeoutMs });
  try {
    await deps.openBrowser(
      authorizeUrl(base, { challenge: pair.challenge, redirectUri: loopback.redirectUri, state }),
    );
    const code = await loopback.code;
    const answer = await postJson(deps, endpoint(base, "/auth/native/token"), {
      grant_type: "authorization_code",
      code,
      code_verifier: pair.verifier,
      redirect_uri: loopback.redirectUri,
    });
    if (answer.status !== 200) {
      throw new OAuthError("denied", `the server refused the sign-in (${answer.status})`);
    }
    return parseTokens(answer.json, (deps.nowS ?? nowSeconds)());
  } finally {
    loopback.close();
  }
}

function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

export interface TokenVault {
  get(): string | null;
  put(value: string): void;
  delete(): void;
}

/** One connection's signed-in state: a fresh access token on demand, refreshed single-flight. */
export class OAuthSession {
  private readonly base: string;
  private readonly vault: TokenVault;
  private readonly deps: Omit<OAuthDeps, "openBrowser">;
  private inflight: Promise<TokenSet> | null = null;

  constructor(base: string, vault: TokenVault, deps: Omit<OAuthDeps, "openBrowser"> = {}) {
    this.base = base;
    this.vault = vault;
    this.deps = deps;
  }

  signedIn(): boolean {
    return decodeTokens(this.vault.get()) !== null;
  }

  store(tokens: TokenSet): void {
    this.vault.put(encodeTokens(tokens));
  }

  signOut(): void {
    this.vault.delete();
  }

  /** A usable access token; refreshes when near expiry. */
  async accessToken(): Promise<string> {
    const tokens = decodeTokens(this.vault.get());
    if (tokens === null) throw new OAuthError("signed-out", "sign in to this server again");
    const now = (this.deps.nowS ?? nowSeconds)();
    if (!needsRefresh(tokens, now)) return tokens.accessToken;
    return (await this.refresh(tokens)).accessToken;
  }

  /** After an explicit 401/403: try one refresh; a refused refresh means sign in again. */
  async afterUnauthorized(): Promise<string> {
    const tokens = decodeTokens(this.vault.get());
    if (tokens === null) throw new OAuthError("signed-out", "sign in to this server again");
    return (await this.refresh({ ...tokens, expiresAt: 0 })).accessToken;
  }

  private refresh(current: TokenSet): Promise<TokenSet> {
    if (this.inflight !== null) return this.inflight;
    const run = (async () => {
      if (current.refreshToken === "") {
        this.vault.delete();
        throw new OAuthError("signed-out", "sign in to this server again");
      }
      const answer = await postJson(
        { ...this.deps, openBrowser: () => undefined },
        endpoint(this.base, "/auth/native/refresh"),
        { grant_type: "refresh_token", refresh_token: current.refreshToken },
      );
      if (answer.status === 400 || answer.status === 401) {
        this.vault.delete();
        throw new OAuthError("signed-out", "sign in to this server again");
      }
      if (answer.status !== 200) {
        throw new OAuthError(
          "unreachable",
          `the server could not refresh the sign-in (${answer.status})`,
        );
      }
      const next = parseTokens(answer.json, (this.deps.nowS ?? nowSeconds)(), current);
      this.vault.put(encodeTokens(next));
      return next;
    })();
    this.inflight = run;
    void run.then(
      () => {
        this.inflight = null;
      },
      () => {
        this.inflight = null;
      },
    );
    return run;
  }
}
