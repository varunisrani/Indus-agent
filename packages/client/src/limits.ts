// Every number the client layer runs on, in one place (research 05 §3.30 limits table).

export const DEFAULT_RPC_TIMEOUT_MS = 120_000;
export const RPC_TIMEOUT_FLOOR_MS = 30_000;
export const RPC_TIMEOUT_ENV = "INDUS_TUI_RPC_TIMEOUT_MS";

export const DEFAULT_STARTUP_TIMEOUT_MS = 15_000;
export const STARTUP_TIMEOUT_FLOOR_MS = 5_000;
export const STARTUP_TIMEOUT_ENV = "INDUS_TUI_STARTUP_TIMEOUT_MS";
/** Log lines attached to a start-timeout event. */
export const START_TIMEOUT_TAIL_LINES = 20;

export const HEARTBEAT_INTERVAL_MS = 15_000;
export const HEARTBEAT_DEADLINE_MS = 45_000;
export const HEARTBEAT_MAX_OUTSTANDING = 8;

export const BACKOFF_BASE_MS = 1_000;
export const BACKOFF_CAP_MS = 30_000;

export const EVENT_BUFFER_CAPACITY = 2_000;
/** Server requests held before the handler table is installed. */
export const HELD_REQUEST_CAPACITY = 256;
/** Recently answered server-request ids remembered to drop a second answer or delivery. */
export const ANSWERED_ID_MEMORY = 512;

export const LOG_RING_LINES = 200;
export const LOG_LINE_MAX_BYTES = 4_096;

export const PROTOCOL_ERROR_PREVIEW_CHARS = 120;
/** A stdout line longer than this is dropped as a protocol error instead of buffered. */
export const MAX_FRAME_LINE_BYTES = 64 * 1024 * 1024;

export const CLOSE_GRACE_MS = 2_000;

/**
 * Read a millisecond setting from its raw env text: absent or unparsable → `fallback`;
 * otherwise never below `floor`.
 */
export function msFromEnv(raw: string | undefined, fallback: number, floor: number): number {
  if (raw === undefined || raw.trim() === "") return fallback;
  const value = Number(raw.trim());
  if (!Number.isFinite(value) || value <= 0) return fallback;
  return Math.max(floor, Math.round(value));
}

/** The default call timeout from `INDUS_TUI_RPC_TIMEOUT_MS` (floor 30,000 ms). */
export function rpcTimeoutFromEnv(raw: string | undefined): number {
  return msFromEnv(raw, DEFAULT_RPC_TIMEOUT_MS, RPC_TIMEOUT_FLOOR_MS);
}

/** The ready deadline from `INDUS_TUI_STARTUP_TIMEOUT_MS` (floor 5,000 ms). */
export function startupTimeoutFromEnv(raw: string | undefined): number {
  return msFromEnv(raw, DEFAULT_STARTUP_TIMEOUT_MS, STARTUP_TIMEOUT_FLOOR_MS);
}
