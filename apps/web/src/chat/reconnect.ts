// What the chat terminal does after its socket closes (P18-26): the timing constants, the
// reconnect ladder and the verdict for each close code, with the words the person sees. Pure: no
// socket, no DOM, so every rule is testable on its own. Close codes never appear in the copy; the
// connection logs them to the console instead.

/** First reconnect delay; each later one doubles up to the cap. */
export const RECONNECT_BASE_MS = 250;
export const RECONNECT_CAP_MS = 3_000;
/** Automatic attempts before the ladder stops and only a manual reconnect is offered. */
export const RECONNECT_ATTEMPTS = 5;
/** Page-return events (visible, focus, online) start at most one attempt per this period. */
export const RESUME_THROTTLE_MS = 1_000;
/** From the start of a dial (ticket fetch included) until the socket must be open. */
export const OPEN_DEADLINE_MS = 8_000;
/** Silence after which a resize frame is sent to keep idle-closing proxies off the socket. */
export const KEEPALIVE_MS = 20_000;
/** How long after a resume the output is sanitised (R09.8). */
export const SANITIZE_WINDOW_MS = 30_000;

/** Delay before automatic attempt `attempt` (1-based): 250, 500, 1000, 2000, 3000 ms. */
export function reconnectDelay(attempt: number): number {
  const step = Math.max(0, Math.floor(attempt) - 1);
  return Math.min(RECONNECT_CAP_MS, RECONNECT_BASE_MS * 2 ** step);
}

/** Counts automatic attempts in one outage. */
export class ReconnectLadder {
  #used = 0;
  readonly limit: number;

  constructor(limit: number = RECONNECT_ATTEMPTS) {
    this.limit = limit;
  }

  /** Attempts used since the last successful open. */
  get used(): number {
    return this.#used;
  }

  get exhausted(): boolean {
    return this.#used >= this.limit;
  }

  /** Claim the next attempt: its delay, or null when the ladder is used up. */
  climb(): number | null {
    if (this.exhausted) return null;
    this.#used += 1;
    return reconnectDelay(this.#used);
  }

  reset(): void {
    this.#used = 0;
  }
}

export const CLOSE = {
  normal: 1000,
  goingAway: 1001,
  abnormal: 1006,
  startFailed: 1011,
  restarting: 1012,
  tryAgain: 1013,
  credential: 4401,
  origin: 4403,
  disabled: 4404,
  peer: 4408,
  superseded: 4409,
  exited: 4410,
} as const;

export const COPY = {
  reconnecting: "The chat connection dropped. Reconnecting…",
  gaveUp:
    "The chat lost its connection to the dashboard server. If you stopped indus dashboard, start it again; otherwise reconnect.",
  inputRefused: "The chat is not connected right now, so this input was not sent.",
  exited:
    "This chat session has ended. If you did not end it, open Logs to see what happened, or start a new session.",
  startFailed: "The chat could not start. The terminal above says why.",
  superseded: "This chat terminal moved to another tab or window.",
  loginExpired: "Your dashboard login expired (the server may have restarted). Reload the page.",
  origin:
    "The dashboard refused this chat because the page address does not match the server. Open the address indus dashboard printed.",
  disabled: "This dashboard server does not offer the chat terminal.",
  peer: "This dashboard only takes chat connections from the machine it runs on.",
  endedLine: "[chat session ended]",
} as const;

export type CloseVerdict =
  /** 4401 in loopback mode, first time: the token rotated; reload the page once. */
  | { kind: "reload" }
  /** Refused before any PTY existed; retrying cannot help. */
  | { kind: "rejected"; message: string; reload: boolean }
  /** 1011: the server could not start the chat and printed why in the terminal. */
  | { kind: "start-failed" }
  /** 4410 or a clean 1000: the TUI process ended. */
  | { kind: "exited" }
  /** 4409: another socket took this PTY over; stay quiet. */
  | { kind: "superseded" }
  /** Anything transient: climb the ladder. */
  | { kind: "retry" };

export interface CloseContext {
  /** Gated mode (a login provider guards the dashboard). */
  gated: boolean;
  /** False when the one allowed reload already happened (or storage cannot remember it). */
  mayReload: boolean;
}

/** What to do after a close with `code` (`clean` is the event's `wasClean`). */
export function judgeClose(code: number, clean: boolean, context: CloseContext): CloseVerdict {
  switch (code) {
    case CLOSE.credential:
      if (!context.gated && context.mayReload) return { kind: "reload" };
      return { kind: "rejected", message: COPY.loginExpired, reload: true };
    case CLOSE.origin:
      return { kind: "rejected", message: COPY.origin, reload: false };
    case CLOSE.disabled:
      return { kind: "rejected", message: COPY.disabled, reload: true };
    case CLOSE.peer:
      return { kind: "rejected", message: COPY.peer, reload: false };
    case CLOSE.startFailed:
      return { kind: "start-failed" };
    case CLOSE.exited:
      return { kind: "exited" };
    case CLOSE.superseded:
      return { kind: "superseded" };
    case CLOSE.goingAway:
    case CLOSE.abnormal:
    case CLOSE.restarting:
    case CLOSE.tryAgain:
      return { kind: "retry" };
    case CLOSE.normal:
      return clean ? { kind: "exited" } : { kind: "retry" };
    default:
      return clean ? { kind: "exited" } : { kind: "retry" };
  }
}
