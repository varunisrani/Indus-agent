// Profile switching in one window (P20-26, R08.6). Three shapes that must not be confused:
//
//   connection apply (local ↔ remote)  soft re-home: the shell stays mounted, every gateway-bound
//                                      store is wiped explicitly, main applies, sockets redial
//   runtime home change                hard re-home: the window reloads
//   live profile swap                  the other profile's socket starts (or is reused); the swap
//                                      commits once it is ready; background profiles keep their
//                                      sockets and keep streaming; only an explicit selection
//                                      starts a fresh draft
//
// After every switch the foreground profile, its connection and the socket's descriptor must agree
// (`assertAgreement`); a mismatch is an error, never a silent misroute. Rename and delete go
// through main first (it detaches from that profile), then the caller's REST call, then the
// profile-keyed storage families move or drop through the one registry (storageFamilies.ts).

import {
  browserStorage,
  dropProfileStorage,
  migrateProfileStorage,
  profileKey,
  type StorageLike,
} from "./storageFamilies.ts";

export const SWITCH_COMMIT_TIMEOUT_MS = 20_000;
export const REMEMBER_TIMEOUT_MS = 5_000;

export type ConnectionMode = "local" | "remote";

export interface ProfileState {
  readonly active: string | null;
  readonly defaultProfile: string | null;
  /** The profile main's current boot attempt runs. */
  readonly launch: string | null;
}

/** `window.indusDesktop.profile` (the parts the switcher uses). */
export interface ProfileBridge {
  get(): Promise<ProfileState>;
  set(payload: { profile: string | null }): Promise<ProfileState>;
  prepareChange(payload: { action: "rename" | "delete"; profile: string }): Promise<{ ok: true }>;
  commitChange(payload: {
    action: "rename" | "delete";
    profile: string;
    to?: string;
  }): Promise<ProfileState>;
}

export interface SwitchDescriptor {
  readonly profile: string | null;
  readonly mode: ConnectionMode;
}

/** What the switcher needs of a profile's connection (`GatewayConnection` has it). */
export interface SwitchConnection {
  readonly profile: string | null;
  start(): void;
  reconnect(): void;
  whenReady(timeoutMs?: number): Promise<void>;
  getState(): { readonly ready: boolean; readonly descriptor: SwitchDescriptor | null };
  close(): Promise<void>;
}

/** A profile's gateway-bound world in this window. */
export interface ProfileWorkspace {
  readonly profile: string;
  readonly connection: SwitchConnection;
  /** Drop every gateway-bound store (transcripts, open requests, cached lists). */
  wipe(): void;
  dispose(): Promise<void>;
}

export type SwitchStatus = "idle" | "switching" | "error";

export interface SwitcherState {
  readonly foreground: string;
  readonly status: SwitchStatus;
  readonly error: string | null;
  /** The profile being switched to (null when none). */
  readonly pending: string | null;
  /** Bumped by every explicit selection: the chat starts a fresh draft. */
  readonly freshDraft: number;
}

export type SwitchResult =
  | { readonly ok: true; readonly freshDraft: boolean }
  | {
      readonly ok: false;
      readonly reason: "superseded" | "timeout" | "mismatch" | "failed";
      readonly error: string;
    };

export class ProfileAgreementError extends Error {
  readonly problems: readonly string[];
  constructor(problems: readonly string[]) {
    super(`profile, connection and socket disagree: ${problems.join("; ")}`);
    this.name = "ProfileAgreementError";
    this.problems = problems;
  }
}

export interface SwitchTimers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface SwitcherOptions<W extends ProfileWorkspace> {
  initial: string;
  createWorkspace(profile: string): W;
  bridge?: ProfileBridge | null;
  storage?: StorageLike | null;
  timers?: SwitchTimers;
  /** A hard re-home reloads the window. */
  reload?: () => void;
  log?: (line: string) => void;
}

const systemTimers: SwitchTimers = {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

class Timeout extends Error {}

function within<T>(promise: Promise<T>, ms: number, timers: SwitchTimers): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const handle = timers.setTimeout(() => reject(new Timeout(`no answer in ${ms} ms`)), ms);
    promise.then(
      (value) => {
        timers.clearTimeout(handle);
        resolve(value);
      },
      (error: unknown) => {
        timers.clearTimeout(handle);
        reject(error);
      },
    );
  });
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export class ProfileSwitcher<W extends ProfileWorkspace> {
  private readonly options: SwitcherOptions<W>;
  private readonly timers: SwitchTimers;
  private readonly storage: StorageLike | null;
  private readonly spaces = new Map<string, W>();
  private readonly listeners = new Set<() => void>();
  private readonly wipers = new Set<(profile: string | null) => void>();
  private current: SwitcherState;
  private seq = 0;
  /** A soft re-home or a rename/delete in progress: swaps wait for it. */
  private exclusive: Promise<unknown> = Promise.resolve();
  /** The mode each profile's socket last reported (for the agreement check). */
  private readonly modes = new Map<string, ConnectionMode>();

  constructor(options: SwitcherOptions<W>) {
    this.options = options;
    this.timers = options.timers ?? systemTimers;
    this.storage = options.storage === undefined ? browserStorage() : options.storage;
    const initial = profileKey(options.initial);
    this.current = {
      foreground: initial,
      status: "idle",
      error: null,
      pending: null,
      freshDraft: 0,
    };
  }

  // -- reading -----------------------------------------------------------------------------------

  getState(): SwitcherState {
    return this.current;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /** The workspace of `profile` (created on first use, not started). */
  workspace(profile: string | null): W {
    const name = profileKey(profile);
    let space = this.spaces.get(name);
    if (space === undefined) {
      space = this.options.createWorkspace(name);
      this.spaces.set(name, space);
    }
    return space;
  }

  foreground(): W {
    return this.workspace(this.current.foreground);
  }

  workspaces(): W[] {
    return [...this.spaces.values()];
  }

  /** Another gateway-bound store to wipe on a soft re-home (`null`: every profile). */
  registerWipe(wipe: (profile: string | null) => void): () => void {
    this.wipers.add(wipe);
    return () => {
      this.wipers.delete(wipe);
    };
  }

  // -- the switch shapes -------------------------------------------------------------------------

  /** Start the foreground profile's socket (the window's first dial). */
  start(): void {
    this.foreground().connection.start();
  }

  /**
   * Live swap to `profile`. The swap commits when its socket is ready (20 s); the old profile's
   * socket stays open. `explicit` (a rail click) starts a fresh draft.
   */
  async select(profile: string, options: { explicit?: boolean } = {}): Promise<SwitchResult> {
    const target = profileKey(profile);
    await this.exclusive;
    if (target === this.current.foreground && this.current.status !== "switching") {
      return { ok: true, freshDraft: false };
    }
    this.seq += 1;
    const token = this.seq;
    this.patch({ status: "switching", pending: target, error: null });
    const space = this.workspace(target);
    space.connection.start();
    try {
      await space.connection.whenReady(SWITCH_COMMIT_TIMEOUT_MS);
    } catch (error) {
      if (token !== this.seq) return { ok: false, reason: "superseded", error: "a newer switch" };
      const text = `could not switch to ${target}: ${message(error)}`;
      this.patch({ status: "error", pending: null, error: text });
      this.log(text);
      return { ok: false, reason: "timeout", error: text };
    }
    if (token !== this.seq) return { ok: false, reason: "superseded", error: "a newer switch" };
    const explicit = options.explicit === true;
    this.patch({
      foreground: target,
      status: "idle",
      pending: null,
      error: null,
      freshDraft: this.current.freshDraft + (explicit ? 1 : 0),
    });
    const agreement = this.check();
    if (agreement !== null) return agreement;
    void this.remember(target);
    return { ok: true, freshDraft: explicit };
  }

  /**
   * Soft re-home (connection apply): wipe every gateway-bound store, let `apply` change the
   * connection in main, then redial every open socket and check the foreground agrees.
   */
  applyConnectionChange(apply: () => Promise<void>): Promise<SwitchResult> {
    return this.exclusively(async () => {
      this.seq += 1;
      this.patch({ status: "switching", pending: this.current.foreground, error: null });
      this.wipeAll();
      try {
        await apply();
      } catch (error) {
        const text = `the connection change failed: ${message(error)}`;
        this.patch({ status: "error", pending: null, error: text });
        return { ok: false as const, reason: "failed" as const, error: text };
      }
      this.modes.clear();
      for (const space of this.spaces.values()) space.connection.reconnect();
      try {
        await this.foreground().connection.whenReady(SWITCH_COMMIT_TIMEOUT_MS);
      } catch (error) {
        const text = `not connected after the change: ${message(error)}`;
        this.patch({ status: "error", pending: null, error: text });
        return { ok: false as const, reason: "timeout" as const, error: text };
      }
      this.patch({ status: "idle", pending: null });
      return this.check() ?? { ok: true as const, freshDraft: false };
    });
  }

  /** Hard re-home (runtime home change): let `apply` run, then reload the window. */
  async hardRehome(apply?: () => Promise<void>): Promise<void> {
    await this.exclusively(async () => {
      if (apply !== undefined) await apply();
      (this.options.reload ?? (() => window.location.reload()))();
    });
  }

  /**
   * Main saw the backend change mode (local ↔ remote) without going through
   * `applyConnectionChange`: wipe as a soft re-home would (safety net).
   */
  noteDescriptor(descriptor: SwitchDescriptor | null): void {
    if (descriptor === null) return;
    const name = profileKey(descriptor.profile);
    const before = this.modes.get(name);
    this.modes.set(name, descriptor.mode);
    if (before !== undefined && before !== descriptor.mode) {
      this.log(`[profiles] ${name} moved from ${before} to ${descriptor.mode}; wiping its stores`);
      this.spaces.get(name)?.wipe();
      for (const wipe of [...this.wipers]) wipe(name);
    }
  }

  // -- rename and delete --------------------------------------------------------------------------

  /** Rename: main detaches, `run` renames on the backend, then storage moves to the new name. */
  renameProfile(from: string, to: string, run: () => Promise<void>): Promise<SwitchResult> {
    return this.changeProfile("rename", profileKey(from), profileKey(to), run);
  }

  /** Delete: main detaches, `run` deletes on the backend, then the profile's storage goes. */
  deleteProfile(name: string, run: () => Promise<void>): Promise<SwitchResult> {
    return this.changeProfile("delete", profileKey(name), null, run);
  }

  private async changeProfile(
    action: "rename" | "delete",
    profile: string,
    to: string | null,
    run: () => Promise<void>,
  ): Promise<SwitchResult> {
    // The foreground leaves the profile first (a rename comes back to the new name).
    const wasForeground = this.current.foreground === profile;
    if (wasForeground) {
      const away = await this.select("default");
      if (!away.ok && profile !== "default") return away;
    }
    return this.exclusively(async () => {
      const bridge = this.options.bridge;
      try {
        await bridge?.prepareChange({ action, profile });
        const space = this.spaces.get(profile);
        this.spaces.delete(profile);
        if (space !== undefined) {
          space.wipe();
          await space.dispose();
        }
        for (const wipe of [...this.wipers]) wipe(profile);
        await run();
        await bridge?.commitChange({ action, profile, ...(to === null ? {} : { to }) });
      } catch (error) {
        const text = `could not ${action} ${profile}: ${message(error)}`;
        this.patch({ status: "error", error: text });
        return { ok: false as const, reason: "failed" as const, error: text };
      }
      if (this.storage !== null) {
        if (action === "rename" && to !== null) migrateProfileStorage(this.storage, profile, to);
        else dropProfileStorage(this.storage, profile);
      }
      return { ok: true as const, freshDraft: false };
    }).then(async (result) => {
      if (result.ok && wasForeground && action === "rename" && to !== null) {
        return this.select(to);
      }
      return result;
    });
  }

  // -- agreement ---------------------------------------------------------------------------------

  /** Throws when the foreground profile, its connection and its socket disagree. */
  assertAgreement(): void {
    const problems = this.problems();
    if (problems.length > 0) throw new ProfileAgreementError(problems);
  }

  private problems(): string[] {
    const name = this.current.foreground;
    const space = this.spaces.get(name);
    if (space === undefined) return [`no workspace for ${name}`];
    const problems: string[] = [];
    if (space.profile !== name) problems.push(`workspace is ${space.profile}, not ${name}`);
    if (profileKey(space.connection.profile) !== name) {
      problems.push(`connection is for ${profileKey(space.connection.profile)}`);
    }
    const state = space.connection.getState();
    if (state.ready && state.descriptor === null) problems.push("socket has no descriptor");
    if (state.descriptor !== null) {
      const socketProfile = profileKey(state.descriptor.profile);
      if (socketProfile !== name) problems.push(`socket serves ${socketProfile}`);
      const mode = this.modes.get(name);
      if (mode !== undefined && mode !== state.descriptor.mode) {
        problems.push(`socket is ${state.descriptor.mode}, main reported ${mode}`);
      }
    }
    return problems;
  }

  private check(): SwitchResult | null {
    const problems = this.problems();
    if (problems.length === 0) return null;
    const error = new ProfileAgreementError(problems).message;
    this.patch({ status: "error", error });
    this.log(`[profiles] ${error}`);
    return { ok: false, reason: "mismatch", error };
  }

  // -- internals ---------------------------------------------------------------------------------

  private wipeAll(): void {
    for (const space of this.spaces.values()) space.wipe();
    for (const wipe of [...this.wipers]) wipe(null);
  }

  private exclusively<T>(fn: () => Promise<T>): Promise<T> {
    const run = this.exclusive.then(fn, fn);
    this.exclusive = run.catch(() => undefined);
    return run;
  }

  private async remember(profile: string): Promise<void> {
    const bridge = this.options.bridge;
    if (bridge === null || bridge === undefined) return;
    try {
      await within(bridge.set({ profile }), REMEMBER_TIMEOUT_MS, this.timers);
    } catch (error) {
      // the swap stands; the next launch simply opens the previous profile
      this.log(`[profiles] ${profile} not remembered: ${message(error)}`);
    }
  }

  private patch(changes: Partial<SwitcherState>): void {
    this.current = { ...this.current, ...changes };
    for (const listener of [...this.listeners]) listener();
  }

  private log(line: string): void {
    this.options.log?.(line);
  }
}
