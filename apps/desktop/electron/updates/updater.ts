// The desktop's own updates (P20-37, unsigned 1.0 scope).
//
// One owner, declared by the build stamp and never guessed: `electron-updater` (macOS bundles, the
// Linux AppImage) or `manual` (the unsigned Windows build, source checkouts, no stamp). A check
// never downloads. Apply (electron-updater only): refused while another update owns the runtime
// or one runs here; otherwise recheck → download → wait for the updater's acceptance → stop
// app-owned work → quit and install. Anything failing before the stop leaves everything running.

import fs from "node:fs";
import path from "node:path";
import type { Timers } from "../ambient/hud.ts";
import { readJsonFile, writeFileAtomic } from "../fsSecure.ts";
import type { UpdateEngine } from "./engine.ts";
import type { ReleaseInfo, UpdateChannel } from "./feed.ts";
import type { BuildStamp, UpdateOwner } from "./skew.ts";
import { isNewer } from "./version.ts";

export const FIRST_CHECK_DELAY_MS = 30_000;
export const CHECK_INTERVAL_MS = 6 * 60 * 60_000;
export const MANUAL_COMMAND = "indus update";

export type UpdateState =
  | "idle"
  | "checking"
  | "available"
  | "downloading"
  | "ready"
  | "installing"
  | "error"
  | "manual";

export interface UpdateStatus {
  owner: UpdateOwner;
  channel: UpdateChannel;
  state: UpdateState;
  currentVersion: string;
  version?: string;
  progress?: number;
  error?: string;
  manualHint?: string;
  downloadUrl?: string;
  checkedAt?: number;
}

export interface UpdaterDeps {
  stamp: BuildStamp | null;
  appVersion: string;
  /** A packaged app (else a source checkout run with `indus desktop` or a dev server). */
  packaged: boolean;
  platform: string;
  env: Readonly<Record<string, string | undefined>>;
  feed: { latest(channel: UpdateChannel): Promise<ReleaseInfo | null> };
  /** The installing engine (electron-updater), made on first apply. */
  engine(): Promise<UpdateEngine>;
  gate: {
    setInFlight(on: boolean): void;
    inFlightNow(): boolean;
    /** True while another owner's live marker holds the runtime. */
    liveMarker(): Promise<boolean>;
  };
  /** Stop what the app itself owns before installing (installer runs; daemons outlive the app). */
  stopAppWork(): Promise<void>;
  /** `updates.json` in userData (the per-device channel). */
  channelFile: string;
  emit(status: UpdateStatus): void;
  timers: Timers;
  now(): number;
  log(line: string): void;
}

export function updateOwner(stamp: BuildStamp | null): UpdateOwner {
  return stamp?.updateOwner ?? "manual";
}

function readChannel(file: string, fallback: UpdateChannel): UpdateChannel {
  const raw = readJsonFile(file) as { channel?: unknown } | null;
  return raw?.channel === "canary" || raw?.channel === "stable" ? raw.channel : fallback;
}

export class Updater {
  private readonly deps: UpdaterDeps;
  private readonly owner: UpdateOwner;
  private channel: UpdateChannel;
  private current: UpdateStatus;
  private applying: Promise<void> | null = null;
  private checking: Promise<UpdateStatus> | null = null;
  private timer: unknown = null;

  constructor(deps: UpdaterDeps) {
    this.deps = deps;
    this.owner = updateOwner(deps.stamp);
    this.channel = readChannel(deps.channelFile, deps.stamp?.channel ?? "stable");
    this.current = this.base("idle");
  }

  status(): UpdateStatus {
    return { ...this.current };
  }

  /** Look for a newer release (never downloads). Concurrent calls share one lookup. */
  check(): Promise<UpdateStatus> {
    if (this.applying !== null) return Promise.resolve(this.status());
    if (this.checking !== null) return this.checking;
    this.set(this.base("checking"));
    const run = this.lookup().finally(() => {
      this.checking = null;
    });
    this.checking = run;
    return run;
  }

  async setChannel(channel: UpdateChannel): Promise<UpdateStatus> {
    if (channel !== this.channel) {
      this.channel = channel;
      try {
        fs.mkdirSync(path.dirname(this.deps.channelFile), { recursive: true });
        writeFileAtomic(this.deps.channelFile, `${JSON.stringify({ channel })}\n`);
      } catch {
        this.deps.log("[updates] could not save the update channel");
      }
      if (this.applying === null) this.set(this.base("idle"));
    }
    return this.status();
  }

  /**
   * Start an apply. `accepted` is false when it was refused (manual owner, already running,
   * another update owns the runtime); `done` settles when the pipeline ends.
   */
  async apply(): Promise<{ accepted: boolean; done: Promise<void> }> {
    const refused = { accepted: false, done: Promise.resolve() };
    if (this.owner === "manual") return refused;
    if (this.applying !== null || this.deps.gate.inFlightNow()) return refused;
    if (await this.deps.gate.liveMarker()) {
      this.set(
        this.base("error", { error: "Another update is running. Try again when it has finished." }),
      );
      return refused;
    }
    if (this.applying !== null) return refused;
    this.deps.gate.setInFlight(true);
    const done = this.pipeline().finally(() => {
      this.applying = null;
    });
    this.applying = done;
    return { accepted: true, done };
  }

  /** Check 30 s after launch, then every 6 hours. */
  startBackgroundChecks(): void {
    if (this.timer !== null) return;
    const tick = (delay: number) => {
      this.timer = this.deps.timers.setTimeout(() => {
        void this.check().finally(() => tick(CHECK_INTERVAL_MS));
      }, delay);
    };
    tick(FIRST_CHECK_DELAY_MS);
  }

  stop(): void {
    if (this.timer !== null) this.deps.timers.clearTimeout(this.timer);
    this.timer = null;
  }

  // -- internals ---------------------------------------------------------------------------------

  private base(state: UpdateState, extra: Partial<UpdateStatus> = {}): UpdateStatus {
    return {
      owner: this.owner,
      channel: this.channel,
      state,
      currentVersion: this.deps.appVersion,
      ...extra,
    };
  }

  private set(status: UpdateStatus): void {
    this.current = status;
    this.deps.emit(this.status());
  }

  private async lookup(): Promise<UpdateStatus> {
    let release: ReleaseInfo | null;
    try {
      release = await this.deps.feed.latest(this.channel);
    } catch (error) {
      const message = error instanceof Error ? error.message : "the check failed";
      this.deps.log(`[updates] check failed: ${message}`);
      if (this.applying === null)
        this.set(this.base("error", { error: message, checkedAt: this.deps.now() }));
      return this.status();
    }
    if (this.applying !== null) return this.status();
    const checkedAt = this.deps.now();
    if (release === null || !isNewer(release.version, this.deps.appVersion)) {
      this.set(this.base("idle", { checkedAt }));
      return this.status();
    }
    if (this.owner === "manual") {
      this.set(
        this.base("manual", {
          version: release.version,
          checkedAt,
          ...(this.deps.packaged ? {} : { manualHint: MANUAL_COMMAND }),
          ...(release.url === null ? {} : { downloadUrl: release.url }),
        }),
      );
    } else {
      this.set(
        this.base("available", {
          version: release.version,
          checkedAt,
          ...(release.url === null ? {} : { downloadUrl: release.url }),
        }),
      );
    }
    return this.status();
  }

  private async pipeline(): Promise<void> {
    let stopped = false;
    try {
      if (this.deps.platform === "linux" && (this.deps.env.APPIMAGE ?? "") === "") {
        throw new Error("This build cannot update itself. Download the new version instead.");
      }
      this.set(this.base("checking"));
      const engine = await this.deps.engine();
      const found = await engine.check(this.channel);
      if (!found.available) {
        this.set(this.base("idle", { checkedAt: this.deps.now() }));
        return;
      }
      const version = found.version ?? this.current.version;
      const withVersion = version === undefined ? {} : { version };
      this.set(this.base("downloading", { ...withVersion, progress: 0 }));
      await engine.download((percent) =>
        this.set(this.base("downloading", { ...withVersion, progress: Math.round(percent) })),
      );
      // Accepted by the updater: only now does anything stop.
      this.set(this.base("ready", withVersion));
      stopped = true;
      await this.deps.stopAppWork();
      this.set(this.base("installing", withVersion));
      engine.install();
    } catch (error) {
      const message = error instanceof Error ? error.message : "the update failed";
      this.deps.log(
        `[updates] apply failed${stopped ? " after stopping app work" : "; nothing was stopped"}: ${message}`,
      );
      this.set(this.base("error", { error: message }));
    } finally {
      this.deps.gate.setInFlight(false);
    }
  }
}
