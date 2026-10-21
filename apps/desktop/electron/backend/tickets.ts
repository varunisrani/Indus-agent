// One ticket per WebSocket dial (P20-10, R08.4). A window asks `connection.ticket`; main mints a
// fresh single-use ticket, keeps a reservation for it and answers the ticket plus the exact
// subprotocol list to offer. The renderer builds the WebSocket URL without any credential.
//
// The reservation exists for main's `onBeforeSendHeaders` hook: the daemon refuses browser
// origins, so for exactly the reserved upgrade — same window, same origin and path, offering the
// reserved ticket, within 25 s — main removes the `Origin` header; every other request passes
// untouched. Chromium does not show a WebSocket upgrade's `Sec-WebSocket-Protocol` to that hook
// (and ignores a rewritten one), so inside Electron a local upgrade is matched by window and exact
// URL instead (the oldest live local reservation; the daemon still redeems the ticket). For a
// remote connection (P20-11) the renderer offers a reservation id instead of a credential and main
// swaps that one subprotocol entry for the saved token on that upgrade, so the token never crosses
// the bridge — this needs the subprotocol header, so it works where the hook sees it (Node, tests)
// but not inside Electron 44 (integration finding; the owner decides the remote dial).

import { randomBytes } from "node:crypto";

export const RESERVATION_TTL_MS = 25_000;
export const MAX_PENDING_DIALS = 100;
export const GATEWAY_SUBPROTOCOL = "indus-gateway-v1";
export const GATEWAY_TICKET_PREFIX = "indus-gateway-ticket.";
export const DASHBOARD_SUBPROTOCOL = "indus-dashboard-v1";
export const DASHBOARD_TICKET_PREFIX = "indus-dashboard-ticket.";
export const DASHBOARD_TOKEN_PREFIX = "indus-dashboard-token.";

export interface TicketAnswer {
  ticket: string;
  wsUrl: string;
  expiresInMs: number;
  /** Offer exactly these subprotocols, in this order. */
  protocols: string[];
}

interface Reservation {
  /** The subprotocol entry that identifies the dial. */
  marker: string;
  /** For a remote dial: what that entry becomes on the wire. */
  replaceWith: string | null;
  webContentsId: number;
  expires: number;
  origin: string;
  pathname: string;
}

export interface UpgradeDetails {
  url: string;
  webContentsId?: number | undefined;
  resourceType: string;
  requestHeaders: Record<string, string>;
}

export class TooManyDials extends Error {}

function header(headers: Record<string, string>, name: string): [string, string] | null {
  for (const [key, value] of Object.entries(headers)) {
    if (key.toLowerCase() === name) return [key, value];
  }
  return null;
}

export class DialReservations {
  private readonly pending = new Map<string, Reservation>();
  private readonly now: () => number;

  constructor(now: () => number = Date.now) {
    this.now = now;
  }

  size(): number {
    this.prune();
    return this.pending.size;
  }

  private prune(): void {
    const now = this.now();
    for (const [key, value] of this.pending) if (value.expires <= now) this.pending.delete(key);
  }

  private add(
    url: string,
    webContentsId: number,
    marker: string,
    replaceWith: string | null,
  ): void {
    this.prune();
    if (this.pending.size >= MAX_PENDING_DIALS) throw new TooManyDials("too many pending dials");
    const parsed = new URL(url);
    this.pending.set(marker, {
      marker,
      replaceWith,
      webContentsId,
      expires: this.now() + RESERVATION_TTL_MS,
      origin: parsed.origin,
      pathname: parsed.pathname,
    });
  }

  /** A local daemon dial: `ticket` is offered as `indus-gateway-ticket.<ticket>`. */
  reserveLocal(url: string, ticket: string, webContentsId: number): TicketAnswer {
    const marker = `${GATEWAY_TICKET_PREFIX}${ticket}`;
    this.add(url, webContentsId, marker, null);
    return {
      ticket,
      wsUrl: url,
      expiresInMs: RESERVATION_TTL_MS,
      protocols: [GATEWAY_SUBPROTOCOL, marker],
    };
  }

  /** A remote dial: a reservation id stands in for the token until main rewrites the upgrade. */
  reserveRemote(url: string, token: string, webContentsId: number): TicketAnswer {
    const id = randomBytes(24).toString("base64url");
    const marker = `${DASHBOARD_TICKET_PREFIX}${id}`;
    this.add(url, webContentsId, marker, `${DASHBOARD_TOKEN_PREFIX}${token}`);
    return {
      ticket: id,
      wsUrl: url,
      expiresInMs: RESERVATION_TTL_MS,
      protocols: [DASHBOARD_SUBPROTOCOL, marker],
    };
  }

  /**
   * For `session.webRequest.onBeforeSendHeaders`: the headers to send for the reserved upgrade
   * (Origin removed; a remote marker swapped for the credential), or null to leave the request
   * as it is. A matching reservation is consumed (single use) even when it has expired.
   */
  rewrite(details: UpgradeDetails): Record<string, string> | null {
    if (details.resourceType !== "webSocket") return null;
    const offered = header(details.requestHeaders, "sec-websocket-protocol");
    const entries = offered === null ? null : offered[1].split(",").map((entry) => entry.trim());
    let parsed: URL;
    try {
      parsed = new URL(details.url);
    } catch {
      return null;
    }
    const marker =
      entries === null
        ? this.localDialFor(details.webContentsId, parsed)
        : entries.find((entry) => this.pending.has(entry));
    if (marker === undefined) return null;
    const reservation = this.pending.get(marker) as Reservation;
    if (
      reservation.webContentsId !== details.webContentsId ||
      reservation.origin !== parsed.origin ||
      reservation.pathname !== parsed.pathname ||
      parsed.search !== ""
    ) {
      return null;
    }
    this.pending.delete(marker);
    if (reservation.expires <= this.now()) return null;
    const headers: Record<string, string> = {};
    for (const [key, value] of Object.entries(details.requestHeaders)) {
      if (key.toLowerCase() === "origin") continue;
      headers[key] = value;
    }
    if (reservation.replaceWith !== null && offered !== null && entries !== null) {
      headers[offered[0]] = entries
        .map((entry) => (entry === marker ? (reservation.replaceWith as string) : entry))
        .join(", ");
    }
    return headers;
  }

  /** The oldest live local reservation of this window for exactly this URL (no subprotocols seen). */
  private localDialFor(webContentsId: number | undefined, url: URL): string | undefined {
    if (url.search !== "") return undefined;
    this.prune();
    for (const reservation of this.pending.values()) {
      if (
        reservation.replaceWith === null &&
        reservation.webContentsId === webContentsId &&
        reservation.origin === url.origin &&
        reservation.pathname === url.pathname
      ) {
        return reservation.marker;
      }
    }
    return undefined;
  }
}
