// Events the client makes up itself, plus stand-ins for wire names the protocol lacks so far.
// They carry `type` like server events, so one event stream serves the renderer.

import type { Event as ServerEvent } from "@indus/protocol";
import type { ReconnectingEvent } from "./backoff.ts";

/** A stdout line that is not a JSON-RPC frame; never painted to the terminal. */
export interface ProtocolErrorEvent {
  type: "transport.protocol_error";
  /** The start of the offending line, at most 120 characters. */
  preview: string;
}

/** One line the child wrote to stderr. */
export interface StderrEvent {
  type: "transport.stderr";
  line: string;
}

/** No ready event within the startup timeout. */
export interface StartTimeoutEvent {
  type: "transport.start_timeout";
  /** The command line that was started. */
  python: string;
  cwd: string;
  /** The last log lines, oldest first. */
  stderr_tail: string[];
}

/** The child process ended. */
export interface ExitEvent {
  type: "transport.exit";
  code: number | null;
  signal: string | null;
  /** The last stderr line, if any. */
  stderr_last: string | null;
}

/** A WebSocket link closed (P17-20); `terminal` when no retry can help. */
export interface ClosedEvent {
  type: "transport.closed";
  /** The WebSocket close code, null when the link never opened. */
  code: number | null;
  reason: string;
  terminal: boolean;
}

/**
 * Getting attach credentials failed (P17-19: the grant helper). `terminal` failures carry a
 * repair hint and are not retried; the others go to the reconnect backoff.
 */
export interface AttachFailedEvent {
  type: "transport.attach_failed";
  terminal: boolean;
  code: string;
  message: string;
  hint: string | null;
  /** True when the surface carries on with a local agent instead. */
  fallback: boolean;
}

export type TransportEvent =
  | ProtocolErrorEvent
  | StderrEvent
  | StartTimeoutEvent
  | ExitEvent
  | ClosedEvent
  | AttachFailedEvent
  | ReconnectingEvent;

/**
 * STUB (protocol gap): the bridge's first frame. Not declared in `indus.protocol` yet; slice G
 * owns adding it. Shape assumed here: a notification with this method and these params.
 */
export const READY_METHOD = "gateway.ready";

export interface ReadyEvent {
  type: "gateway.ready";
  /** The server's `PROTOCOL_VERSION`. */
  protocol_version?: number;
  /** True when the server answers `gateway.ping`. */
  heartbeat?: boolean;
  /** The daemon's replay epoch (WebSocket attach): event cursors are valid within it. */
  replay_epoch?: string | null;
  /** The profile the daemon serves (WebSocket attach). */
  profile_id?: string | null;
  /** This daemon start (WebSocket attach). */
  instance_id?: string | null;
  /** What the daemon offers attached clients (for example `request.decline`). */
  capabilities?: string[] | null;
}

/** STUB (protocol gap): the keep-alive method; any answer, even an error frame, counts. */
export const PING_METHOD = "gateway.ping";

/** Everything a client subscriber may see. */
export type ClientEvent = ServerEvent | TransportEvent | ReadyEvent;
