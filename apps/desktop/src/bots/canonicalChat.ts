// A bot's forever-chat in the renderer (P24-31). Identity is the daemon's: (profile, canonical
// title), resolved by `bots.open` (hidden rows included, compression followed to the live tip,
// adopt before mint — `service/bots_rpc.py`). The renderer never stores a session-id pointer: it
// asks every time, keeps one in-flight open per bot (a double click makes one request, so one
// chat), and remembers the ids it was told only to recognise a canonical chat on screen and to keep
// canonical chats out of the sessions list. Side-chats of a bot are ordinary sessions and stay.

export const CANONICAL_TITLE = "Bot chat";
export const BOTS_SOURCE = "bots";

/** The daemon methods the Bots surface calls (`bots.*`, `groups.*`). */
export interface RpcLike {
  request(method: string, params: Record<string, unknown>): Promise<unknown>;
  /** The connection's notifications (`groups.event`, `gateway.ready`, ...); absent: none. */
  onEvent?(listener: (event: { type: string } & Record<string, unknown>) => void): () => void;
}

export interface CanonicalRow {
  readonly session_id: string;
}

export interface SessionRowLike {
  readonly id: string;
  readonly title?: string | null;
  readonly source?: string | null;
  readonly lineage_root?: string | null;
  readonly durableId?: string;
}

export class CanonicalChats {
  private readonly rpc: RpcLike;
  private readonly inflight = new Map<string, Promise<string>>();
  private readonly seen = new Map<string, Set<string>>();

  constructor(rpc: RpcLike) {
    this.rpc = rpc;
  }

  /** Remember what `bots.list` / `profiles.list` reported for a bot. */
  remember(profile: string, canonical: CanonicalRow | null): void {
    if (canonical === null) return;
    const ids = this.seen.get(profile) ?? new Set<string>();
    ids.add(canonical.session_id);
    this.seen.set(profile, ids);
  }

  /** The live tip of the bot's forever-chat, created by the daemon only when none exists. */
  open(profile: string): Promise<string> {
    const running = this.inflight.get(profile);
    if (running !== undefined) return running;
    const run = (async () => {
      const answer = (await this.rpc.request("bots.open", { profile })) as { session_id?: unknown };
      if (typeof answer?.session_id !== "string" || answer.session_id === "") {
        throw new Error("the daemon did not name the bot's chat");
      }
      this.remember(profile, { session_id: answer.session_id });
      return answer.session_id;
    })();
    this.inflight.set(profile, run);
    const clear = () => {
      if (this.inflight.get(profile) === run) this.inflight.delete(profile);
    };
    run.then(clear, clear);
    return run;
  }

  /** Is `sessionId` (any segment we were told of) some bot's forever-chat? */
  isCanonical(sessionId: string): boolean {
    for (const ids of this.seen.values()) if (ids.has(sessionId)) return true;
    return false;
  }

  /** The owner of a canonical chat id, or null. */
  ownerOf(sessionId: string): string | null {
    for (const [profile, ids] of this.seen) if (ids.has(sessionId)) return profile;
    return null;
  }

  /** `rows` without canonical chats (known ids, or the canonical title on a bots row). */
  withoutCanonical<R extends SessionRowLike>(rows: readonly R[]): R[] {
    return rows.filter((row) => {
      const ids = [row.id, row.lineage_root ?? "", row.durableId ?? ""];
      if (ids.some((id) => id !== "" && this.isCanonical(id))) return false;
      return !(row.title === CANONICAL_TITLE && row.source === BOTS_SOURCE);
    });
  }
}
