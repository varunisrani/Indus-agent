// How the TUI reaches its agent (P17-19). By default it attaches to the profile's shared daemon over
// the loopback WebSocket; `--local` (INDUS_TUI_LOCAL=1) keeps the P12 stdio child; an explicit
// INDUS_TUI_GATEWAY_URL is dialled as is and never falls back to anything.
//
// Attaching takes a single-use ticket that only the owner-only control socket mints, so the
// renderer runs `indus gateway grant --json` (40 s, 1 MiB of output at most) and reads
// `{url, protocols, profile_id, instance_id}`: the ticket is inside `protocols` and goes nowhere but
// the WebSocket subprotocol list — never a URL, a log line, an event or a file. A failure is
// terminal (`gateway_unavailable`: the hint is shown once, nothing is retried) or not (the client's
// backoff asks again). Each dial gets a fresh grant; dials after the first may re-ensure (start) the
// daemon at most 3 times per 60 s, otherwise they only discover it.
//
// Once the socket is open and the daemon's `gateway.ready` arrived, the renderer asks
// `runtime.describe` and only then publishes one ready event for this connection.

import { execFile } from "node:child_process";
import {
  type Clock,
  type CloseInfo,
  type ConnectHooks,
  GATEWAY_SUBPROTOCOL,
  type OutboundFrame,
  protocolsProblem,
  systemClock,
  type Transport,
  type TransportEvent,
  type Unsubscribe,
  WsTransport,
} from "@indus/client";
import { BRAND_NAME } from "@indus/ui";
import type { Env } from "../app/launchOptions.ts";
import { planRestart } from "../app/recovery.ts";
import { splitCommand } from "../composer/externalEditor.ts";

export const ATTACH_ENV = {
  local: "INDUS_TUI_LOCAL",
  url: "INDUS_TUI_GATEWAY_URL",
  grant: "INDUS_TUI_GRANT_COMMAND",
} as const;

/** The helper's exec limits (research 05 §3.30). */
export const GRANT_TIMEOUT_MS = 40_000;
export const GRANT_MAX_BYTES = 1024 * 1024;
/** Re-ensures (dials that may start the daemon) after the first: at most 3 per 60 s. */
export const RECOVER_LIMIT = 3;
export const RECOVER_WINDOW_MS = 60_000;
/**
 * From the dial until the published ready (the daemon's ready, then `runtime.describe`): past it
 * the link counts as failed and goes to the backoff (research 05 §3.30: startup 15,000 ms).
 */
export const READY_TIMEOUT_MS = 15_000;

const READY = "gateway.ready";
const DESCRIBE = "runtime.describe";
const METHOD_NOT_FOUND = -32601;

export type AttachPlan =
  | { readonly mode: "local" }
  | { readonly mode: "explicit"; readonly url: string }
  | { readonly mode: "daemon"; readonly command: string; readonly args: readonly string[] };

const TRUTHY = new Set(["1", "true", "yes", "on"]);

/** Which transport this run uses (see the module notes). */
export function chooseTransport(env: Env): AttachPlan {
  const url = env[ATTACH_ENV.url]?.trim();
  if (url) return { mode: "explicit", url };
  if (TRUTHY.has((env[ATTACH_ENV.local] ?? "").trim().toLowerCase())) return { mode: "local" };
  const words = splitCommand(env[ATTACH_ENV.grant]?.trim() ?? "");
  const [program, ...rest] = words;
  if (program === undefined) return { mode: "daemon", command: "indus", args: GRANT_ARGS };
  return {
    mode: "daemon",
    command: program,
    args: rest.includes("grant") ? rest : [...rest, ...GRANT_ARGS],
  };
}

const GRANT_ARGS: readonly string[] = ["gateway", "grant", "--json"];

// -- the grant ------------------------------------------------------------------------------------

export interface Grant {
  readonly url: string;
  readonly protocols: readonly string[];
  readonly profile_id: string;
  readonly instance_id: string;
}

export type GrantOutcome =
  | { readonly ok: true; readonly grant: Grant }
  | {
      readonly ok: false;
      readonly terminal: boolean;
      readonly code: string;
      readonly message: string;
      readonly hint: string | null;
    };

export type GrantMode = "start" | "discover" | "recover";
export type GrantRunner = (mode: GrantMode) => Promise<GrantOutcome>;

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const LOOPBACK = new Set(["127.0.0.1", "[::1]", "localhost"]);

function grantUrlOk(url: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  return (
    (parsed.protocol === "ws:" || parsed.protocol === "wss:") &&
    LOOPBACK.has(parsed.hostname) &&
    parsed.port !== "" &&
    !parsed.username &&
    !parsed.password &&
    !parsed.search &&
    !parsed.hash &&
    !url.includes("?") &&
    !url.includes("#")
  );
}

function failure(terminal: boolean, code: string, message: string, hint: string | null = null) {
  return { ok: false as const, terminal, code, message, hint };
}

/**
 * Read the helper's stdout. Never echoes stdout into a message: on success it holds the ticket.
 * `stderrFirst` is the first stderr line, shown when the helper did not even print JSON.
 */
export function parseGrant(
  stdout: string,
  exitCode: number | null,
  stderrFirst = "",
): GrantOutcome {
  let value: unknown;
  try {
    value = JSON.parse(stdout.trim());
  } catch {
    const said = stderrFirst.trim().slice(0, 200);
    return failure(
      true,
      "helper_failed",
      `the grant helper did not answer (exit ${exitCode ?? "none"})${said ? `: ${said}` : ""}`,
      "Check that `indus` runs (`indus --version`), or start with `indus --tui --local`.",
    );
  }
  if (!isObject(value)) return failure(false, "bad_grant", "the grant helper printed no object");
  if (isObject(value.error)) {
    const error = value.error;
    return failure(
      error.verdict === "gateway_unavailable",
      typeof error.code === "string" ? error.code : "failed",
      typeof error.message === "string" ? error.message : "no grant",
      typeof error.hint === "string" ? error.hint : null,
    );
  }
  const { url, protocols, profile_id, instance_id } = value;
  if (
    typeof url !== "string" ||
    !Array.isArray(protocols) ||
    !protocols.every((p) => typeof p === "string") ||
    typeof profile_id !== "string" ||
    typeof instance_id !== "string"
  ) {
    return failure(false, "bad_grant", "the grant is missing fields");
  }
  if (!grantUrlOk(url))
    return failure(false, "bad_grant", "the grant's URL is not a loopback ws URL");
  const list = protocols as string[];
  if (list.length !== 2 || protocolsProblem(list) !== null) {
    return failure(false, "bad_grant", "the grant's subprotocols are not ours");
  }
  return { ok: true, grant: { url, protocols: list, profile_id, instance_id } };
}

/** Launch options of this run: a daemon the helper may start must not inherit them. */
const LAUNCH_NAMES = new Set([
  "INDUS_MODEL",
  "INDUS_PROVIDER",
  "INDUS_YOLO_MODE",
  "INDUS_IGNORE_RULES",
  "INDUS_SAFE_MODE",
  "INDUS_IGNORE_USER_CONFIG",
  "INDUS_ACCEPT_HOOKS",
  "INDUS_CWD",
  "INDUS_LAUNCH_CWD",
  "NODE_OPTIONS",
]);

/** The helper's env: ours without the launch options of this run. */
export function helperEnv(env: Env): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [name, value] of Object.entries(env)) {
    if (value === undefined || LAUNCH_NAMES.has(name) || name.startsWith("INDUS_TUI_")) continue;
    out[name] = value;
  }
  return out;
}

type ExecFile = (
  file: string,
  args: readonly string[],
  options: {
    env: Record<string, string>;
    timeout: number;
    maxBuffer: number;
    windowsHide: boolean;
    encoding: "utf8";
  },
  done: (error: (Error & { code?: unknown }) | null, stdout: string, stderr: string) => void,
) => unknown;

export interface GrantRunnerOptions {
  command: string;
  args: readonly string[];
  env: Record<string, string>;
  timeoutMs?: number;
  maxBytes?: number;
  exec?: ExecFile;
}

/** Run the helper once per call, with its limits. */
export function grantRunner(options: GrantRunnerOptions): GrantRunner {
  const exec = options.exec ?? (execFile as unknown as ExecFile);
  return (mode) =>
    new Promise<GrantOutcome>((resolve) => {
      exec(
        options.command,
        [...options.args, "--mode", mode],
        {
          env: options.env,
          timeout: options.timeoutMs ?? GRANT_TIMEOUT_MS,
          maxBuffer: options.maxBytes ?? GRANT_MAX_BYTES,
          windowsHide: true,
          encoding: "utf8",
        },
        (error, stdout, stderr) => {
          const firstErr = (stderr ?? "").split("\n")[0] ?? "";
          if (error !== null) {
            const code = error.code;
            if (code === "ENOENT" || code === "EACCES") {
              resolve(
                failure(
                  true,
                  "helper_failed",
                  `could not run ${options.command} (${String(code)})`,
                  `Install ${BRAND_NAME} so \`indus\` is on PATH, or start with \`indus --tui --local\`.`,
                ),
              );
              return;
            }
            if (code === "ERR_CHILD_PROCESS_STDIO_MAXBUFFER") {
              resolve(failure(false, "helper_output", "the grant helper printed too much"));
              return;
            }
            if ((error as { killed?: unknown }).killed === true) {
              resolve(failure(false, "helper_timeout", "the grant helper did not finish in time"));
              return;
            }
            const exitCode = typeof code === "number" ? code : null;
            resolve(parseGrant(stdout ?? "", exitCode, firstErr));
            return;
          }
          resolve(parseGrant(stdout ?? "", 0, firstErr));
        },
      );
    });
}

/** Re-ensures allowed per sliding window (dials after the first). */
export class RecoverBudget {
  private attempts: number[] = [];
  private readonly clock: Clock;
  private readonly limit: number;
  private readonly windowMs: number;

  constructor(options: { clock?: Clock; limit?: number; windowMs?: number } = {}) {
    this.clock = options.clock ?? systemClock;
    this.limit = options.limit ?? RECOVER_LIMIT;
    this.windowMs = options.windowMs ?? RECOVER_WINDOW_MS;
  }

  /** True (and counted) when one more re-ensure fits the window. */
  take(): boolean {
    const plan = planRestart(this.attempts, this.clock.now(), this.limit, this.windowMs);
    this.attempts = plan.attempts;
    return plan.granted;
  }
}

// -- the transport --------------------------------------------------------------------------------

export type SocketOpener = (
  url: string,
  protocols: readonly string[],
  hooks: ConnectHooks,
) => Transport;

/** Dial with `WsTransport` (the host's WebSocket). */
export const openWebSocket: SocketOpener = (url, protocols, hooks) =>
  new WsTransport({ url, protocols, logs: hooks.logs, onEvent: hooks.onEvent });

export interface AttachTransportOptions {
  hooks: ConnectHooks;
  /** Where the credentials come from: the helper, or an explicit endpoint as it is. */
  source: { readonly grant: () => Promise<GrantOutcome> } | { readonly explicit: string };
  open?: SocketOpener;
  /** A local agent to run instead when the grant is terminal (startup only); null: none. */
  fallback?: (() => Transport) | null;
  /** The profile an earlier attach of this run served; another one is refused. */
  profileId?: string | null;
  clock?: Clock;
  readyTimeoutMs?: number;
  /** The connection reached ready (after describe). */
  onAttached?: (grant: { profile_id: string | null; instance_id: string | null }) => void;
  /** A terminal grant switched this run to the local agent. */
  onFallback?: (transport: Transport) => void;
}

type Listener<T> = (value: T) => void;

/**
 * One `Transport` for the channel, bound late: the grant runs first, then the WebSocket (or, on a
 * terminal grant at startup, the local child). Frames and the close of the bound link pass
 * through; the daemon's ready is held until `runtime.describe` answered.
 */
export class AttachTransport implements Transport {
  private readonly options: AttachTransportOptions;
  private readonly clock: Clock;
  private readonly frames = new Set<Listener<unknown>>();
  private readonly closes = new Set<Listener<CloseInfo>>();
  private inner: Transport | null = null;
  private unbind: Unsubscribe[] = [];
  private finished = false;
  private cancelled = false;
  private passThrough = false;
  private readyPublished = false;
  private describeId: string | null = null;
  private readyTimer: unknown;
  private heldReady: Record<string, unknown> | null = null;
  private expected: { profile_id: string | null; instance_id: string | null } = {
    profile_id: null,
    instance_id: null,
  };
  private static serial = 0;

  constructor(options: AttachTransportOptions) {
    this.options = options;
    this.clock = options.clock ?? systemClock;
    void this.begin();
  }

  /** The link in use once bound (the WebSocket or the local child). */
  get link(): Transport | null {
    return this.inner;
  }

  send(frame: OutboundFrame): void {
    const inner = this.inner;
    if (this.finished || inner === null || !(this.passThrough || this.readyPublished)) {
      throw new Error("not attached yet");
    }
    inner.send(frame);
  }

  onFrame(listener: Listener<unknown>): Unsubscribe {
    this.frames.add(listener);
    return () => this.frames.delete(listener);
  }

  onClose(listener: Listener<CloseInfo>): Unsubscribe {
    this.closes.add(listener);
    return () => this.closes.delete(listener);
  }

  async close(): Promise<void> {
    this.cancelled = true;
    const inner = this.inner;
    if (inner !== null) {
      await inner.close();
      return;
    }
    this.finish({ reason: "closed by client" });
  }

  private async begin(): Promise<void> {
    const source = this.options.source;
    if ("explicit" in source) {
      this.dial(source.explicit, [GATEWAY_SUBPROTOCOL]);
      return;
    }
    let outcome: GrantOutcome;
    try {
      outcome = await source.grant();
    } catch (error) {
      outcome = failure(false, "helper_failed", error instanceof Error ? error.message : "failed");
    }
    if (this.cancelled || this.finished) return;
    if (!outcome.ok) {
      const fallback = outcome.terminal ? (this.options.fallback ?? null) : null;
      this.emit({
        type: "transport.attach_failed",
        terminal: outcome.terminal,
        code: outcome.code,
        message: outcome.message,
        hint: outcome.hint,
        fallback: fallback !== null,
      });
      this.log(
        `[attach] no grant (${outcome.terminal ? "terminal" : "retry"}, ${outcome.code}): ${outcome.message}`,
      );
      if (fallback !== null) {
        const local = fallback();
        this.passThrough = true;
        this.bind(local);
        this.options.onFallback?.(local);
        return;
      }
      this.finish({
        reason: `attach failed: ${outcome.code}`,
        ...(outcome.terminal ? { terminal: true } : {}),
      });
      return;
    }
    const { grant } = outcome;
    if (this.options.profileId && grant.profile_id !== this.options.profileId) {
      this.emit({
        type: "transport.attach_failed",
        terminal: true,
        code: "profile_mismatch",
        message: "the daemon serves another profile than before",
        hint: "Check INDUS_HOME; restart the TUI.",
        fallback: false,
      });
      this.finish({ reason: "attach failed: profile_mismatch", terminal: true });
      return;
    }
    this.expected = { profile_id: grant.profile_id, instance_id: grant.instance_id };
    this.dial(grant.url, grant.protocols);
  }

  private dial(url: string, protocols: readonly string[]): void {
    const open = this.options.open ?? openWebSocket;
    this.readyTimer = this.clock.setTimeout(() => {
      this.readyTimer = undefined;
      if (this.readyPublished || this.finished) return;
      this.abandon({
        reason:
          this.describeId === null
            ? "the daemon sent no ready in time"
            : "runtime.describe did not answer",
      });
    }, this.options.readyTimeoutMs ?? READY_TIMEOUT_MS);
    this.bind(open(url, protocols, this.options.hooks));
  }

  private bind(inner: Transport): void {
    this.inner = inner;
    this.unbind = [
      inner.onFrame((frame) => this.onInnerFrame(frame)),
      inner.onClose((info) => this.finish(info)),
    ];
    if (this.cancelled) void inner.close();
  }

  private onInnerFrame(frame: unknown): void {
    if (this.finished) return;
    if (this.passThrough) {
      this.deliver(frame);
      return;
    }
    if (isObject(frame) && frame.id !== undefined && frame.id === this.describeId) {
      this.onDescribed(frame);
      return;
    }
    if (isObject(frame) && frame.method === READY && frame.id === undefined) {
      this.onDaemonReady(isObject(frame.params) ? frame.params : {});
      return;
    }
    this.deliver(frame);
  }

  private onDaemonReady(params: Record<string, unknown>): void {
    if (this.readyPublished || this.describeId !== null) return;
    const profile = typeof params.profile_id === "string" ? params.profile_id : null;
    const instance = typeof params.instance_id === "string" ? params.instance_id : null;
    if (this.expected.profile_id !== null && profile !== this.expected.profile_id) {
      this.emit({
        type: "transport.attach_failed",
        terminal: true,
        code: "profile_mismatch",
        message: "the daemon that answered serves another profile",
        hint: "Check INDUS_HOME; restart the TUI.",
        fallback: false,
      });
      this.abandon({ reason: "attach failed: profile_mismatch", terminal: true });
      return;
    }
    if (this.expected.instance_id !== null && instance !== this.expected.instance_id) {
      this.abandon({ reason: "the daemon restarted between grant and dial" });
      return;
    }
    this.heldReady = params;
    AttachTransport.serial += 1;
    const id = `attach-describe-${AttachTransport.serial}`;
    this.describeId = id;
    try {
      this.inner?.send({ jsonrpc: "2.0", id, method: DESCRIBE, params: {} });
    } catch {
      this.abandon({ reason: "runtime.describe could not be sent" });
    }
  }

  private onDescribed(frame: Record<string, unknown>): void {
    this.clearReadyTimer();
    let described: Record<string, unknown> | null = null;
    if (isObject(frame.result)) {
      described = frame.result;
    } else if (!(isObject(frame.error) && frame.error.code === METHOD_NOT_FOUND)) {
      this.abandon({ reason: "runtime.describe failed" });
      return;
    }
    // `-32601`: a daemon from before runtime.describe; its ready frame says what we need.
    const ready = this.heldReady ?? {};
    const params = {
      ...ready,
      type: READY,
      ...(described === null ? {} : { describe: described }),
    };
    this.readyPublished = true;
    const profile = typeof ready.profile_id === "string" ? ready.profile_id : null;
    const instance = typeof ready.instance_id === "string" ? ready.instance_id : null;
    this.options.onAttached?.({ profile_id: profile, instance_id: instance });
    this.deliver({ jsonrpc: "2.0", method: READY, params });
  }

  private abandon(info: CloseInfo): void {
    const inner = this.inner;
    this.finish(info);
    if (inner !== null) void inner.close();
  }

  private finish(info: CloseInfo): void {
    if (this.finished) return;
    this.finished = true;
    this.clearReadyTimer();
    for (const unsubscribe of this.unbind) unsubscribe();
    this.unbind = [];
    for (const listener of [...this.closes]) listener(info);
  }

  private deliver(frame: unknown): void {
    for (const listener of [...this.frames]) listener(frame);
  }

  private clearReadyTimer(): void {
    if (this.readyTimer !== undefined) this.clock.clearTimeout(this.readyTimer);
    this.readyTimer = undefined;
  }

  private emit(event: TransportEvent): void {
    this.options.hooks.onEvent(event);
  }

  private log(line: string): void {
    this.options.hooks.logs.push(line);
  }
}

// -- the connect factory --------------------------------------------------------------------------

export interface ConnectorOptions {
  plan: Exclude<AttachPlan, { mode: "local" }>;
  /** The helper (daemon plan); ignored for an explicit URL. */
  runGrant?: GrantRunner;
  /** The local stdio child, for `--local` after a terminal grant at startup; null: never. */
  local?: ((hooks: ConnectHooks) => Transport) | null;
  open?: SocketOpener;
  clock?: Clock;
  budget?: RecoverBudget;
  readyTimeoutMs?: number;
}

/** What the connector decided so far (for the entry and tests). */
export interface ConnectorState {
  /** A terminal grant switched this run to the local child. */
  readonly local: boolean;
  /** Some dial of this run reached ready. */
  readonly attached: boolean;
  /** The modes the helper was asked with, in order. */
  readonly grants: readonly GrantMode[];
}

/**
 * The `connect` factory for `IndusClient`: every call is one dial with a fresh grant. The first
 * asks `start`; later ones `recover` while the budget allows, else `discover`. A terminal grant
 * before any dial reached ready switches the run to the local child (when one is given); after
 * that it is reported and the link closes terminal (the app stops reconnecting).
 */
export function attachConnector(
  options: ConnectorOptions,
): ((hooks: ConnectHooks) => Transport) & { readonly state: ConnectorState } {
  const budget = options.budget ?? new RecoverBudget(options.clock ? { clock: options.clock } : {});
  const state = { local: false, attached: false, grants: [] as GrantMode[] };
  let profileId: string | null = null;
  let dials = 0;
  const connect = (hooks: ConnectHooks): Transport => {
    if (state.local && options.local) return options.local(hooks);
    dials += 1;
    const plan = options.plan;
    const common = {
      hooks,
      ...(options.open ? { open: options.open } : {}),
      ...(options.clock ? { clock: options.clock } : {}),
      ...(options.readyTimeoutMs === undefined ? {} : { readyTimeoutMs: options.readyTimeoutMs }),
      profileId,
      onAttached: (seen: { profile_id: string | null }) => {
        state.attached = true;
        profileId = seen.profile_id ?? profileId;
      },
    };
    if (plan.mode === "explicit") {
      return new AttachTransport({ ...common, source: { explicit: plan.url }, fallback: null });
    }
    const mode: GrantMode = dials === 1 ? "start" : budget.take() ? "recover" : "discover";
    const run = options.runGrant;
    const local = options.local ?? null;
    return new AttachTransport({
      ...common,
      source: {
        grant: () => {
          state.grants.push(mode);
          return run ? run(mode) : Promise.resolve(failure(true, "no_helper", "no grant helper"));
        },
      },
      fallback: !state.attached && local !== null ? () => local(hooks) : null,
      onFallback: () => {
        state.local = true;
      },
    });
  };
  return Object.assign(connect, { state: state as ConnectorState });
}

/** Lines a person reads when attaching failed (the app shows them once). */
export function attachFailureLines(event: {
  terminal: boolean;
  code: string;
  message: string;
  hint: string | null;
  fallback: boolean;
}): string {
  const head = event.terminal
    ? `error: cannot attach to the agent daemon (${event.code}): ${event.message}`
    : `could not attach to the agent daemon yet (${event.code}): ${event.message}; retrying`;
  const tail = event.fallback
    ? "Running a local agent for this window instead (as with `indus --tui --local`)."
    : (event.hint ?? "");
  return tail ? `${head}\n${tail}` : head;
}
