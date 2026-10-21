// SSH remotes (P24-30): the desktop reaches an Indus on another machine through the system
// OpenSSH client, so `~/.ssh/config`, the agent, ProxyJump and hardware keys behave as they do in
// a terminal. No JS SSH library; no Electron import (main wires this in).
//
// One master per entry carries every exec and forward: `ControlMaster=auto`, `ControlPersist=300`,
// and a control socket named by a hash of the target under a short private directory, so the path
// plus the 17 bytes OpenSSH appends for its temporary listener stays within the 104-byte
// `sun_path` limit. Every call runs with `BatchMode=yes` (no prompt can hang a spawned ssh: a key
// with a passphrase must be in the agent) and `StrictHostKeyChecking=accept-new` (trust on first
// use, never `no`); a changed host key fails closed and the OpenSSH text is shown verbatim.
// Timeouts: connect 15 s, exec 20 s, forward 15 s, and a 15 s watchdog on the remote side of probe
// commands so a hung remote command dies with our local kill. A `-O check` runs every 60 s; a dead
// master means reconnect, never retry in place. A forward child that exits is restarted after 1 s,
// at most 5 times in a row (a run that stayed up for 30 s resets the count); after that the
// connection is reported dead.
//
// Credentials never ride in argv: the remote serve's session token is written to the remote
// command's stdin. Client-only files reach the remote as bytes over stdin (R08.23), never as a
// client path.

import { type ChildProcess, spawn as nodeSpawn } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import type { SshTarget } from "./registry.ts";

export const SSH_CONNECT_TIMEOUT_MS = 15_000;
export const SSH_EXEC_TIMEOUT_MS = 20_000;
export const SSH_FORWARD_TIMEOUT_MS = 15_000;
export const SSH_PROBE_WATCHDOG_S = 15;
export const TUNNEL_RESTART_LIMIT = 5;
export const TUNNEL_RESTART_DELAY_MS = 1_000;
export const TUNNEL_STABLE_MS = 30_000;
export const CONTROL_PERSIST_S = 300;
export const KEEPALIVE_MS = 60_000;
export const SUN_PATH_MAX = 104;
/** OpenSSH binds `<ControlPath>.<16 random chars>` while it sets the master up. */
export const LISTENER_SUFFIX_BYTES = 17;
const SERVE_ENSURE_TIMEOUT_S = 60;

export type SshErrorKind =
  | "unreachable"
  | "auth-failed"
  | "host-key-changed"
  | "timeout"
  | "bad-target"
  | "unknown";

export class SshError extends Error {
  readonly kind: SshErrorKind;
  /** OpenSSH's own text (stderr), shown verbatim; never contains a credential we passed. */
  readonly detail: string;

  constructor(kind: SshErrorKind, message: string, detail = "") {
    super(message);
    this.kind = kind;
    this.detail = detail;
  }
}

// -- targets and arguments ------------------------------------------------------------------------

/** Any control character (C0 or DEL). */
function hasControlChars(text: string): boolean {
  for (let i = 0; i < text.length; i += 1) {
    const code = text.charCodeAt(i);
    if (code < 0x20 || code === 0x7f) return true;
  }
  return false;
}

export function validateTarget(target: SshTarget): void {
  const bad = (why: string) => new SshError("bad-target", why);
  if (!target.host || target.host.startsWith("-") || hasControlChars(target.host)) {
    throw bad("the host name is not usable");
  }
  if (!/^[A-Za-z0-9._-]+$|^\[?[0-9A-Fa-f:.]+(%[A-Za-z0-9._-]+)?\]?$/.test(target.host)) {
    throw bad("the host name is not usable");
  }
  if (
    target.user !== undefined &&
    (target.user.startsWith("-") || !/^[A-Za-z0-9._-]+$/.test(target.user))
  ) {
    throw bad("the user name is not usable");
  }
  if (
    target.port !== undefined &&
    (!Number.isInteger(target.port) || target.port < 1 || target.port > 65_535)
  ) {
    throw bad("the port must be 1–65535");
  }
  if (
    target.keyPath !== undefined &&
    (target.keyPath.startsWith("-") || hasControlChars(target.keyPath))
  ) {
    throw bad("the key file path is not usable");
  }
}

export function destination(target: SshTarget): string {
  return target.user ? `${target.user}@${target.host}` : target.host;
}

export function describeTarget(target: SshTarget): string {
  return `${destination(target)}${target.port && target.port !== 22 ? `:${target.port}` : ""}`;
}

/** The control socket for `target`: a hashed name under `preferredDir`, or `fallbackDir` when the
 * preferred path plus OpenSSH's listener suffix would pass 104 bytes. */
export function controlPath(target: SshTarget, preferredDir: string, fallbackDir: string): string {
  const id = createHash("sha256")
    .update(
      JSON.stringify([
        target.user ?? "",
        target.host.toLowerCase(),
        target.port ?? 22,
        target.keyPath ?? "",
      ]),
    )
    .digest("hex")
    .slice(0, 16);
  const name = `${id}.sock`;
  const fits = (dir: string) =>
    Buffer.byteLength(path.join(dir, name)) + LISTENER_SUFFIX_BYTES <= SUN_PATH_MAX;
  if (fits(preferredDir)) return path.join(preferredDir, name);
  if (fits(fallbackDir)) return path.join(fallbackDir, name);
  throw new SshError("bad-target", "no short enough folder for the SSH control socket");
}

/** The short fallback directory (owner-only; checked before use). */
export function shortControlDir(uid: number): string {
  return `/tmp/indus-ssh-${uid}`;
}

/** Make `dir` (0700) and refuse one that another user owns or others can write. */
export function ensureControlDir(dir: string): void {
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const stat = fs.lstatSync(dir);
  const uid = typeof process.getuid === "function" ? process.getuid() : -1;
  if (!stat.isDirectory() || (uid >= 0 && stat.uid !== uid) || (stat.mode & 0o077) !== 0) {
    throw new SshError("bad-target", `the SSH control folder ${dir} is not private to this user`);
  }
}

export function baseOptions(
  control: string | null,
  connectTimeoutMs = SSH_CONNECT_TIMEOUT_MS,
): string[] {
  const mux =
    control === null
      ? []
      : [
          "-o",
          `ControlPath=${control}`,
          "-o",
          "ControlMaster=auto",
          "-o",
          `ControlPersist=${CONTROL_PERSIST_S}`,
        ];
  return [
    ...mux,
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ExitOnForwardFailure=yes",
    "-o",
    `ConnectTimeout=${Math.max(1, Math.round(connectTimeoutMs / 1000))}`,
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=3",
    "-o",
    "TCPKeepAlive=yes",
  ];
}

export function hostArgs(target: SshTarget): string[] {
  const out: string[] = [];
  if (target.port !== undefined && target.port !== 22) out.push("-p", String(target.port));
  if (target.keyPath !== undefined) out.push("-i", target.keyPath);
  return out;
}

/** Quote for the remote login shell (POSIX). */
export function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

/** Run `command` remotely under a watchdog that kills it after `seconds`. */
export function withWatchdog(command: string, seconds = SSH_PROBE_WATCHDOG_S): string {
  // the watchdog keeps no pipe of the session open and takes its sleep with it when it is stopped
  const dog = `trap 'kill $s 2>/dev/null; exit 0' TERM; sleep ${seconds} & s=$!; wait $s; kill -9 $p 2>/dev/null`;
  const inner = `(${command}) & p=$!; (${dog}) < /dev/null > /dev/null 2>&1 & w=$!; wait $p; r=$?; kill $w 2>/dev/null; exit $r`;
  return `sh -c ${shellQuote(inner)}`;
}

export function classifySshError(stderr: string): SshErrorKind {
  if (
    /REMOTE HOST IDENTIFICATION HAS CHANGED|Host key verification failed|Offending (key|ECDSA|RSA|ED25519)/i.test(
      stderr,
    )
  ) {
    return "host-key-changed";
  }
  if (
    /Permission denied|Too many authentication failures|no matching host key|publickey|keyboard-interactive/i.test(
      stderr,
    )
  ) {
    return "auth-failed";
  }
  if (
    /Could not resolve hostname|Connection refused|Connection timed out|No route to host|Network is unreachable|Operation timed out|port \d+: Connection/i.test(
      stderr,
    )
  ) {
    return "unreachable";
  }
  return "unknown";
}

export function sshErrorMessage(kind: SshErrorKind, target: SshTarget, stderr: string): string {
  const who = describeTarget(target);
  const text = stderr.trim();
  switch (kind) {
    case "host-key-changed":
      return `The host key of ${who} has changed since the last connection. SSH refused to connect: this may be an attack, or the machine was reinstalled. If the change is expected, remove the old key with \`ssh-keygen -R ${target.host}\` and connect again.\n\n${text}`;
    case "auth-failed":
      return `SSH could not sign in to ${who}. The app never asks for a password or passphrase: load the key into your SSH agent (ssh-add) or set it in ~/.ssh/config.\n\n${text}`;
    case "unreachable":
      return `${who} could not be reached over SSH.\n\n${text}`;
    case "timeout":
      return `${who} did not answer over SSH in time.`;
    default:
      return `SSH to ${who} failed.\n\n${text}`;
  }
}

// -- processes ------------------------------------------------------------------------------------

export type SpawnLike = (
  command: string,
  args: readonly string[],
  options: { env?: NodeJS.ProcessEnv; stdio: ["pipe" | "ignore", "pipe", "pipe"] },
) => ChildProcess;

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
  setInterval(fn: () => void, ms: number): unknown;
  clearInterval(handle: unknown): void;
}

const realTimers: Timers = {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
  setInterval: (fn, ms) => setInterval(fn, ms),
  clearInterval: (handle) => clearInterval(handle as ReturnType<typeof setInterval>),
};

export interface RunResult {
  code: number | null;
  stdout: string;
  stderr: string;
  timedOut: boolean;
}

export interface SshDeps {
  sshPath?: string;
  spawn?: SpawnLike;
  env?: NodeJS.ProcessEnv;
  /** The preferred control folder (e.g. `<userData>/ssh`); the short `/tmp` one is the fallback. */
  controlDir: string;
  fallbackDir?: string;
  platform?: string;
  timers?: Timers;
  now?: () => number;
  log: (line: string) => void;
  restartDelayMs?: number;
  stableMs?: number;
}

function run(
  deps: SshDeps,
  args: readonly string[],
  options: { stdin?: string | Buffer; timeoutMs: number },
): Promise<RunResult> {
  return new Promise((resolve) => {
    const child = (deps.spawn ?? (nodeSpawn as unknown as SpawnLike))(deps.sshPath ?? "ssh", args, {
      env: deps.env ?? process.env,
      stdio: [options.stdin === undefined ? "ignore" : "pipe", "pipe", "pipe"],
    });
    const out: Buffer[] = [];
    const err: Buffer[] = [];
    let timedOut = false;
    const timers = deps.timers ?? realTimers;
    const timer = timers.setTimeout(() => {
      timedOut = true;
      child.kill("SIGKILL");
    }, options.timeoutMs);
    child.stdout?.on("data", (chunk: Buffer) => out.push(chunk));
    child.stderr?.on("data", (chunk: Buffer) => err.push(chunk));
    child.on("error", () => undefined);
    child.on("close", (code) => {
      timers.clearTimeout(timer);
      resolve({
        code,
        stdout: Buffer.concat(out).toString("utf8"),
        stderr: Buffer.concat(err).toString("utf8").slice(-8192),
        timedOut,
      });
    });
    if (options.stdin !== undefined) {
      child.stdin?.on("error", () => undefined);
      child.stdin?.end(options.stdin);
    }
  });
}

// -- tunnels --------------------------------------------------------------------------------------

export interface Tunnel {
  readonly localPort: number;
  readonly remotePort: number;
  restarts(): number;
  alive(): boolean;
  close(): void;
}

/** A free 127.0.0.1 port. */
export function pickLocalPort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const port = (server.address() as net.AddressInfo).port;
      server.close(() => resolve(port));
    });
  });
}

// -- the connection -------------------------------------------------------------------------------

export interface ServeOnRemote {
  remotePort: number;
  url: string;
}

export class SshConnection {
  readonly target: SshTarget;
  private readonly deps: SshDeps;
  private readonly control: string | null;
  private readonly tunnels = new Set<Tunnel>();
  private keepalive: unknown = null;
  private dead = false;
  private readonly deadListeners = new Set<(why: string) => void>();

  constructor(target: SshTarget, deps: SshDeps) {
    validateTarget(target);
    this.target = target;
    this.deps = deps;
    const uid = typeof process.getuid === "function" ? process.getuid() : 0;
    this.control =
      (deps.platform ?? process.platform) === "win32"
        ? null // OpenSSH on Windows has no mux sockets: each call signs in on its own
        : controlPath(target, deps.controlDir, deps.fallbackDir ?? shortControlDir(uid));
  }

  controlSocket(): string | null {
    return this.control;
  }

  private args(extra: readonly string[], command?: string, connectTimeoutMs?: number): string[] {
    const out = [
      ...baseOptions(this.control, connectTimeoutMs),
      ...hostArgs(this.target),
      ...extra,
      "--",
      destination(this.target),
    ];
    if (command !== undefined) out.push(command);
    return out;
  }

  private fail(result: RunResult): SshError {
    if (result.timedOut)
      return new SshError("timeout", sshErrorMessage("timeout", this.target, ""));
    const kind = classifySshError(result.stderr);
    return new SshError(
      kind,
      sshErrorMessage(kind, this.target, result.stderr),
      result.stderr.trim(),
    );
  }

  onDead(listener: (why: string) => void): () => void {
    this.deadListeners.add(listener);
    return () => this.deadListeners.delete(listener);
  }

  private markDead(why: string): void {
    if (this.dead) return;
    this.dead = true;
    this.deps.log(`[ssh] ${describeTarget(this.target)}: ${why}`);
    for (const listener of [...this.deadListeners]) listener(why);
  }

  isDead(): boolean {
    return this.dead;
  }

  /** Open (or reuse) the master; a changed host key or failed sign-in is thrown, never retried. */
  async connect(): Promise<void> {
    if (this.control !== null) ensureControlDir(path.dirname(this.control));
    const result = await run(this.deps, this.args([], "true", SSH_CONNECT_TIMEOUT_MS), {
      timeoutMs: SSH_CONNECT_TIMEOUT_MS,
    });
    if (result.code !== 0) throw this.fail(result);
    this.dead = false;
    this.startKeepalive();
  }

  private startKeepalive(): void {
    if (this.keepalive !== null || this.control === null) return;
    const timers = this.deps.timers ?? realTimers;
    this.keepalive = timers.setInterval(() => {
      void this.check().then((ok) => {
        if (!ok) this.markDead("the SSH master stopped answering");
      });
    }, KEEPALIVE_MS);
  }

  /** `ssh -O check`: is the master alive? */
  async check(): Promise<boolean> {
    if (this.control === null) return !this.dead;
    const result = await run(this.deps, this.args(["-O", "check"]), {
      timeoutMs: SSH_EXEC_TIMEOUT_MS,
    });
    return result.code === 0;
  }

  /** Run `command` on the remote; `stdin` is written to it (credentials and file bytes go here). */
  async exec(
    command: string,
    options: { stdin?: string | Buffer; timeoutMs?: number } = {},
  ): Promise<RunResult> {
    const result = await run(this.deps, this.args(["-T"], command), {
      timeoutMs: options.timeoutMs ?? SSH_EXEC_TIMEOUT_MS,
      ...(options.stdin === undefined ? {} : { stdin: options.stdin }),
    });
    if (result.timedOut) {
      this.markDead("an SSH command timed out; the connection will be rebuilt");
      throw this.fail(result);
    }
    if (result.code === 255) throw this.fail(result);
    return result;
  }

  /** A probe (`indus --version` and the like) under the remote watchdog. */
  probe(command: string): Promise<RunResult> {
    return this.exec(withWatchdog(command));
  }

  /** Client-only bytes onto the remote (R08.23): the remote picks the path; no client path goes. */
  async uploadBytes(bytes: Buffer, name: string): Promise<string> {
    const safe =
      path
        .basename(name)
        .replace(/[^A-Za-z0-9._-]+/g, "_")
        .slice(0, 120) || "upload";
    const script = `d=$(mktemp -d) && cat > "$d"/${shellQuote(safe)} && printf '%s' "$d"/${shellQuote(safe)}`;
    const result = await this.exec(script, { stdin: bytes, timeoutMs: 120_000 });
    if (result.code !== 0 || result.stdout.trim() === "") {
      throw new SshError(
        "unknown",
        "the file could not be written on the remote",
        result.stderr.trim(),
      );
    }
    return result.stdout.trim();
  }

  /** Start (or find) the remote `indus serve` with `token` sent over stdin; answers its port. */
  async ensureServe(token: string, profile?: string): Promise<ServeOnRemote> {
    const profileArgs = profile ? ` -p ${shellQuote(profile)}` : "";
    const script =
      "IFS= read -r INDUS_DASHBOARD_SESSION_TOKEN && export INDUS_DASHBOARD_SESSION_TOKEN && " +
      `exec indus${profileArgs} serve ensure --json --timeout ${SERVE_ENSURE_TIMEOUT_S}`;
    const result = await this.exec(script, {
      stdin: `${token}\n`,
      timeoutMs: (SERVE_ENSURE_TIMEOUT_S + 15) * 1000,
    });
    if (result.code !== 0) {
      throw new SshError(
        "unknown",
        "the remote Indus server did not start",
        result.stderr.trim().slice(-2000),
      );
    }
    let parsed: { url?: unknown } | null = null;
    for (const line of result.stdout.split(/\r?\n/).reverse()) {
      try {
        parsed = JSON.parse(line) as { url?: unknown };
        break;
      } catch {
        // keep looking
      }
    }
    const url = typeof parsed?.url === "string" ? parsed.url : "";
    let port = 0;
    try {
      port = Number(new URL(url).port);
    } catch {
      port = 0;
    }
    if (!Number.isInteger(port) || port < 1) {
      throw new SshError("unknown", "the remote Indus server gave no address");
    }
    return { remotePort: port, url };
  }

  /** Forward `127.0.0.1:localPort` here to `127.0.0.1:remotePort` there, restarted when it dies. */
  forward(localPort: number, remotePort: number, onDead?: () => void): Tunnel {
    const timers = this.deps.timers ?? realTimers;
    const now = this.deps.now ?? Date.now;
    const spec = `127.0.0.1:${localPort}:127.0.0.1:${remotePort}`;
    let child: ChildProcess | null = null;
    let closed = false;
    let consecutive = 0;
    let total = 0;
    let startedAt = 0;
    let pending: unknown = null;
    const spawnChild = () => {
      startedAt = now();
      child = (this.deps.spawn ?? (nodeSpawn as unknown as SpawnLike))(
        this.deps.sshPath ?? "ssh",
        this.args(["-N", "-L", spec], undefined, SSH_FORWARD_TIMEOUT_MS),
        { env: this.deps.env ?? process.env, stdio: ["ignore", "pipe", "pipe"] },
      );
      child.on("error", () => undefined);
      child.stdout?.resume();
      child.stderr?.resume();
      child.on("exit", () => {
        child = null;
        if (closed) return;
        if (now() - startedAt >= (this.deps.stableMs ?? TUNNEL_STABLE_MS)) consecutive = 0;
        if (consecutive >= TUNNEL_RESTART_LIMIT) {
          closed = true;
          this.deps.log(`[ssh] the forward to port ${remotePort} kept failing; giving up`);
          onDead?.();
          return;
        }
        consecutive += 1;
        total += 1;
        pending = timers.setTimeout(
          spawnChild,
          this.deps.restartDelayMs ?? TUNNEL_RESTART_DELAY_MS,
        );
      });
    };
    spawnChild();
    const tunnel: Tunnel = {
      localPort,
      remotePort,
      restarts: () => total,
      alive: () => !closed,
      close: () => {
        closed = true;
        if (pending !== null) timers.clearTimeout(pending);
        (child as ChildProcess | null)?.kill("SIGTERM");
        this.tunnels.delete(tunnel);
      },
    };
    this.tunnels.add(tunnel);
    return tunnel;
  }

  /** Stop the tunnels, the keepalive and the master. */
  async close(): Promise<void> {
    for (const tunnel of [...this.tunnels]) tunnel.close();
    const timers = this.deps.timers ?? realTimers;
    if (this.keepalive !== null) timers.clearInterval(this.keepalive);
    this.keepalive = null;
    if (this.control !== null) {
      await run(this.deps, this.args(["-O", "exit"]), { timeoutMs: SSH_EXEC_TIMEOUT_MS });
    }
  }
}

// -- what the registry dials ----------------------------------------------------------------------

export interface SshEntryLike {
  id: string;
  ssh?: SshTarget;
}

/** One SSH connection per registry entry; `prepare` gives the local URL of the remote serve. */
export class SshBackends {
  private readonly deps: SshDeps;
  private readonly open = new Map<string, { conn: SshConnection; serve: Tunnel; url: string }>();
  private readonly previews = new Map<string, Map<number, Tunnel>>();

  constructor(deps: SshDeps) {
    this.deps = deps;
  }

  async prepare(entry: SshEntryLike, token: string): Promise<{ url: string }> {
    const target = entry.ssh;
    if (target === undefined) throw new SshError("bad-target", "this connection has no SSH target");
    const existing = this.open.get(entry.id);
    if (existing !== undefined && !existing.conn.isDead() && existing.serve.alive()) {
      return { url: existing.url };
    }
    if (existing !== undefined) await this.drop(entry.id);
    const conn = new SshConnection(target, this.deps);
    await conn.connect();
    const serve = await conn.ensureServe(token, target.remoteProfile);
    const localPort = await pickLocalPort();
    const tunnel = conn.forward(localPort, serve.remotePort, () => void this.drop(entry.id));
    const url = `http://127.0.0.1:${localPort}`;
    this.open.set(entry.id, { conn, serve: tunnel, url });
    return { url };
  }

  /** A local port for the remote's `localhost:remotePort` (the preview's links). */
  async previewPort(entryId: string, remotePort: number): Promise<number> {
    const open = this.open.get(entryId);
    if (open === undefined) throw new SshError("unknown", "this SSH connection is not open");
    const known = this.previews.get(entryId) ?? new Map<number, Tunnel>();
    this.previews.set(entryId, known);
    const found = known.get(remotePort);
    if (found?.alive()) return found.localPort;
    const localPort = await pickLocalPort();
    known.set(remotePort, open.conn.forward(localPort, remotePort));
    return localPort;
  }

  connection(entryId: string): SshConnection | null {
    return this.open.get(entryId)?.conn ?? null;
  }

  async drop(entryId: string): Promise<void> {
    const open = this.open.get(entryId);
    this.open.delete(entryId);
    for (const tunnel of this.previews.get(entryId)?.values() ?? []) tunnel.close();
    this.previews.delete(entryId);
    await open?.conn.close();
  }

  async closeAll(): Promise<void> {
    for (const id of [...this.open.keys()]) await this.drop(id);
  }
}

/** Rewrite a `localhost:N` / `127.0.0.1:N` link to the forwarded local port. */
export function rewritePreviewUrl(raw: string, localPort: number): string {
  const url = new URL(raw);
  if (url.hostname !== "localhost" && url.hostname !== "127.0.0.1") return raw;
  url.hostname = "127.0.0.1";
  url.port = String(localPort);
  return url.toString();
}
