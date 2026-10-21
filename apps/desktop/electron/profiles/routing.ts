// Profile routing in main (P20-26): the remembered active and default profiles, and the one
// launch decision per boot attempt.
//
// `<userData>/profile-state.json` keeps `{ version: 1, active, defaultProfile }`. The launch
// decision: an explicit `--profile NAME` / `-p NAME` / `--profile=NAME` on the app's command line,
// else the remembered active profile, else the default profile, else `default`. It is taken once
// per boot attempt and frozen: a "remember" (set active) that lands while an attempt runs is saved
// but applies to the next attempt. The same value feeds the route the window shows (`launch` in
// the state answer), the `-p` flag of every `indus` command (localAttach always passes it, also
// `-p default`, so the CLI's own sticky profile cannot pick another daemon) and the child
// environment (`INDUS_HOME` = the root; the flag selects the profile).
//
// Rename and delete: `prepare-change` tears down what is bound to that profile's connection
// (when main is attached to it, main re-homes to the default profile first, so the serve can
// rename or delete it), and `commit-change` renames or clears the remembered names afterwards.

import fs from "node:fs";
import path from "node:path";
import type { MainContext } from "../context.ts";
import { writeFileAtomic } from "../fsSecure.ts";
import { IpcError } from "../ipc/envelope.ts";
import type { EventTarget } from "../ipc/registry.ts";

export const PROFILE_STATE_FILE = "profile-state.json";
export const DEFAULT_PROFILE = "default";
/** The backend's rule for a profile id (`indus.foundation.profiles.ID_PATTERN`). */
export const PROFILE_ID = /^[a-z0-9][a-z0-9_-]{0,63}$/;

export interface StoredProfiles {
  readonly active: string | null;
  readonly defaultProfile: string | null;
}

export type LaunchSource = "flag" | "remembered" | "default-profile" | "fallback";

export interface LaunchDecision {
  readonly profile: string;
  readonly source: LaunchSource;
  /** Which boot attempt took it (1 for the first). */
  readonly attempt: number;
}

export interface ProfileStateAnswer {
  readonly active: string | null;
  readonly defaultProfile: string | null;
  readonly launch: string | null;
}

/** A profile id as the backend spells it, or null when it is not one. */
export function normalizeProfile(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const text = value.trim().toLowerCase();
  if (text === DEFAULT_PROFILE) return DEFAULT_PROFILE;
  return PROFILE_ID.test(text) ? text : null;
}

/** `--profile NAME`, `--profile=NAME` or `-p NAME` in the app's argv (stops at `--`). */
export function profileFromArgv(argv: readonly string[]): string | null {
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i] as string;
    if (arg === "--") break;
    if (arg.startsWith("--profile=")) return normalizeProfile(arg.slice("--profile=".length));
    if ((arg === "--profile" || arg === "-p") && i + 1 < argv.length) {
      return normalizeProfile(argv[i + 1]);
    }
  }
  return null;
}

export function decideLaunch(
  argv: readonly string[],
  stored: StoredProfiles,
): { profile: string; source: LaunchSource } {
  const flag = profileFromArgv(argv);
  if (flag !== null) return { profile: flag, source: "flag" };
  if (stored.active !== null) return { profile: stored.active, source: "remembered" };
  if (stored.defaultProfile !== null) {
    return { profile: stored.defaultProfile, source: "default-profile" };
  }
  return { profile: DEFAULT_PROFILE, source: "fallback" };
}

export function readStoredProfiles(file: string): StoredProfiles {
  try {
    const parsed = JSON.parse(fs.readFileSync(file, "utf8")) as Record<string, unknown>;
    if (parsed.version !== 1) return { active: null, defaultProfile: null };
    return {
      active: normalizeProfile(parsed.active),
      defaultProfile: normalizeProfile(parsed.defaultProfile),
    };
  } catch {
    return { active: null, defaultProfile: null };
  }
}

export interface ProfileRoutingOptions {
  userData: string;
  argv: readonly string[];
  log(line: string): void;
}

export class ProfileRouting {
  private readonly file: string;
  private readonly argv: readonly string[];
  private readonly log: (line: string) => void;
  private stored: StoredProfiles;
  private decision: LaunchDecision | null = null;
  private attempts = 0;
  private readonly listeners = new Set<(state: ProfileStateAnswer) => void>();

  constructor(options: ProfileRoutingOptions) {
    this.file = path.join(options.userData, PROFILE_STATE_FILE);
    this.argv = options.argv;
    this.log = options.log;
    this.stored = readStoredProfiles(this.file);
  }

  /** The current attempt's decision (taken on first use). */
  launch(): LaunchDecision {
    return this.decision ?? this.beginAttempt();
  }

  /** A new boot attempt: decide again from what is remembered now. */
  beginAttempt(): LaunchDecision {
    this.attempts += 1;
    const { profile, source } = decideLaunch(this.argv, this.stored);
    this.decision = { profile, source, attempt: this.attempts };
    this.log(`[profiles] attempt ${this.attempts} runs profile ${profile} (${source})`);
    return this.decision;
  }

  state(): ProfileStateAnswer {
    return {
      active: this.stored.active,
      defaultProfile: this.stored.defaultProfile,
      launch: this.launch().profile,
    };
  }

  onChanged(listener: (state: ProfileStateAnswer) => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /** Remember the active profile (applies to the next attempt). */
  setActive(profile: string | null): ProfileStateAnswer {
    return this.save({ ...this.stored, active: this.checked(profile) });
  }

  setDefault(profile: string | null): ProfileStateAnswer {
    return this.save({ ...this.stored, defaultProfile: this.checked(profile) });
  }

  /** After the backend renamed or deleted a profile: follow it in what is remembered. */
  commitChange(action: "rename" | "delete", profile: string, to?: string): ProfileStateAnswer {
    const from = this.checked(profile);
    const next = action === "rename" ? this.checked(to ?? null) : null;
    if (action === "rename" && next === null) {
      throw new IpcError("bad-request", "a rename needs the new name");
    }
    const swap = (name: string | null) => (name === from ? next : name);
    if (this.decision !== null && this.decision.profile === from) {
      // the running attempt follows the rename; a deleted profile falls back next attempt
      this.decision = next === null ? null : { ...this.decision, profile: next };
    }
    return this.save({
      active: swap(this.stored.active),
      defaultProfile: swap(this.stored.defaultProfile),
    });
  }

  private checked(profile: string | null): string | null {
    if (profile === null) return null;
    const name = normalizeProfile(profile);
    if (name === null) throw new IpcError("bad-request", "that is not a profile name");
    return name;
  }

  private save(next: StoredProfiles): ProfileStateAnswer {
    this.stored = next;
    try {
      fs.mkdirSync(path.dirname(this.file), { recursive: true });
      writeFileAtomic(
        this.file,
        `${JSON.stringify({ version: 1, active: next.active, defaultProfile: next.defaultProfile })}\n`,
      );
    } catch (error) {
      this.log(
        `[profiles] could not save ${PROFILE_STATE_FILE}: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
    const state = this.state();
    for (const listener of [...this.listeners]) listener(state);
    return state;
  }
}

/** What main's backend side offers the profile routing (`installBackend`'s orchestrator). */
export interface BackendPort {
  /** The profile main is attached to, if any. */
  attachedProfile(): string | null;
  /** Attach to `profile` instead (resolves when it is up, or rejects). */
  rehome(profile: string): Promise<void>;
}

/**
 * Register `indus:profile:*` and return the routing (main.ts passes `routing.launch().profile`
 * to `installBackend` for requests that name no profile, and the backend port back here).
 */
export function installProfiles(
  context: MainContext,
  options: { argv: readonly string[]; backend?: () => BackendPort | null },
): ProfileRouting {
  const { registry, windows, log } = context;
  const routing = new ProfileRouting({
    userData: context.paths.userData,
    argv: options.argv,
    log: (line) => log.write(line),
  });
  routing.launch(); // the launch's boot attempt decides now, before any window can remember
  routing.onChanged((state) => {
    for (const win of windows.windows()) {
      registry.emit(win.webContents as EventTarget, "indus:profile:changed", state);
    }
  });

  registry.handle("indus:profile:get", () => routing.state());
  registry.handle("indus:profile:set", ({ profile }) => routing.setActive(profile));
  registry.handle("indus:profile:set-default", ({ profile }) => routing.setDefault(profile));
  registry.handle("indus:profile:prepare-change", async ({ profile }) => {
    const name = normalizeProfile(profile);
    if (name === null) throw new IpcError("bad-request", "that is not a profile name");
    if (name === DEFAULT_PROFILE) {
      throw new IpcError("bad-request", "the default profile cannot be renamed or deleted");
    }
    const backend = options.backend?.() ?? null;
    if (backend !== null && backend.attachedProfile() === name) {
      log.write(`[profiles] leaving ${name} before it changes`);
      try {
        await backend.rehome(DEFAULT_PROFILE);
      } catch (error) {
        throw new IpcError(
          "profile-busy",
          `could not leave ${name}: ${error instanceof Error ? error.message : String(error)}`,
        );
      }
    }
    return { ok: true as const };
  });
  registry.handle("indus:profile:commit-change", ({ action, profile, to }) =>
    routing.commitChange(action, profile, to),
  );
  return routing;
}
