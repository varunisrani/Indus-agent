// The terminal pane's PTY host (P20-31). Main owns every shell: `node-pty` spawns it, output goes
// to the window that started it (batched, one message per 8 ms burst), input and resizes are taken
// only from that window, and the PTY is killed when the window disposes it, when the window's web
// contents is destroyed, or when the app quits — no shell outlives the app. A hidden terminal is
// untouched here (the pane keeps it mounted), so its PTY and pid stay the same.
//
// `node-pty` is a native, optional dependency loaded on first use; without it `terminal.start`
// answers `not-available` with a message instead of failing the whole app.

import nodeFs from "node:fs";
import path from "node:path";
import type { PathGuard } from "../fs/pathGuard.ts";
import { IpcError } from "../ipc/envelope.ts";
import type { EventTarget, IpcRegistry } from "../ipc/registry.ts";
import type { ResponseOf } from "../ipc/schemas.ts";
import { terminalEnv } from "./terminalEnv.ts";

export const OUTPUT_BATCH_MS = 8;
export const TERMINALS_PER_WINDOW = 16;

/** The part of a node-pty `IPty` the host uses. */
export interface Pty {
  readonly pid: number;
  onData(listener: (data: string) => void): { dispose(): void } | unknown;
  onExit(
    listener: (event: { exitCode: number; signal?: number }) => void,
  ): { dispose(): void } | unknown;
  write(data: string): void;
  resize(cols: number, rows: number): void;
  kill(signal?: string): void;
}

export interface PtySpawnOptions {
  name: string;
  cols: number;
  rows: number;
  cwd: string;
  env: Record<string, string>;
}

export type PtySpawn = (file: string, args: string[], options: PtySpawnOptions) => Pty;

/** Load node-pty's `spawn`, or null when the module is missing or not built. */
export async function loadNodePty(): Promise<PtySpawn | null> {
  const moduleName = "node-pty";
  try {
    const loaded = (await import(/* @vite-ignore */ moduleName)) as {
      spawn?: PtySpawn;
      default?: { spawn?: PtySpawn };
    };
    return loaded.spawn ?? loaded.default?.spawn ?? null;
  } catch {
    return null;
  }
}

export interface ShellSpec {
  command: string;
  args: string[];
  name: string;
}

/** The interactive shell to start. */
export function chooseShell(options: {
  platform: string;
  env: Readonly<Record<string, string | undefined>>;
  isExecutable: (file: string) => boolean;
  findOnPath: (name: string) => string | null;
}): ShellSpec {
  const { platform, env, isExecutable, findOnPath } = options;
  const flavour = platform === "win32" ? path.win32 : path.posix;
  const specFor = (command: string): ShellSpec => {
    const name = flavour
      .basename(command)
      .toLowerCase()
      .replace(/\.exe$/, "");
    if (name.startsWith("pwsh") || name.startsWith("powershell")) {
      return { command, args: ["-NoLogo"], name };
    }
    if (name === "cmd") return { command, args: [], name };
    const login = name === "bash" || name === "zsh" ? ["-il"] : ["-i"];
    return { command, args: login, name };
  };
  const wanted = (env.INDUS_DESKTOP_SHELL ?? (platform === "win32" ? "" : env.SHELL) ?? "").trim();
  if (wanted !== "") {
    const resolved = flavour.isAbsolute(wanted)
      ? isExecutable(wanted)
        ? wanted
        : null
      : findOnPath(wanted);
    if (resolved !== null) return specFor(resolved);
  }
  if (platform === "win32") {
    const root = env.SystemRoot ?? env.windir ?? "C:\\Windows";
    const builtin = path.win32.join(
      root,
      "System32",
      "WindowsPowerShell",
      "v1.0",
      "powershell.exe",
    );
    const pick =
      findOnPath("pwsh") ?? (isExecutable(builtin) ? builtin : null) ?? env.COMSPEC ?? "cmd.exe";
    return specFor(pick);
  }
  const fallback = ["/bin/zsh", "/bin/bash", "/bin/sh"].find((file) => isExecutable(file));
  return specFor(fallback ?? "/bin/sh");
}

export function isExecutableFile(file: string): boolean {
  try {
    nodeFs.accessSync(file, nodeFs.constants.X_OK);
    return nodeFs.statSync(file).isFile();
  } catch {
    return false;
  }
}

/** The owning window's web contents. */
export interface TerminalOwner extends EventTarget {
  readonly id: number;
  isDestroyed(): boolean;
  on(event: "destroyed", listener: () => void): unknown;
}

export interface PtyHostTimers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface PtyHostOptions {
  registry: Pick<IpcRegistry, "emit">;
  guard: Pick<PathGuard, "workingFolder">;
  /** node-pty's spawn, loaded lazily (null: not available in this build). */
  loadSpawn: () => Promise<PtySpawn | null>;
  shell: () => ShellSpec;
  env: Readonly<Record<string, string | undefined>>;
  platform: string;
  appVersion: string;
  contents: (id: number) => TerminalOwner | null;
  timers?: PtyHostTimers;
  newId?: () => string;
  log: (line: string) => void;
}

interface Session {
  id: string;
  ownerId: number;
  owner: TerminalOwner;
  pty: Pty;
  pending: string;
  flush: unknown;
  exited: boolean;
}

const REAL_TIMERS: PtyHostTimers = {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

export class PtyHost {
  private readonly options: PtyHostOptions;
  private readonly timers: PtyHostTimers;
  private readonly newId: () => string;
  private readonly sessions = new Map<string, Session>();
  private readonly tied = new Set<number>();
  private spawnFn: PtySpawn | null | undefined;

  constructor(options: PtyHostOptions) {
    this.options = options;
    this.timers = options.timers ?? REAL_TIMERS;
    this.newId = options.newId ?? (() => crypto.randomUUID());
  }

  private async spawner(): Promise<PtySpawn> {
    if (this.spawnFn === undefined) this.spawnFn = await this.options.loadSpawn();
    if (this.spawnFn === null) {
      throw new IpcError(
        "not-available",
        "terminals are not available in this build (the native terminal module is missing)",
      );
    }
    return this.spawnFn;
  }

  async start(
    ownerId: number,
    request: { cwd?: string; cols: number; rows: number },
  ): Promise<ResponseOf<"indus:terminal:start">> {
    const owner = this.options.contents(ownerId);
    if (owner === null || owner.isDestroyed())
      throw new IpcError("not-found", "the window is gone");
    if (this.countFor(ownerId) >= TERMINALS_PER_WINDOW) {
      throw new IpcError(
        "too-many-terminals",
        `at most ${TERMINALS_PER_WINDOW} terminals per window`,
      );
    }
    const spawnPty = await this.spawner();
    const cwd = await this.options.guard.workingFolder(ownerId, request.cwd);
    const shell = this.options.shell();
    const cols = Math.max(2, request.cols);
    const rows = Math.max(2, request.rows);
    let pty: Pty;
    try {
      pty = spawnPty(shell.command, shell.args, {
        name: "xterm-256color",
        cols,
        rows,
        cwd,
        env: terminalEnv(this.options.env, {
          platform: this.options.platform,
          appVersion: this.options.appVersion,
        }),
      });
    } catch {
      throw new IpcError("spawn-failed", `the shell ${shell.name} could not be started`);
    }
    const id = this.newId();
    const session: Session = { id, ownerId, owner, pty, pending: "", flush: null, exited: false };
    this.sessions.set(id, session);
    this.tie(owner);
    pty.onData((data) => this.onData(session, data));
    pty.onExit(({ exitCode, signal }) => this.onExit(session, exitCode, signal ?? null));
    this.options.log(`[terminal] started ${shell.name}`);
    return { terminalId: id, pid: pty.pid, shell: shell.name, cwd };
  }

  write(ownerId: number, terminalId: string, data: string): void {
    const session = this.owned(ownerId, terminalId);
    if (session === null || session.exited) return;
    session.pty.write(data);
  }

  resize(ownerId: number, terminalId: string, cols: number, rows: number): void {
    const session = this.owned(ownerId, terminalId);
    if (session === null || session.exited) return;
    try {
      session.pty.resize(Math.max(2, cols), Math.max(2, rows));
    } catch {
      // a PTY that just exited cannot be resized
    }
  }

  /** Kill one terminal (only its window may). */
  dispose(ownerId: number, terminalId: string): boolean {
    const session = this.owned(ownerId, terminalId);
    if (session === null) return false;
    this.kill(session);
    return true;
  }

  /** Kill every terminal (app quit). */
  disposeAll(): void {
    for (const session of [...this.sessions.values()]) this.kill(session);
  }

  countFor(ownerId: number): number {
    let count = 0;
    for (const session of this.sessions.values()) if (session.ownerId === ownerId) count += 1;
    return count;
  }

  size(): number {
    return this.sessions.size;
  }

  private owned(ownerId: number, terminalId: string): Session | null {
    const session = this.sessions.get(terminalId);
    return session !== undefined && session.ownerId === ownerId ? session : null;
  }

  private tie(owner: TerminalOwner): void {
    if (this.tied.has(owner.id)) return;
    this.tied.add(owner.id);
    owner.on("destroyed", () => {
      this.tied.delete(owner.id);
      for (const session of [...this.sessions.values()]) {
        if (session.ownerId === owner.id) this.kill(session);
      }
    });
  }

  private onData(session: Session, data: string): void {
    if (!this.sessions.has(session.id)) return;
    session.pending += data;
    if (session.flush !== null) return;
    session.flush = this.timers.setTimeout(() => {
      session.flush = null;
      this.flush(session);
    }, OUTPUT_BATCH_MS);
  }

  private flush(session: Session): void {
    if (session.pending === "") return;
    const data = session.pending;
    session.pending = "";
    if (session.owner.isDestroyed()) return;
    this.options.registry.emit(session.owner, "indus:terminal:data", {
      terminalId: session.id,
      data,
    });
  }

  private onExit(session: Session, exitCode: number, signal: number | null): void {
    if (session.exited) return;
    session.exited = true;
    if (session.flush !== null) this.timers.clearTimeout(session.flush);
    session.flush = null;
    this.flush(session);
    if (!session.owner.isDestroyed()) {
      this.options.registry.emit(session.owner, "indus:terminal:exit", {
        terminalId: session.id,
        exitCode,
        signal: signal === null || signal === 0 ? null : String(signal),
      });
    }
    // The child is gone; release the PTY's master side now rather than at dispose.
    this.kill(session);
  }

  private kill(session: Session): void {
    if (!this.sessions.has(session.id)) return;
    this.sessions.delete(session.id);
    if (session.flush !== null) this.timers.clearTimeout(session.flush);
    session.flush = null;
    try {
      session.pty.kill();
    } catch {
      // already gone
    }
  }
}

/** Register `indus:terminal:*`. */
export function installTerminalIpc(registry: IpcRegistry, host: PtyHost): void {
  registry.handle("indus:terminal:start", ({ cwd, cols, rows }, { senderId }) =>
    host.start(senderId, { ...(cwd === undefined ? {} : { cwd }), cols, rows }),
  );
  registry.on("indus:terminal:write", ({ terminalId, data }, { senderId }) =>
    host.write(senderId, terminalId, data),
  );
  registry.on("indus:terminal:resize", ({ terminalId, cols, rows }, { senderId }) =>
    host.resize(senderId, terminalId, cols, rows),
  );
  registry.handle("indus:terminal:dispose", ({ terminalId }, { senderId }) => {
    host.dispose(senderId, terminalId);
    return { ok: true as const };
  });
}
