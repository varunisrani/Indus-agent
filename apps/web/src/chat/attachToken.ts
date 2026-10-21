// This tab's keep-alive identity for the chat PTY (P18-26, R09.6), sent as `?attach=`.
//
// The server keeps one PTY per attach token (plus profile and resume target) and closes an older
// socket of the same key 4409 when a new one attaches. A token shared by two tabs would make them
// steal the terminal from each other, so the token must be per tab:
// - it lives in session storage (`indus.pty.token.chat`), which survives a reload of this tab but
//   not a new tab;
// - "Duplicate tab" copies session storage, so the token is also claimed with a Web Lock
//   (`indus.pty.attach.<token>`) held for the document's lifetime. A document that finds the lock
//   taken knows another live tab owns that token and mints its own. A reload frees the lock (the
//   old document is gone), so the reloaded tab gets its token, and its PTY, back.
// - "Start new session" rotates the token so the old keep-alive PTY is not reattached; the lock of
//   the old token is released.
// Without Web Locks the session storage alone decides; with storage blocked every document mints.

export const ATTACH_TOKEN_KEY = "indus.pty.token.chat";
export const ATTACH_LOCK_PREFIX = "indus.pty.attach.";

/** The slice of `LockManager` the keeper needs. */
export interface LockSource {
  request(
    name: string,
    options: { ifAvailable: true },
    callback: (lock: unknown) => Promise<unknown>,
  ): Promise<unknown>;
}

export interface AttachTokenDeps {
  storage(): Storage | null;
  locks(): LockSource | undefined;
  mint(): string;
}

function browserStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

function browserLocks(): LockSource | undefined {
  if (typeof navigator === "undefined") return undefined;
  const locks = (navigator as Navigator & { locks?: LockSource }).locks;
  return locks ?? undefined;
}

/** 128 random bits as hex. */
export function mintToken(): string {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  let out = "";
  for (const byte of bytes) out += byte.toString(16).padStart(2, "0");
  return out;
}

export const browserTokenDeps: AttachTokenDeps = {
  storage: browserStorage,
  locks: browserLocks,
  mint: mintToken,
};

interface Held {
  token: string;
  release: () => void;
}

export class AttachTokenKeeper {
  #held: Held | null = null;
  /** Serialises claims: two overlapping calls must not both mint. */
  #queue: Promise<unknown> = Promise.resolve();
  readonly #deps: AttachTokenDeps;

  constructor(deps: AttachTokenDeps = browserTokenDeps) {
    this.#deps = deps;
  }

  /** The token this document holds right now, if any. */
  get held(): string | null {
    return this.#held?.token ?? null;
  }

  /** This tab's token: the one held, else the stored one if no live tab claims it, else a new one. */
  current(): Promise<string> {
    return this.#serial(async () => {
      if (this.#held) return this.#held.token;
      const stored = this.#read();
      if (stored) {
        const release = await this.#claim(stored);
        if (release) {
          this.#held = { token: stored, release };
          return stored;
        }
      }
      return this.#adopt(this.#deps.mint());
    });
  }

  /** A new token for a fresh session; the old one's lock is let go. */
  rotate(): Promise<string> {
    return this.#serial(async () => {
      const previous = this.#held;
      this.#held = null;
      previous?.release();
      return this.#adopt(this.#deps.mint());
    });
  }

  #serial<T>(work: () => Promise<T>): Promise<T> {
    const next = this.#queue.then(work, work);
    this.#queue = next.catch(() => undefined);
    return next;
  }

  async #adopt(token: string): Promise<string> {
    this.#write(token);
    // A freshly minted token cannot be held elsewhere; no lock support still counts as held.
    const release = (await this.#claim(token)) ?? (() => {});
    this.#held = { token, release };
    return token;
  }

  /** Take the token's lock: a release function, or null when another document holds it. */
  async #claim(token: string): Promise<(() => void) | null> {
    const locks = this.#deps.locks();
    if (!locks) return () => {};
    return new Promise<(() => void) | null>((settle) => {
      let answered = false;
      const answer = (value: (() => void) | null) => {
        if (!answered) {
          answered = true;
          settle(value);
        }
      };
      locks
        .request(`${ATTACH_LOCK_PREFIX}${token}`, { ifAvailable: true }, (lock) => {
          if (lock === null) {
            answer(null);
            return Promise.resolve();
          }
          // Keep the lock until released: the callback's promise is the hold.
          return new Promise<void>((letGo) => answer(() => letGo()));
        })
        .catch(() => answer(() => {}));
    });
  }

  #read(): string {
    try {
      return this.#deps.storage()?.getItem(ATTACH_TOKEN_KEY) ?? "";
    } catch {
      return "";
    }
  }

  #write(token: string): void {
    try {
      this.#deps.storage()?.setItem(ATTACH_TOKEN_KEY, token);
    } catch {
      // storage blocked: the token lasts for this document only
    }
  }
}

/** The keeper every chat host in this document shares. */
export const attachTokens = new AttachTokenKeeper();
