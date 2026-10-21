// After a reconnect: subscribe every watched session again from its cursor. The daemon replays
// what this window missed (ahead of its answer) and re-offers still-open server requests marked
// `replayed`; the client's cursors drop anything already applied. Each call is bounded, the
// sessions go one after another, and the whole pass stops as soon as a newer connection exists
// (its own pass owns the cursors then, R08.19).

import type { CallOptions } from "@indus/client";
import { REPLAY_CURSOR_LIMIT, REPLAY_REQUEST_TIMEOUT_MS } from "./timing.ts";

export interface ReplayTarget {
  subscribeSession(sessionId: string, options?: CallOptions): Promise<unknown>;
}

export interface ReplayPassOptions {
  /** True while the connection that started the pass is still the current one. */
  isCurrent: () => boolean;
  timeoutMs?: number;
  limit?: number;
  /** A session the daemon no longer knows, or one call that failed. */
  onFailure?: (sessionId: string, error: unknown) => void;
}

export interface ReplayPassResult {
  resubscribed: string[];
  failed: string[];
  /** True when a newer connection cut the pass short. */
  superseded: boolean;
}

/** Newest-touched sessions last in `sessions`; at most `limit` of the newest are replayed. */
export async function replayWatchedSessions(
  target: ReplayTarget,
  sessions: readonly string[],
  options: ReplayPassOptions,
): Promise<ReplayPassResult> {
  const limit = Math.max(0, options.limit ?? REPLAY_CURSOR_LIMIT);
  const chosen = sessions.slice(Math.max(0, sessions.length - limit));
  const result: ReplayPassResult = { resubscribed: [], failed: [], superseded: false };
  for (const sessionId of chosen) {
    if (!options.isCurrent()) {
      result.superseded = true;
      return result;
    }
    try {
      await target.subscribeSession(sessionId, {
        timeoutMs: options.timeoutMs ?? REPLAY_REQUEST_TIMEOUT_MS,
      });
      if (!options.isCurrent()) {
        result.superseded = true;
        return result;
      }
      result.resubscribed.push(sessionId);
    } catch (error) {
      if (!options.isCurrent()) {
        result.superseded = true;
        return result;
      }
      result.failed.push(sessionId);
      options.onFailure?.(sessionId, error);
    }
  }
  return result;
}
