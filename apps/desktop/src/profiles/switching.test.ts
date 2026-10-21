import type { ClientEvent } from "@indus/client";
import { describe, expect, it, vi } from "vitest";
import { FakeTimers } from "../../testing/fakeTimers.ts";
import { type TranscriptSource, TranscriptStore } from "../chat/transcriptStore.ts";
import { harness, settle } from "../gateway/testSupport.ts";
import { PROFILE_FAMILIES, profileKeysIn, type StorageLike } from "./storageFamilies.ts";
import {
  ProfileAgreementError,
  type ProfileBridge,
  type ProfileState,
  ProfileSwitcher,
  type ProfileWorkspace,
  REMEMBER_TIMEOUT_MS,
  SWITCH_COMMIT_TIMEOUT_MS,
  type SwitchConnection,
  type SwitchDescriptor,
} from "./switching.ts";
import { ChatWorkspace } from "./workspace.ts";

class FakeConnection implements SwitchConnection {
  started = 0;
  reconnects = 0;
  closed = false;
  ready = false;
  descriptor: SwitchDescriptor | null = null;
  private waiters: Array<{ resolve: () => void; reject: (error: Error) => void }> = [];
  readonly profile: string | null;
  private readonly timers: FakeTimers;

  constructor(profile: string | null, timers: FakeTimers) {
    this.profile = profile;
    this.timers = timers;
  }

  start(): void {
    this.started += 1;
  }

  reconnect(): void {
    this.reconnects += 1;
    this.ready = false;
  }

  whenReady(timeoutMs = 60_000): Promise<void> {
    if (this.ready) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const waiter = { resolve, reject };
      this.waiters.push(waiter);
      this.timers.setTimeout(() => {
        this.waiters = this.waiters.filter((w) => w !== waiter);
        reject(new Error("not connected in time"));
      }, timeoutMs);
    });
  }

  becomeReady(descriptor?: SwitchDescriptor): void {
    this.ready = true;
    this.descriptor = descriptor ?? { profile: this.profile, mode: "local" };
    const waiters = this.waiters;
    this.waiters = [];
    for (const waiter of waiters) waiter.resolve();
  }

  getState() {
    return { ready: this.ready, descriptor: this.descriptor };
  }

  async close(): Promise<void> {
    this.closed = true;
  }
}

function transcriptSource(): TranscriptSource {
  return {
    onEvent: (_listener: (event: ClientEvent) => void) => () => undefined,
    request: (async () => ({ messages: [] })) as unknown as TranscriptSource["request"],
    watchSession: async () => undefined,
    unwatchSession: () => undefined,
  };
}

class FakeWorkspace implements ProfileWorkspace {
  transcripts = new TranscriptStore(transcriptSource());
  disposed = false;
  wipes = 0;
  readonly profile: string;
  readonly connection: FakeConnection;
  constructor(profile: string, connection: FakeConnection) {
    this.profile = profile;
    this.connection = connection;
  }
  wipe(): void {
    this.wipes += 1;
    this.transcripts.dispose();
    this.transcripts = new TranscriptStore(transcriptSource());
  }
  async dispose(): Promise<void> {
    this.disposed = true;
  }
}

class MemoryStorage implements StorageLike {
  readonly data = new Map<string, string>();
  get length(): number {
    return this.data.size;
  }
  key(index: number): string | null {
    return [...this.data.keys()][index] ?? null;
  }
  getItem(key: string): string | null {
    return this.data.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.data.set(key, value);
  }
  removeItem(key: string): void {
    this.data.delete(key);
  }
}

function fakeBridge(overrides: Partial<ProfileBridge> = {}) {
  let state: ProfileState = { active: null, defaultProfile: null, launch: "default" };
  const calls: string[] = [];
  const bridge: ProfileBridge = {
    get: async () => state,
    set: vi.fn(async ({ profile }) => {
      calls.push(`set ${profile}`);
      state = { ...state, active: profile };
      return state;
    }),
    prepareChange: vi.fn(async ({ action, profile }) => {
      calls.push(`prepare ${action} ${profile}`);
      return { ok: true as const };
    }),
    commitChange: vi.fn(async ({ action, profile, to }) => {
      calls.push(`commit ${action} ${profile}${to === undefined ? "" : ` ${to}`}`);
      return state;
    }),
    ...overrides,
  };
  return { bridge, calls };
}

function setup(options: { bridge?: ProfileBridge; storage?: StorageLike } = {}) {
  const timers = new FakeTimers();
  const spaces = new Map<string, FakeWorkspace>();
  const lines: string[] = [];
  const reload = vi.fn();
  const switcher = new ProfileSwitcher<FakeWorkspace>({
    initial: "default",
    createWorkspace: (profile) => {
      const space = new FakeWorkspace(profile, new FakeConnection(profile, timers));
      spaces.set(profile, space);
      return space;
    },
    bridge: options.bridge ?? fakeBridge().bridge,
    storage: options.storage ?? new MemoryStorage(),
    timers,
    reload,
    log: (line) => lines.push(line),
  });
  const space = (profile: string) => switcher.workspace(profile);
  return { switcher, timers, spaces, space, lines, reload };
}

async function flush(): Promise<void> {
  for (let i = 0; i < 6; i += 1) await Promise.resolve();
}

describe("live profile swap", () => {
  it("commits once the other socket is ready and keeps the old one streaming", async () => {
    const { switcher, space } = setup();
    switcher.start();
    space("default").connection.becomeReady();
    const swap = switcher.select("work", { explicit: true });
    await flush();
    expect(switcher.getState()).toMatchObject({
      status: "switching",
      pending: "work",
      foreground: "default",
    });
    expect(space("work").connection.started).toBe(1);
    space("work").connection.becomeReady();
    expect(await swap).toEqual({ ok: true, freshDraft: true });
    expect(switcher.getState()).toMatchObject({
      foreground: "work",
      status: "idle",
      freshDraft: 1,
    });
    // the background profile keeps its socket and its transcripts
    expect(space("default").connection.closed).toBe(false);
    expect(space("default").wipes).toBe(0);
    expect(switcher.workspaces().map((w) => w.profile)).toEqual(["default", "work"]);
  });

  it("only an explicit selection starts a fresh draft", async () => {
    const { switcher, space } = setup();
    space("work").connection.becomeReady();
    expect(await switcher.select("work")).toEqual({ ok: true, freshDraft: false });
    expect(switcher.getState().freshDraft).toBe(0);
    space("default").connection.becomeReady();
    expect(await switcher.select("default", { explicit: true })).toEqual({
      ok: true,
      freshDraft: true,
    });
  });

  it("a socket that is not ready in 20 s leaves the foreground where it was", async () => {
    const { switcher, timers } = setup();
    const swap = switcher.select("slow", { explicit: true });
    await flush();
    timers.advance(SWITCH_COMMIT_TIMEOUT_MS - 1);
    await flush();
    expect(switcher.getState().status).toBe("switching");
    timers.advance(1);
    const result = await swap;
    expect(result).toMatchObject({ ok: false, reason: "timeout" });
    expect(switcher.getState()).toMatchObject({
      foreground: "default",
      status: "error",
      pending: null,
    });
  });

  it("a later swap supersedes an earlier one still waiting", async () => {
    const { switcher, space } = setup();
    const first = switcher.select("a");
    await flush();
    space("b").connection.becomeReady();
    const second = switcher.select("b");
    expect(await second).toMatchObject({ ok: true });
    space("a").connection.becomeReady();
    expect(await first).toMatchObject({ ok: false, reason: "superseded" });
    expect(switcher.getState().foreground).toBe("b");
  });

  it("remembers the profile in main, and a slow remember never undoes the swap", async () => {
    const never = new Promise<ProfileState>(() => undefined);
    const { bridge } = fakeBridge({ set: vi.fn(() => never) });
    const { switcher, space, timers, lines } = setup({ bridge });
    space("work").connection.becomeReady();
    await switcher.select("work");
    expect(bridge.set).toHaveBeenCalledWith({ profile: "work" });
    timers.advance(REMEMBER_TIMEOUT_MS);
    await flush();
    expect(switcher.getState().foreground).toBe("work");
    expect(lines.some((line) => line.includes("not remembered"))).toBe(true);
  });
});

describe("agreement", () => {
  it("trips on a forced mismatch between the profile and its socket", async () => {
    const { switcher, space } = setup();
    space("work").connection.becomeReady({ profile: "home", mode: "local" });
    const result = await switcher.select("work");
    expect(result).toMatchObject({ ok: false, reason: "mismatch" });
    expect(switcher.getState().status).toBe("error");
    expect(() => switcher.assertAgreement()).toThrow(ProfileAgreementError);
    expect(() => switcher.assertAgreement()).toThrow(/socket serves home/);
  });

  it("trips when main reported another mode than the socket has", () => {
    const { switcher, space } = setup();
    space("default").connection.becomeReady({ profile: "default", mode: "local" });
    switcher.noteDescriptor({ profile: "default", mode: "local" });
    expect(() => switcher.assertAgreement()).not.toThrow();
    space("default").connection.descriptor = { profile: null, mode: "remote" };
    expect(() => switcher.assertAgreement()).toThrow(/socket is remote, main reported local/);
  });

  it("a null profile on the socket means the default profile", () => {
    const { switcher, space } = setup();
    space("default").connection.becomeReady({ profile: null, mode: "local" });
    expect(() => switcher.assertAgreement()).not.toThrow();
  });
});

describe("soft re-home (connection apply)", () => {
  it("leaves no session store entry from the old connection", async () => {
    const { switcher, space } = setup();
    space("default").connection.becomeReady();
    space("work").connection.becomeReady();
    const before = space("default").transcripts;
    before.addLocalUser("s-old", "hello");
    space("work").transcripts.addLocalUser("s-work", "hi");
    const cached = new Map([["s-old", "row"]]);
    switcher.registerWipe(() => cached.clear());
    const apply = vi.fn(async () => undefined); // main switches local → remote
    const applying = switcher.applyConnectionChange(apply);
    await flush();
    // the sockets come back on the new route
    space("default").connection.becomeReady({ profile: "default", mode: "remote" });
    const result = await applying;
    expect(result).toEqual({ ok: true, freshDraft: false });
    expect(apply).toHaveBeenCalledOnce();
    expect(space("default").transcripts).not.toBe(before);
    expect(space("default").transcripts.get("s-old").items).toEqual([]);
    expect(space("work").transcripts.get("s-work").items).toEqual([]);
    expect(cached.size).toBe(0);
    expect(space("default").connection.reconnects).toBe(1);
    expect(space("work").connection.reconnects).toBe(1);
    // the shell stays mounted: no reload
    expect(switcher.getState()).toMatchObject({ foreground: "default", status: "idle" });
  });

  it("a mode change seen from main wipes that profile's stores (safety net)", () => {
    const { switcher, space } = setup();
    switcher.noteDescriptor({ profile: "default", mode: "local" });
    space("default").transcripts.addLocalUser("s1", "x");
    switcher.noteDescriptor({ profile: null, mode: "remote" });
    expect(space("default").wipes).toBe(1);
    expect(space("default").transcripts.get("s1").items).toEqual([]);
  });

  it("a swap waits for a soft re-home in progress", async () => {
    const { switcher, space } = setup();
    let release: () => void = () => undefined;
    const applying = switcher.applyConnectionChange(
      () =>
        new Promise<void>((resolve) => {
          release = resolve;
        }),
    );
    space("work").connection.becomeReady();
    const swap = switcher.select("work");
    await flush();
    expect(space("work").connection.started).toBe(0);
    release();
    await flush();
    space("default").connection.becomeReady();
    await applying;
    await flush();
    // the re-home redialled every socket, the swap's included; it commits once that one is back
    expect(space("work").connection.started).toBe(1);
    space("work").connection.becomeReady();
    expect(await swap).toMatchObject({ ok: true });
  });
});

describe("hard re-home", () => {
  it("applies and reloads the window", async () => {
    const { switcher, reload } = setup();
    const apply = vi.fn(async () => undefined);
    await switcher.hardRehome(apply);
    expect(apply).toHaveBeenCalledOnce();
    expect(reload).toHaveBeenCalledOnce();
  });
});

describe("rename and delete", () => {
  function seeded(storage: MemoryStorage, profile: string): void {
    for (const family of PROFILE_FAMILIES) {
      if (family.kind === "perKey") storage.setItem(`${family.prefix}${profile}`, "1");
      else storage.setItem(family.key, JSON.stringify({ [profile]: 1 }));
    }
  }

  it("rename: main detaches first, then the backend, then every family moves", async () => {
    const storage = new MemoryStorage();
    seeded(storage, "work");
    const { bridge, calls } = fakeBridge();
    const { switcher, space } = setup({ bridge, storage });
    space("default").connection.becomeReady();
    const old = space("work");
    const run = vi.fn(async () => {
      calls.push("rest rename");
    });
    expect(await switcher.renameProfile("work", "client", run)).toMatchObject({ ok: true });
    expect(calls).toEqual(["prepare rename work", "rest rename", "commit rename work client"]);
    expect(old.disposed).toBe(true);
    expect(profileKeysIn(storage).map((e) => e.profile)).toEqual(
      PROFILE_FAMILIES.map(() => "client"),
    );
  });

  it("renaming the foreground profile swaps away first and comes back under the new name", async () => {
    const storage = new MemoryStorage();
    const { switcher, space } = setup({ storage });
    space("work").connection.becomeReady();
    await switcher.select("work");
    space("default").connection.becomeReady();
    const renamed = switcher.renameProfile("work", "client", async () => undefined);
    await flush();
    await flush();
    space("client").connection.becomeReady();
    expect(await renamed).toMatchObject({ ok: true });
    expect(switcher.getState().foreground).toBe("client");
  });

  it("delete of the foreground profile goes to default and drops its storage", async () => {
    const storage = new MemoryStorage();
    seeded(storage, "work");
    seeded(storage, "keep");
    const { switcher, space } = setup({ storage });
    space("work").connection.becomeReady();
    await switcher.select("work");
    space("default").connection.becomeReady();
    expect(await switcher.deleteProfile("work", async () => undefined)).toMatchObject({ ok: true });
    expect(switcher.getState().foreground).toBe("default");
    const owners = profileKeysIn(storage).map((e) => e.profile);
    expect(owners).not.toContain("work");
    expect(owners.filter((p) => p === "keep")).toHaveLength(PROFILE_FAMILIES.length);
  });

  it("a failing backend call keeps the storage and reports the error", async () => {
    const storage = new MemoryStorage();
    seeded(storage, "work");
    const { switcher } = setup({ storage });
    const result = await switcher.deleteProfile("work", async () => {
      throw new Error("profile_in_use");
    });
    expect(result).toMatchObject({ ok: false, reason: "failed" });
    expect(profileKeysIn(storage).map((e) => e.profile)).toContain("work");
  });
});

describe("chat workspace", () => {
  it("a wipe rebuilds the transcripts and clears open requests on the same connection", async () => {
    const h = harness();
    const slots = vi.fn(() => ({}));
    const workspace = new ChatWorkspace("default", h.store, slots);
    const clear = vi.spyOn(workspace.connection.serverRequests, "clear");
    const before = workspace.transcripts;
    before.addLocalUser("s1", "hello");
    let notified = 0;
    workspace.subscribe(() => {
      notified += 1;
    });
    workspace.wipe();
    expect(workspace.transcripts).not.toBe(before);
    expect(workspace.transcripts.get("s1").items).toEqual([]);
    expect(clear).toHaveBeenCalledOnce();
    expect(slots).toHaveBeenCalledTimes(2);
    expect(notified).toBe(1);
    expect(workspace.getVersion()).toBe(1);
    await workspace.dispose();
    await settle();
    expect(h.store.connection("default")).not.toBe(workspace.connection);
  });
});
