// Every number the renderer's gateway connection runs on (P20-17), in one place.

/** Any call without its own deadline. */
export const RPC_DEFAULT_TIMEOUT_MS = 120_000;
/** From dialling to the daemon's `gateway.ready`. */
export const RPC_STARTUP_TIMEOUT_MS = 60_000;
/** A submitted prompt may wait this long for its admission answer. */
export const PROMPT_SUBMIT_TIMEOUT_MS = 1_800_000;
/** An answer to an approval (or any server request): the daemon keeps a card open 300 s. */
export const SERVER_REQUEST_ANSWER_TIMEOUT_MS = 330_000;

/** Reconnect ladder: full jitter in [0, step), step = base · 2^(attempt−1) capped. */
export const RECONNECT_BASE_MS = 300;
export const RECONNECT_CAP_MS = 15_000;
/** A socket that stayed open this long resets the ladder. */
export const RECONNECT_RESET_AFTER_OPEN_MS = 5_000;
/** Continuous failure for this long raises the (non-blocking) warning. */
export const RECONNECT_WARNING_AFTER_MS = 300_000;

/** Each per-session resubscribe after a reconnect. */
export const REPLAY_REQUEST_TIMEOUT_MS = 10_000;
/** Sessions that keep a replay cursor at most. */
export const REPLAY_CURSOR_LIMIT = 256;

/** A running turn with no event for this long is probed. */
export const TURN_WATCHDOG_SILENCE_MS = 300_000;
/** The watchdog's probe deadline. */
export const TURN_WATCHDOG_PROBE_TIMEOUT_MS = 15_000;

/** One liveness ping (window woke, network back). */
export const LIVENESS_PROBE_TIMEOUT_MS = 5_000;
/** After one inconclusive miss during a turn, probe again this much later. */
export const LIVENESS_REPROBE_DELAY_MS = 3_000;
/** Consecutive misses (during a turn) that rebuild the transport. */
export const LIVENESS_FAILURE_STREAK = 2;

/** The daemon's longer deadlines (`rpc.dispatch.DEADLINES`) plus a little room for the answer. */
const LONG_METHODS: Readonly<Record<string, number>> = {
  "pet.generate": 450_000, // the drawing step (P24-19): 435 s on the daemon
  "pet.hatch": 3_630_000, // every mood drawn: 3615 s on the daemon
  "wake.start": 190_000, // a first start may load a model (P24-20): 180 s
  "voice.transcribe": 310_000, // the speech providers (P24-35): 300 s
  "voice.speak": 310_000,
};

const METHOD_TIMEOUTS: Readonly<Record<string, number>> = {
  "prompt.submit": PROMPT_SUBMIT_TIMEOUT_MS,
  "server_request.answer": SERVER_REQUEST_ANSWER_TIMEOUT_MS,
  "server_request.decline": SERVER_REQUEST_ANSWER_TIMEOUT_MS,
  ...LONG_METHODS,
};

/** The default deadline of one method. */
export function methodTimeoutMs(method: string): number {
  return METHOD_TIMEOUTS[method] ?? RPC_DEFAULT_TIMEOUT_MS;
}
