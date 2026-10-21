// The boot orchestrator (P20-12): one object in main owns "give the windows a connection".
// See docs/notes/P20/boot-orchestrator.md. The rules that matter:
//
// - Fencing (R08.1): every attempt carries a token checked after each await; a superseded
//   attempt's late success publishes nothing, its failure publishes nothing and leaves the newer
//   attempt's profile pin alone.
// - Latching (R08.2): a local failure latches in this process. An installer failure is cleared
//   only by Repair or Reset; any other local start failure also by Retry. A latched start
//   rethrows the same error silently. Remote failures never latch, except a confirmed reauth
//   rejection (its own latch); transient remote failures are retryable.
// - A saved remote is tried first and a failing remote never starts a local daemon.

import type { SavedRemote } from "../backend/remote.ts";
import type { Resolution } from "../backend/resolver.ts";
import type { TicketAnswer } from "../backend/tickets.ts";
import type { InstallOutcome } from "../installer/runner.ts";
import { type BootProgress, INITIAL_PROGRESS, PHASE_PROGRESS, ProgressBoard } from "./progress.ts";

export const MAX_SOFT_REPAIRS = 3;
export const DEFAULT_FAKE_STEP_MS = 650;

/** Credential-free: what `indus:connection:get` answers. */
export interface ConnectionDescriptor {
  mode: "local" | "remote";
  profile: string | null;
  profileId: string;
  instanceId: string;
  codeVersion: string;
  wsUrl: string;
}

/** A live connection as main holds it (the REST credential never leaves main). */
export interface Connection {
  descriptor: ConnectionDescriptor;
  rest: { origin: string; token: string };
  /** A fresh single-use dial for `webContentsId`. */
  ticket(webContentsId: number): Promise<TicketAnswer>;
}

export type FirstRunChoice = "install-local" | "connect-remote" | "reset";
export type RetryAction = "retry" | "repair" | "reset";

export type BootErrorKind = "local" | "installer" | "remote" | "reauth";

export class BootError extends Error {
  readonly kind: BootErrorKind;
  readonly retryable: boolean;
  readonly statusCode: number | undefined;

  constructor(
    kind: BootErrorKind,
    message: string,
    options: { retryable?: boolean; statusCode?: number } = {},
  ) {
    super(message);
    this.name = "BootError";
    this.kind = kind;
    this.retryable = options.retryable ?? false;
    this.statusCode = options.statusCode;
  }
}

/** Thrown by `connectRemote` for a failure the orchestrator classifies. */
export class RemoteConnectError extends Error {
  readonly reauth: boolean;
  readonly statusCode: number | undefined;

  constructor(message: string, options: { reauth?: boolean; statusCode?: number } = {}) {
    super(message);
    this.name = "RemoteConnectError";
    this.reauth = options.reauth ?? false;
    this.statusCode = options.statusCode;
  }
}

export class Superseded extends Error {}
export class FirstRunReset extends Error {}

export interface BootHooks {
  /** INDUS_DESKTOP_BOOT_FAKE=1: wait `stepMs` at every progress step. */
  fake: boolean;
  /** INDUS_DESKTOP_BOOT_FAKE_ERROR: fail at resolve with this text. */
  fakeError: string | null;
  stepMs: number;
}

export function bootHooks(env: Readonly<Record<string, string | undefined>>): BootHooks {
  const step = Number(env.INDUS_DESKTOP_BOOT_FAKE_STEP_MS);
  const fakeError = (env.INDUS_DESKTOP_BOOT_FAKE_ERROR ?? "").trim();
  return {
    fake: env.INDUS_DESKTOP_BOOT_FAKE === "1",
    fakeError: fakeError === "" ? null : fakeError,
    stepMs: Number.isFinite(step) && step >= 0 ? Math.min(step, 60_000) : DEFAULT_FAKE_STEP_MS,
  };
}

export interface OrchestratorDeps {
  resolve(options: { repair: boolean }): Promise<Resolution>;
  install(): Promise<InstallOutcome>;
  attachLocal(
    resolution: Extract<Resolution, { kind: "found" }>,
    profile: string | null,
  ): Promise<Connection>;
  savedRemote(): SavedRemote | null;
  connectRemote(saved: SavedRemote, profile: string | null): Promise<Connection>;
  /** Resolves once no update owns the runtime (P20-37); `updateRunning` says whether to show it. */
  waitForUpdate(): Promise<void>;
  updateRunning?(): boolean;
  broadcast(progress: BootProgress): void;
  /** The published connection changed (null: none). */
  onConnection(connection: Connection | null): void;
  sleep(ms: number): Promise<void>;
  hooks: BootHooks;
  log(line: string): void;
}

interface Attempt {
  token: number;
  profile: string | null;
  promise: Promise<Connection>;
}

export class BootOrchestrator {
  private readonly deps: OrchestratorDeps;
  private readonly board = new ProgressBoard();
  private seq = 0;
  private attempt: Attempt | null = null;
  private connection: Connection | null = null;
  private connectionProfile: string | null = null;
  /** The profile the current attempt boots (R08.1: a stale failure never clears it). */
  pinnedProfile: string | null = null;
  private installerLatch: BootError | null = null;
  private startLatch: BootError | null = null;
  private reauthLatch: BootError | null = null;
  private repairRequested = false;
  private repairs = 0;
  private localConfirmed = false;
  private choice: ((choice: FirstRunChoice) => void) | null = null;

  constructor(deps: OrchestratorDeps) {
    this.deps = deps;
    this.board.onChange((progress) => deps.broadcast(progress));
  }

  progress(): BootProgress {
    return this.board.current();
  }

  current(): Connection | null {
    return this.connection;
  }

  waitingForChoice(): boolean {
    return this.choice !== null;
  }

  /** Get (or join, or start) the connection for `profile`. */
  async start(profile: string | null = null): Promise<Connection> {
    if (this.connection !== null && this.connectionProfile === profile) return this.connection;
    if (this.attempt !== null && this.attempt.profile === profile)
      return this.follow(this.attempt.promise);
    const latched = this.latched();
    if (latched !== null) throw latched; // silent: no log, no progress, no installer
    if (this.choice !== null) {
      // an older attempt waits at the chooser: end it (it finds itself superseded)
      const settle = this.choice;
      this.choice = null;
      settle("reset");
    }
    const token = ++this.seq;
    this.pinnedProfile = profile;
    const attempt: Attempt = { token, profile, promise: Promise.resolve() as never };
    this.attempt = attempt;
    attempt.promise = this.run(token, profile).then(
      (connection) => this.succeeded(token, profile, connection),
      (error: unknown) => this.failed(token, profile, error),
    );
    return this.follow(attempt.promise);
  }

  /** A superseded attempt's callers follow the attempt that replaced it. */
  private async follow(promise: Promise<Connection>): Promise<Connection> {
    try {
      return await promise;
    } catch (error) {
      if (error instanceof Superseded && this.attempt !== null)
        return this.follow(this.attempt.promise);
      if (error instanceof Superseded && this.connection !== null) return this.connection;
      throw error;
    }
  }

  private latched(): BootError | null {
    if (this.deps.savedRemote() !== null) return this.reauthLatch;
    return this.installerLatch ?? this.startLatch;
  }

  private check(token: number): void {
    if (this.attempt?.token !== token)
      throw new Superseded("a newer boot attempt replaced this one");
  }

  private async step(token: number, progress: BootProgress): Promise<void> {
    this.check(token);
    this.board.update(progress);
    if (this.deps.hooks.fake) {
      await this.deps.sleep(this.deps.hooks.stepMs);
      this.check(token);
    }
  }

  private async run(token: number, profile: string | null): Promise<Connection> {
    await this.step(token, {
      phase: "resolve",
      message: "Looking for Indus",
      progress: PHASE_PROGRESS.resolve,
      running: true,
    });
    if (this.deps.hooks.fakeError !== null) throw new BootError("local", this.deps.hooks.fakeError);

    const saved = this.deps.savedRemote();
    if (saved !== null) return this.remote(token, saved, profile);

    if (this.deps.updateRunning?.() === true) {
      await this.step(token, {
        phase: "update-wait",
        message: "Waiting for the update to finish",
        progress: this.board.current().progress,
        running: true,
      });
    }
    await this.deps.waitForUpdate();
    this.check(token);

    await this.step(token, {
      phase: "runtime",
      message: "Finding the Indus runtime",
      progress: PHASE_PROGRESS.runtime,
      running: true,
    });
    let resolution = await this.deps.resolve({ repair: this.repairRequested });
    this.check(token);

    if (resolution.kind === "bootstrap-needed") {
      if (!this.localConfirmed) {
        await this.step(token, {
          phase: "first-run",
          message: "Choose how to set up Indus",
          progress: this.board.current().progress,
          running: false,
        });
        // `choose` / `supersede` clear `this.choice` before settling it
        const choice = await new Promise<FirstRunChoice>((resolve) => {
          this.choice = resolve;
        });
        this.check(token);
        if (choice === "reset") throw new FirstRunReset("first-run setup was reset");
        if (choice === "connect-remote") {
          const applied = this.deps.savedRemote();
          if (applied === null) throw new BootError("remote", "no remote connection is saved yet");
          return this.remote(token, applied, profile);
        }
        this.localConfirmed = true;
      }
      if (this.installerLatch !== null) throw this.installerLatch;
      this.repairRequested = false;
      await this.step(token, {
        phase: "runtime",
        message: "Installing Indus",
        progress: PHASE_PROGRESS.runtime,
        running: true,
      });
      const outcome = await this.deps.install();
      this.check(token);
      if (!outcome.ok) {
        this.installerLatch = new BootError(
          "installer",
          outcome.cancelled
            ? "the installation was cancelled"
            : `the installation failed: ${outcome.error}`,
        );
        throw this.installerLatch;
      }
      resolution = await this.deps.resolve({ repair: false });
      this.check(token);
      if (resolution.kind !== "found") {
        throw new BootError(
          "local",
          "the installer finished, but no usable Indus runtime was found",
        );
      }
    }

    await this.step(token, {
      phase: "runtime",
      message: "Starting the agent",
      progress: PHASE_PROGRESS.runtime,
      running: true,
    });
    const connection = await this.deps.attachLocal(resolution, profile);
    this.check(token);
    return connection;
  }

  private async remote(
    token: number,
    saved: SavedRemote,
    profile: string | null,
  ): Promise<Connection> {
    await this.step(token, {
      phase: "remote-connect",
      message: `Connecting to ${new URL(saved.url).host}`,
      progress: PHASE_PROGRESS["remote-connect"],
      running: true,
    });
    try {
      const connection = await this.deps.connectRemote(saved, profile);
      this.check(token);
      return connection;
    } catch (error) {
      if (error instanceof Superseded) throw error;
      this.check(token);
      if (error instanceof RemoteConnectError) {
        throw new BootError(error.reauth ? "reauth" : "remote", error.message, {
          retryable: !error.reauth,
          ...(error.statusCode === undefined ? {} : { statusCode: error.statusCode }),
        });
      }
      throw new BootError("remote", error instanceof Error ? error.message : String(error), {
        retryable: true,
      });
    }
  }

  private succeeded(token: number, profile: string | null, connection: Connection): Connection {
    this.check(token);
    this.attempt = null;
    this.connection = connection;
    this.connectionProfile = profile;
    this.repairs = 0;
    this.board.update({
      phase: "ready",
      message:
        connection.descriptor.mode === "remote"
          ? "Connected to the remote Indus"
          : "Indus is ready",
      progress: PHASE_PROGRESS.ready,
      running: true,
    });
    this.deps.onConnection(connection);
    return connection;
  }

  private failed(token: number, profile: string | null, error: unknown): never {
    if (this.attempt?.token !== token) {
      // a superseded attempt: publish nothing, leave the newer attempt's pin alone
      throw error instanceof Superseded ? error : new Superseded("superseded");
    }
    this.attempt = null;
    this.pinnedProfile = null;
    if (error instanceof Superseded) throw error;
    if (error instanceof FirstRunReset) {
      this.board.update({ ...INITIAL_PROGRESS, message: "Setup was reset" });
      // begin again so the chooser comes back
      queueMicrotask(() => this.restart(profile));
      throw error;
    }
    const boot =
      error instanceof BootError
        ? error
        : new BootError("local", error instanceof Error ? error.message : String(error));
    if (boot.kind === "local") this.startLatch = boot;
    if (boot.kind === "reauth") this.reauthLatch = boot;
    const latched = boot.kind === "local" || boot.kind === "installer";
    this.deps.log(`[boot] failed (${boot.kind}): ${boot.message}`);
    this.board.update({
      phase: "error",
      message: `Indus could not start: ${boot.message}`,
      progress: this.board.current().progress,
      running: false,
      error: boot.message,
      retryable: boot.retryable,
      ...(boot.statusCode === undefined ? {} : { statusCode: boot.statusCode }),
      ...(latched ? { latched: true } : {}),
    });
    throw boot;
  }

  /** Drop the published connection and any running attempt (the next start begins afresh). */
  private supersede(): void {
    this.seq += 1;
    this.attempt = null;
    if (this.choice !== null) {
      const settle = this.choice;
      this.choice = null;
      settle("reset");
    }
    if (this.connection !== null) {
      this.connection = null;
      this.deps.onConnection(null);
    }
  }

  /** The renderer's Retry / Repair / Reset. */
  retry(action: RetryAction): { accepted: boolean } {
    const profile = this.pinnedProfile ?? this.connectionProfile;
    if (action === "repair") {
      if (this.repairs >= MAX_SOFT_REPAIRS) {
        this.deps.log("[boot] repair refused: three repairs already ran in this session");
        return { accepted: false };
      }
      this.repairs += 1;
      this.installerLatch = null;
      this.startLatch = null;
      this.repairRequested = true;
      this.localConfirmed = true;
    } else if (action === "reset") {
      this.installerLatch = null;
      this.startLatch = null;
      this.reauthLatch = null;
      this.repairRequested = false;
      this.localConfirmed = false;
    } else {
      if (this.connection !== null && this.attempt === null) return { accepted: false };
      this.startLatch = null;
    }
    this.supersede();
    this.restart(profile);
    return { accepted: true };
  }

  /** The first-run chooser's answer. */
  choose(choice: FirstRunChoice): { accepted: boolean } {
    if (this.choice === null) return { accepted: false };
    if (choice === "connect-remote" && this.deps.savedRemote() === null) return { accepted: false };
    const settle = this.choice;
    this.choice = null;
    if (choice === "reset") this.localConfirmed = false;
    settle(choice);
    return { accepted: true };
  }

  /** A remote was saved or forgotten: clear the latches and boot the new route. */
  applyConnectionChange(): void {
    const profile = this.pinnedProfile ?? this.connectionProfile;
    this.installerLatch = null;
    this.startLatch = null;
    this.reauthLatch = null;
    this.supersede();
    this.restart(profile);
  }

  /** The published route died (remote liveness, P20-11): reconnect without latching anything. */
  reconnect(): void {
    const profile = this.connectionProfile;
    this.supersede();
    this.restart(profile);
  }

  private restart(profile: string | null): void {
    this.start(profile).catch(() => {
      // the failure is on the progress board already
    });
  }
}
