// Attach to a local profile's daemon and headless REST server (P20-10). The desktop never owns
// either: `indus [-p P] gateway ensure --json` attaches to or starts the profile daemon (P17-15,
// it never installs a service) and `indus [-p P] serve ensure --json` the profile's `indus serve`
// (P18-01). Both print credential-free JSON. The REST session token is minted here once per app
// launch and reaches `indus serve` only through INDUS_DASHBOARD_SESSION_TOKEN; it never leaves
// main (D-16.89). Tickets for the daemon's WebSocket come from its owner-only control socket, one
// per dial (tickets.ts).

import { randomBytes } from "node:crypto";
import path from "node:path";
import { redactSecrets } from "../redact.ts";
import { type ControlReply, identify, type MintReply, mintTicket, wsUrlIsSafe } from "./control.ts";
import { type CommandRunner, lastLine, type RunResult, runCommand } from "./process.ts";
import type { IndusCommand } from "./resolver.ts";

export const ENSURE_DEADLINE_S = 60;
export const ENSURE_CLIENT_GRACE_MS = 15_000;
export const SERVE_ENSURE_DEADLINE_S = 30;
export const SESSION_TOKEN_HEADER = "X-Indus-Session-Token";
/** Control errors after which the daemon is re-ensured (it may have died or restarted). */
export const REENSURE_ERRORS: ReadonlySet<string> = new Set([
  "stale_instance",
  "no_socket",
  "unreachable",
]);
/** Control errors that mean the running daemon's ticket path is stale: restart it once. */
export const RESTART_ERRORS: ReadonlySet<string> = new Set(["stale_socket"]);

export interface GatewayDescriptor {
  wsUrl: string;
  profileId: string;
  instanceId: string;
  codeVersion: string;
}

export interface ServeDescriptor {
  url: string;
  port: number;
  pid: number;
  profileId: string;
  codeVersion: string;
}

export class AttachError extends Error {
  readonly reason: string;

  constructor(reason: string, message: string) {
    super(message);
    this.name = "AttachError";
    this.reason = reason;
  }
}

export interface LocalAttachDeps {
  backend: IndusCommand;
  /** The resolved CLI's version (`indus --version`), for the skew check. */
  version: string;
  /** Named profile, or null for the default one. */
  profile: string | null;
  /** The Indus root home (named profiles live under `<home>/profiles/<name>`). */
  home: string;
  env: Record<string, string | undefined>;
  run?: CommandRunner;
  identify?: (home: string) => Promise<Record<string, unknown> | null>;
  mint?: (home: string, expect: { profileId?: string; instanceId?: string }) => Promise<MintReply>;
  fetch?: typeof fetch;
  /** Stop a serve process an earlier launch left with another token. */
  terminate?: (pid: number) => void;
  log: (line: string) => void;
}

export function newSessionToken(): string {
  return randomBytes(32).toString("base64url");
}

function profileArgs(profile: string | null): string[] {
  return profile === null || profile === "default" ? [] : ["-p", profile];
}

export function profileHome(home: string, profile: string | null): string {
  return profile === null || profile === "default" ? home : path.join(home, "profiles", profile);
}

function parseJsonObject(stdout: string): Record<string, unknown> | null {
  const text = stdout.trim();
  const line =
    text
      .split(/\r?\n/)
      .filter((l) => l.trim().startsWith("{"))
      .at(-1) ?? "";
  try {
    const value = JSON.parse(line) as unknown;
    return value !== null && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function describeFailure(what: string, result: RunResult): AttachError {
  if (result.spawnError !== null) {
    return new AttachError("spawn_failed", `${what} could not start (${result.spawnError})`);
  }
  if (result.timedOut) return new AttachError("timeout", `${what} did not answer in time`);
  const verdict = parseJsonObject(result.stdout);
  if (verdict !== null && typeof verdict.reason === "string") {
    const detail = typeof verdict.detail === "string" ? `: ${verdict.detail}` : "";
    return new AttachError(
      verdict.reason,
      redactSecrets(`${what} failed (${verdict.reason})${detail}`),
    );
  }
  const why = lastLine(result.stderr);
  return new AttachError(
    "no_result",
    redactSecrets(
      `${what} printed no result (exit ${result.code ?? "?"})${why === "" ? "" : `: ${why}`}`,
    ),
  );
}

export class LocalAttach {
  private readonly deps: LocalAttachDeps;
  private readonly run: CommandRunner;
  /** One per app launch; main keeps it and adds it to REST calls (P20-14). */
  readonly sessionToken: string;
  private gateway: GatewayDescriptor | null = null;
  private skewRestarted = false;
  /** Whether this app started the daemon (no daemon answered before the first ensure). */
  startedByApp = false;

  constructor(deps: LocalAttachDeps, sessionToken: string = newSessionToken()) {
    this.deps = deps;
    this.run = deps.run ?? runCommand;
    this.sessionToken = sessionToken;
  }

  /** The profile's own home (its control socket lives there). */
  home(): string {
    return profileHome(this.deps.home, this.deps.profile);
  }

  descriptor(): GatewayDescriptor | null {
    return this.gateway;
  }

  private env(extra: Record<string, string> = {}): Record<string, string | undefined> {
    const env: Record<string, string | undefined> = {
      ...this.deps.env,
      INDUS_HOME: this.deps.home,
      ...extra,
    };
    // never hand our launch token to a command that does not need it
    if (extra.INDUS_DASHBOARD_SESSION_TOKEN === undefined) delete env.INDUS_DASHBOARD_SESSION_TOKEN;
    return env;
  }

  /** `gateway ensure --json`: attach to or start the profile daemon. */
  async ensureGateway(): Promise<GatewayDescriptor> {
    const before = await (this.deps.identify ?? identify)(this.home());
    const result = await this.run(
      this.deps.backend,
      [
        ...profileArgs(this.deps.profile),
        "gateway",
        "ensure",
        "--json",
        "--timeout",
        String(ENSURE_DEADLINE_S),
      ],
      { env: this.env(), timeoutMs: ENSURE_DEADLINE_S * 1000 + ENSURE_CLIENT_GRACE_MS },
    );
    if (result.code !== 0) throw describeFailure("gateway ensure", result);
    const json = parseJsonObject(result.stdout);
    if (json === null) throw describeFailure("gateway ensure", { ...result, code: result.code });
    const descriptor = {
      wsUrl: json.ws_url,
      profileId: json.profile_id,
      instanceId: json.instance_id,
      codeVersion: json.code_version,
    };
    if (!wsUrlIsSafe(descriptor.wsUrl)) {
      throw new AttachError(
        "bad_descriptor",
        "gateway ensure answered a WebSocket URL that is not loopback",
      );
    }
    if (
      typeof descriptor.profileId !== "string" ||
      typeof descriptor.instanceId !== "string" ||
      typeof descriptor.codeVersion !== "string"
    ) {
      throw new AttachError("bad_descriptor", "gateway ensure answered an incomplete descriptor");
    }
    if (before === null) this.startedByApp = true;
    this.gateway = descriptor as GatewayDescriptor;
    return this.gateway;
  }

  /**
   * `gateway restart` then `gateway ensure`: replace a stale daemon (the CLI restarts an installed
   * service through its manager and a detached daemon by stop + start).
   */
  async restartGateway(reason: string): Promise<GatewayDescriptor> {
    this.deps.log(`[attach] restarting the local daemon (${reason})`);
    await this.run(
      this.deps.backend,
      [
        ...profileArgs(this.deps.profile),
        "gateway",
        "restart",
        "--wait",
        String(ENSURE_DEADLINE_S),
      ],
      { env: this.env(), timeoutMs: ENSURE_DEADLINE_S * 1000 + ENSURE_CLIENT_GRACE_MS },
    );
    const descriptor = await this.ensureGateway();
    this.startedByApp = true;
    return descriptor;
  }

  /** Ensure the daemon; a daemon on other code than the resolved CLI is restarted once per launch. */
  async attachGateway(): Promise<GatewayDescriptor> {
    let descriptor = await this.ensureGateway();
    if (descriptor.codeVersion !== this.deps.version && !this.skewRestarted) {
      this.skewRestarted = true;
      descriptor = await this.restartGateway(
        `it runs ${descriptor.codeVersion}, this CLI is ${this.deps.version}`,
      );
      if (descriptor.codeVersion !== this.deps.version) {
        this.deps.log(`[attach] the daemon still runs ${descriptor.codeVersion}; attaching as is`);
      }
    }
    return descriptor;
  }

  /** `serve ensure --json` with this launch's token; a serve holding another token is replaced once. */
  async ensureServe(): Promise<ServeDescriptor> {
    let serve = await this.serveOnce();
    if (!(await this.tokenAccepted(serve))) {
      this.deps.log(
        `[attach] the headless server (pid ${serve.pid}) holds another token; replacing it`,
      );
      (this.deps.terminate ?? ((pid) => process.kill(pid, "SIGTERM")))(serve.pid);
      await this.waitGone(serve.pid);
      serve = await this.serveOnce();
      if (!(await this.tokenAccepted(serve))) {
        throw new AttachError(
          "serve_token",
          "the headless server refuses this app's session token",
        );
      }
    }
    return serve;
  }

  private async serveOnce(): Promise<ServeDescriptor> {
    const result = await this.run(
      this.deps.backend,
      [
        ...profileArgs(this.deps.profile),
        "serve",
        "ensure",
        "--json",
        "--timeout",
        String(SERVE_ENSURE_DEADLINE_S),
      ],
      {
        env: this.env({ INDUS_DASHBOARD_SESSION_TOKEN: this.sessionToken }),
        timeoutMs: SERVE_ENSURE_DEADLINE_S * 1000 + ENSURE_CLIENT_GRACE_MS,
      },
    );
    if (result.code !== 0) throw describeFailure("serve ensure", result);
    const json = parseJsonObject(result.stdout);
    if (json === null) throw describeFailure("serve ensure", result);
    const url = json.url;
    let origin: URL | null = null;
    try {
      origin = typeof url === "string" ? new URL(url) : null;
    } catch {
      origin = null;
    }
    if (
      origin === null ||
      origin.protocol !== "http:" ||
      !["127.0.0.1", "[::1]", "localhost"].includes(origin.hostname) ||
      origin.username !== "" ||
      origin.password !== "" ||
      typeof json.pid !== "number" ||
      typeof json.port !== "number"
    ) {
      throw new AttachError(
        "bad_descriptor",
        "serve ensure answered an address that is not loopback",
      );
    }
    return {
      url: origin.origin,
      port: json.port,
      pid: json.pid,
      profileId: String(json.profile_id ?? ""),
      codeVersion: String(json.code_version ?? ""),
    };
  }

  /** One authenticated call: 401 means the serve was started with another token. */
  private async tokenAccepted(serve: ServeDescriptor): Promise<boolean> {
    try {
      const response = await (this.deps.fetch ?? fetch)(`${serve.url}/api/config`, {
        headers: { [SESSION_TOKEN_HEADER]: this.sessionToken },
        signal: AbortSignal.timeout(5_000),
      });
      await response.body?.cancel();
      return response.status !== 401 && response.status !== 403;
    } catch {
      // unreachable right after start: the proxy reports it on use
      return true;
    }
  }

  private async waitGone(pid: number): Promise<void> {
    for (let i = 0; i < 100; i += 1) {
      try {
        process.kill(pid, 0);
      } catch {
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
  }

  /**
   * Mint one ticket for the current daemon. A stale answer is handled once: a daemon that went
   * away or restarted is re-ensured, a stale ticket path restarts the daemon; then the mint is
   * tried again and its answer is final.
   */
  async mint(): Promise<MintReply> {
    const descriptor = this.gateway ?? (await this.attachGateway());
    const mint = this.deps.mint ?? mintTicket;
    const first = await mint(this.home(), {
      profileId: descriptor.profileId,
      instanceId: descriptor.instanceId,
    });
    if (first.ok) return first;
    let fresh: GatewayDescriptor;
    if (RESTART_ERRORS.has(first.error))
      fresh = await this.restartGateway(`ticket mint: ${first.error}`);
    else if (REENSURE_ERRORS.has(first.error)) {
      this.deps.log(`[attach] ticket mint: ${first.error}; ensuring the daemon again`);
      fresh = await this.ensureGateway();
    } else return first;
    return mint(this.home(), { profileId: fresh.profileId, instanceId: fresh.instanceId });
  }
}

export type { ControlReply };
