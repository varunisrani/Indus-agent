// WebSocket targets for the dashboard's sockets (PTY, events, RPC sidecar). Browsers cannot set
// headers on an upgrade, so the credential rides the subprotocol list, never the URL: the client
// offers `indus-dashboard-v1` plus `indus-dashboard-token.<session token>` in loopback mode, or in
// gated mode `indus-dashboard-ticket.<ticket>` with a single-use ticket minted by a
// cookie-authenticated POST just before each dial (R09.2). The server echoes only
// `indus-dashboard-v1` and refuses a credential found in the query. The value never goes to a log.

import { ApiError } from "./api.ts";
import { authRequired, basePath, sessionToken } from "./bootstrap.ts";

export const WS_TICKET_PATH = "/api/auth/ws-ticket";
export const TICKET_TIMEOUT_MS = 8_000;

/** The protocol every dashboard socket offers and the server echoes. */
export const WS_PROTOCOL = "indus-dashboard-v1";
/** A credential subprotocol is this prefix, the kind, a dot and the value. */
export const WS_CREDENTIAL_PREFIX = "indus-dashboard-";

export type WsCredential = readonly ["token" | "ticket", string];

/** What `new WebSocket(url, protocols)` needs for one dial. */
export interface WsTarget {
  url: string;
  protocols: string[];
}

interface TicketReply {
  ticket: string;
  ttl_seconds?: number;
}

function isTicketReply(value: unknown): value is TicketReply {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { ticket?: unknown }).ticket === "string"
  );
}

/** Mint one ticket for `purpose`; fails with `ApiError(0)` when nothing answers within 8 s. */
export async function fetchWsTicket(purpose: string): Promise<string> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TICKET_TIMEOUT_MS);
  let res: Response;
  try {
    res = await fetch(`${basePath()}${WS_TICKET_PATH}`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ purpose }),
      signal: controller.signal,
    });
  } catch (cause) {
    const timedOut = controller.signal.aborted;
    throw new ApiError(
      0,
      timedOut
        ? "The dashboard did not hand out a connection ticket in time."
        : "The dashboard backend is unreachable.",
      `POST ${WS_TICKET_PATH}: ${timedOut ? "timed out" : String(cause)}`,
    );
  } finally {
    clearTimeout(timer);
  }
  const body: unknown = await res.json().catch(() => undefined);
  if (!res.ok || !isTicketReply(body)) {
    throw new ApiError(
      res.status,
      "The dashboard refused a connection ticket.",
      `POST ${WS_TICKET_PATH} → ${res.status}`,
    );
  }
  return body.ticket;
}

/** The credential for one dial: a fresh ticket in gated mode, else the session token. */
export async function wsCredential(purpose: string): Promise<WsCredential> {
  if (authRequired()) return ["ticket", await fetchWsTicket(purpose)];
  return ["token", sessionToken() ?? ""];
}

/** The subprotocol list that carries `credential` (just the protocol name when it is empty). */
export function wsProtocols([kind, value]: WsCredential): string[] {
  return value ? [WS_PROTOCOL, `${WS_CREDENTIAL_PREFIX}${kind}.${value}`] : [WS_PROTOCOL];
}

/**
 * Absolute `ws(s)://` URL for a dashboard socket path such as `/api/pty` with `params` in the
 * query. It never holds a credential. The scheme follows the page (https → wss).
 */
export function buildWsUrl(path: string, params: Record<string, string | undefined> = {}): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") query.set(key, value);
  }
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  const text = query.toString();
  return `${scheme}//${window.location.host}${basePath()}${path}${text ? `?${text}` : ""}`;
}

/** URL and subprotocols for one dial of `path` (a fresh credential each time). */
export async function wsTarget(
  path: string,
  purpose: string,
  params: Record<string, string | undefined> = {},
): Promise<WsTarget> {
  return { url: buildWsUrl(path, params), protocols: wsProtocols(await wsCredential(purpose)) };
}

/** Open a dashboard socket with its credential in the subprotocols. */
export async function openDashboardSocket(
  path: string,
  purpose: string,
  params: Record<string, string | undefined> = {},
): Promise<WebSocket> {
  const target = await wsTarget(path, purpose, params);
  return new WebSocket(target.url, target.protocols);
}
