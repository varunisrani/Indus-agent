import { describe, expect, it } from "vitest";
import {
  ATTACH_LOCK_PREFIX,
  ATTACH_TOKEN_KEY,
  type AttachTokenDeps,
  AttachTokenKeeper,
  type LockSource,
  mintToken,
} from "./attachToken.ts";

/** A Web Locks stand-in shared by several "documents"; `ifAvailable` only. */
class FakeLocks implements LockSource {
  readonly held = new Set<string>();

  request(
    name: string,
    _options: { ifAvailable: true },
    callback: (lock: unknown) => Promise<unknown>,
  ): Promise<unknown> {
    if (this.held.has(name)) return callback(null);
    this.held.add(name);
    const hold = callback({ name });
    return hold.finally(() => this.held.delete(name));
  }
}

function memoryStorage(): Storage {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    getItem: (key) => map.get(key) ?? null,
    key: (index) => [...map.keys()][index] ?? null,
    removeItem: (key) => {
      map.delete(key);
    },
    setItem: (key, value) => {
      map.set(key, value);
    },
  };
}

function counter(): () => string {
  let n = 0;
  return () => {
    n += 1;
    return `minted-${n}`;
  };
}

function deps(storage: Storage | null, locks: LockSource | undefined, mint = counter()) {
  return { storage: () => storage, locks: () => locks, mint } satisfies AttachTokenDeps;
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("AttachTokenKeeper", () => {
  it("mints, stores the token in session storage and holds its lock", async () => {
    const storage = memoryStorage();
    const locks = new FakeLocks();
    const keeper = new AttachTokenKeeper(deps(storage, locks));
    const token = await keeper.current();
    expect(token).toBe("minted-1");
    expect(storage.getItem(ATTACH_TOKEN_KEY)).toBe(token);
    expect(locks.held.has(`${ATTACH_LOCK_PREFIX}${token}`)).toBe(true);
    expect(await keeper.current()).toBe(token);
  });

  it("a reloaded tab gets its stored token back", async () => {
    const storage = memoryStorage();
    storage.setItem(ATTACH_TOKEN_KEY, "kept-from-before");
    const keeper = new AttachTokenKeeper(deps(storage, new FakeLocks()));
    expect(await keeper.current()).toBe("kept-from-before");
  });

  it("a duplicated tab (same storage copy, lock held by the first) mints its own", async () => {
    const locks = new FakeLocks();
    const first = new AttachTokenKeeper(deps(memoryStorage(), locks, () => "first-token"));
    const shared = await first.current();
    // "Duplicate tab" copies session storage into the new document.
    const copy = memoryStorage();
    copy.setItem(ATTACH_TOKEN_KEY, shared);
    const second = new AttachTokenKeeper(deps(copy, locks, () => "second-token"));
    expect(await second.current()).toBe("second-token");
    expect(copy.getItem(ATTACH_TOKEN_KEY)).toBe("second-token");
    expect(await first.current()).toBe("first-token");
  });

  it("rotate mints a new token and lets the old lock go", async () => {
    const storage = memoryStorage();
    const locks = new FakeLocks();
    const keeper = new AttachTokenKeeper(deps(storage, locks));
    const old = await keeper.current();
    const fresh = await keeper.rotate();
    await flush();
    expect(fresh).not.toBe(old);
    expect(storage.getItem(ATTACH_TOKEN_KEY)).toBe(fresh);
    expect(locks.held.has(`${ATTACH_LOCK_PREFIX}${old}`)).toBe(false);
    expect(locks.held.has(`${ATTACH_LOCK_PREFIX}${fresh}`)).toBe(true);
    expect(await keeper.current()).toBe(fresh);
  });

  it("overlapping calls agree on one token", async () => {
    const keeper = new AttachTokenKeeper(deps(memoryStorage(), new FakeLocks()));
    const [a, b] = await Promise.all([keeper.current(), keeper.current()]);
    expect(a).toBe(b);
  });

  it("without Web Locks, session storage alone decides", async () => {
    const storage = memoryStorage();
    storage.setItem(ATTACH_TOKEN_KEY, "stored");
    const keeper = new AttachTokenKeeper(deps(storage, undefined));
    expect(await keeper.current()).toBe("stored");
  });

  it("blocked storage still yields a token for this document", async () => {
    const throwing = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    } as unknown as Storage;
    const keeper = new AttachTokenKeeper(deps(throwing, new FakeLocks()));
    expect(await keeper.current()).toBe("minted-1");
    expect(await keeper.current()).toBe("minted-1");
    const none = new AttachTokenKeeper(deps(null, undefined));
    expect(await none.current()).toBe("minted-1");
  });

  it("mintToken makes 128-bit hex values", () => {
    const a = mintToken();
    expect(a).toMatch(/^[0-9a-f]{32}$/);
    expect(mintToken()).not.toBe(a);
  });
});
