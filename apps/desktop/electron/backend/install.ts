// Main's backend side, wired into the IPC registry (P20-08..14): the resolver, the installer, the
// local attach and its tickets, the saved remote and its liveness, the boot orchestrator, the
// daemon supervisor, the orphan ledger, the quit guard, the GPU relaunch and the REST proxy.
// `main.ts` calls `installBackend(context, electron)` once in its install steps; everything the
// windows see goes through registered channels (D-16.18) and no credential crosses the bridge
// except the single-use WebSocket ticket.

import fs from "node:fs";
import path from "node:path";
import { ApiProxy, type ApiRequest, type ProxyTarget } from "../api/proxy.ts";
import {
  BootError,
  BootOrchestrator,
  bootHooks,
  type Connection,
  FirstRunReset,
  RemoteConnectError,
} from "../boot/orchestrator.ts";
import { ConnectionsService, installConnectionsIpc } from "../connections/install.ts";
import { DialError, mintRemoteTicket, RemoteAuth } from "../connections/remoteDial.ts";
import { SshBackends } from "../connections/ssh.ts";
import type { MainContext } from "../context.ts";
import { InstallerRunner, resolveInstallerScript } from "../installer/runner.ts";
import type { EventChannel } from "../ipc/channels.ts";
import { IpcError } from "../ipc/envelope.ts";
import type { EventTarget } from "../ipc/registry.ts";
import type { PayloadOf } from "../ipc/schemas.ts";
import { gpuCrashDecision, QuitGuard, quitPrompt, relaunchArgs } from "../quitGuard.ts";
import { redactSecrets } from "../redact.ts";
import { identify } from "./control.ts";
import { LocalAttach } from "./localAttach.ts";
import { OrphanLedger } from "./orphans.ts";
import {
  classifyStatus,
  normalizeRemoteUrl,
  RemoteStore,
  RemoteUrlError,
  remoteWsUrl,
  type SavedRemote,
} from "./remote.ts";
import { LivenessTracker, REMOTE_PROBE_TIMEOUT_MS, ResumeGate } from "./remoteLiveness.ts";
import { resolveBackend } from "./resolver.ts";
import { DaemonSupervisor, READY_PROBE_TIMEOUT_MS, withTimeout } from "./supervisor.ts";
import { DialReservations, type UpgradeDetails } from "./tickets.ts";

/** The Electron pieces this step hooks (passed by main.ts so the module stays testable). */
export interface ElectronHooks {
  app: {
    on(event: "before-quit", listener: (event: { preventDefault(): void }) => void): unknown;
    on(
      event: "child-process-gone",
      listener: (
        event: unknown,
        details: { type: string; reason: string; exitCode: number },
      ) => void,
    ): unknown;
    on(
      event: "web-contents-created",
      listener: (
        event: unknown,
        contents: { id: number; once(event: "destroyed", fn: () => void): unknown },
      ) => void,
    ): unknown;
    quit(): void;
    relaunch(options: { args: string[] }): void;
    exit(code: number): void;
    getAppPath(): string;
  };
  webRequest: {
    onBeforeSendHeaders(
      filter: { urls: string[] },
      listener: (
        details: UpgradeDetails,
        callback: (response: { requestHeaders?: Record<string, string> }) => void,
      ) => void,
    ): void;
  };
  powerMonitor: { on(event: "resume", listener: () => void): unknown };
  showMessageBox(options: {
    type: "info" | "warning";
    message: string;
    detail: string;
    buttons: string[];
    defaultId: number;
    cancelId: number;
  }): Promise<{ response: number }>;
  argv: readonly string[];
  execPath: string;
  homeDir: string;
  isWsl: boolean;
  /** P20-37's update gate (`updates/install.ts` → `gateHooks`); absent: no update ever waits. */
  updateGate?: { waitForUpdate(): Promise<void>; updateRunning(): boolean };
}

const GPU_MARKER = "gpu-relaunch.json";
const DAEMON_STATUS_FILE = "gateway_state.json";
const DAEMON_STDIO_LOG = path.join("logs", "gateway-stdio.log");

function tailLines(file: string, count: number): string[] {
  try {
    const stat = fs.statSync(file);
    const start = Math.max(0, stat.size - 16 * 1024);
    const fd = fs.openSync(file, "r");
    try {
      const buffer = Buffer.alloc(stat.size - start);
      fs.readSync(fd, buffer, 0, buffer.length, start);
      return buffer
        .toString("utf8")
        .split(/\r?\n/)
        .filter((line) => line.trim() !== "")
        .slice(-count)
        .map((line) => redactSecrets(line).slice(0, 500));
    } finally {
      fs.closeSync(fd);
    }
  } catch {
    return [];
  }
}

function exitCodeOf(home: string): number | null {
  try {
    const status = JSON.parse(fs.readFileSync(path.join(home, DAEMON_STATUS_FILE), "utf8")) as {
      exit_code?: unknown;
    };
    return typeof status.exit_code === "number" && Number.isInteger(status.exit_code)
      ? status.exit_code
      : null;
  } catch {
    return null;
  }
}

function asIpcError(error: unknown): IpcError {
  if (error instanceof IpcError) return error;
  if (error instanceof FirstRunReset)
    return new IpcError("boot-reset", "first-run setup was reset");
  if (error instanceof BootError) return new IpcError(`boot-${error.kind}`, error.message);
  return new IpcError(
    "boot-local",
    error instanceof Error ? redactSecrets(error.message) : String(error),
  );
}

export interface BackendHandle {
  connections: ConnectionsService;
  orchestrator: BootOrchestrator;
  quitGuard: QuitGuard;
  installer: InstallerRunner;
  proxy: ApiProxy;
}

export interface BackendOptions {
  /** The profile a request that names none runs (P20-26's launch decision). */
  launchProfile?: () => string | null;
  /** The connections registry (P24-29); built here when the caller passes none. */
  connections?: ConnectionsService;
}

export function installBackend(
  context: MainContext,
  electron: ElectronHooks,
  options: BackendOptions = {},
): BackendHandle {
  const { registry, windows, log, paths } = context;
  const line = (text: string) => log.write(text);
  const broadcast = <N extends EventChannel>(name: N, payload: PayloadOf<N>) => {
    for (const win of windows.windows())
      registry.emit(win.webContents as EventTarget, name, payload);
  };

  const reservations = new DialReservations();
  const liveness = new LivenessTracker();
  const timers = {
    setTimeout: (fn: () => void, ms: number) => setTimeout(fn, ms),
    clearTimeout: (handle: unknown) => clearTimeout(handle as ReturnType<typeof setTimeout>),
  };
  const resumeGate = new ResumeGate(timers);
  const quitGuard = new QuitGuard();
  const ledger = new OrphanLedger({
    userData: paths.userData,
    guard: (what, fn) => context.machinery.guard(what, fn),
    log: line,
  });
  const sshBackends = new SshBackends({ controlDir: path.join(paths.userData, "ssh"), log: line }); // P24-30
  const connections =
    options.connections ??
    new ConnectionsService({
      userData: paths.userData,
      secrets: context.secrets,
      log: line,
      ssh: sshBackends,
    });
  const remote = new RemoteStore({
    userData: paths.userData,
    secrets: context.secrets,
    log: line,
    registry: connections,
  });

  const installer = new InstallerRunner({
    home: paths.home,
    appVersion: context.appVersion,
    env: process.env,
    fetchScript: () =>
      resolveInstallerScript({
        env: process.env,
        home: paths.home,
        appVersion: context.appVersion,
        platform: context.platform,
      }),
    emit: (event) => broadcast("indus:installer:event", event),
    log: line,
    claim: (pid) => {
      ledger.claim(pid, "installer-host");
    },
    release: (pid) => ledger.release(pid),
  });

  let current: { attach: LocalAttach | null; connection: Connection } | null = null;
  const supervisor = new DaemonSupervisor({
    alive: async () => {
      const attach = current?.attach;
      if (attach === null || attach === undefined) return true;
      return withTimeout(
        identify(attach.home()).then((answer) => answer !== null),
        READY_PROBE_TIMEOUT_MS,
        false,
        timers,
      );
    },
    respawn: async () => {
      const attach = current?.attach;
      if (attach === null || attach === undefined) return;
      const descriptor = await attach.ensureGateway();
      if (current !== null) {
        current.connection.descriptor = { ...current.connection.descriptor, ...descriptor };
        broadcast("indus:connection:changed", current.connection.descriptor);
      }
    },
    describeExit: async () => {
      const home = current?.attach?.home() ?? paths.home;
      return {
        exitCode: exitCodeOf(home),
        recentOutput: tailLines(path.join(home, DAEMON_STDIO_LOG), 20),
      };
    },
    notify: (notice) => broadcast("indus:backend:notice", notice),
    timers,
    now: Date.now,
    log: line,
  });

  const devCheckoutRoot = context.packaged
    ? null
    : path.resolve(electron.app.getAppPath(), "..", "..");

  const attachLocal = async (
    resolution: Parameters<ConstructorParameters<typeof BootOrchestrator>[0]["attachLocal"]>[0],
    profile: string | null,
  ): Promise<Connection> => {
    const attach = context.machinery.guard(
      "attaching to the local daemon",
      () =>
        new LocalAttach({
          backend: resolution.backend,
          version: resolution.version,
          profile,
          home: paths.home,
          env: process.env,
          log: line,
        }),
    )();
    const gateway = await attach.attachGateway();
    const serve = await attach.ensureServe();
    context.csp.addConnectOrigin(new URL(gateway.wsUrl).origin);
    const connection: Connection = {
      descriptor: { mode: "local", profile, ...gateway },
      rest: { origin: serve.url, token: attach.sessionToken },
      ticket: async (webContentsId) => {
        const minted = await attach.mint();
        if (!minted.ok)
          throw new IpcError("ticket-failed", `the daemon gave no ticket (${minted.error})`);
        if (
          minted.wsUrl !== connection.descriptor.wsUrl ||
          minted.instanceId !== connection.descriptor.instanceId
        ) {
          connection.descriptor = {
            ...connection.descriptor,
            wsUrl: minted.wsUrl,
            instanceId: minted.instanceId,
          };
          context.csp.addConnectOrigin(new URL(minted.wsUrl).origin);
          broadcast("indus:connection:changed", connection.descriptor);
        }
        return reservations.reserveLocal(minted.wsUrl, minted.ticket, webContentsId);
      },
    };
    current = { attach, connection };
    supervisor.watch(attach.startedByApp);
    return connection;
  };

  const connectRemote = async (saved: SavedRemote, profile: string | null): Promise<Connection> => {
    // P24-29: the registry entry's credential (token, OAuth, basic + access-proxy headers); an SSH
    // entry first opens its tunnel and remote serve (P24-30) and dials what that answers.
    let base = saved.url;
    let auth = saved.auth ?? new RemoteAuth(saved.url, { mode: "token", token: saved.token });
    if (saved.prepare !== undefined) {
      try {
        ({ url: base, auth } = await saved.prepare());
      } catch (error) {
        throw new RemoteConnectError(
          error instanceof Error ? redactSecrets(error.message) : "the SSH connection failed",
        );
      }
    }
    let status: Response;
    try {
      status = await fetch(`${base}/api/status`, {
        headers: auth.proxyHeaders(`${base}/api/status`),
        signal: AbortSignal.timeout(REMOTE_PROBE_TIMEOUT_MS),
      });
    } catch {
      throw new RemoteConnectError("the remote Indus did not answer");
    }
    const info = (await status.json().catch(() => ({}))) as Record<string, unknown>;
    let check: Response;
    try {
      check = await auth.request("/api/config");
      await check.body?.cancel();
    } catch (error) {
      if (error instanceof DialError && error.kind === "unauthorized") {
        throw new RemoteConnectError(error.message, {
          reauth: true,
          statusCode: error.status ?? 401,
        });
      }
      throw new RemoteConnectError("the remote Indus did not answer");
    }
    const leg = classifyStatus(check.status);
    if (leg === "unauthorized") {
      throw new RemoteConnectError("the remote Indus refused the saved token", {
        reauth: true,
        statusCode: check.status,
      });
    }
    if (leg !== "ok") {
      throw new RemoteConnectError(`the remote Indus answered ${check.status}`, {
        statusCode: check.status,
      });
    }
    const wsUrl = remoteWsUrl(base);
    context.csp.addConnectOrigin(new URL(wsUrl).origin);
    if (saved.connectionId !== undefined) connections.markUsed(saved.connectionId);
    const connection: Connection = {
      descriptor: {
        mode: "remote",
        profile,
        profileId: typeof info.profile_id === "string" ? info.profile_id : "",
        instanceId: typeof info.instance_id === "string" ? info.instance_id : "",
        codeVersion: typeof info.version === "string" ? info.version : "",
        wsUrl,
      },
      rest: {
        origin: base,
        token: saved.token,
        headers: async (url) => ({ ...auth.proxyHeaders(url), ...(await auth.authHeaders()) }),
      },
      // one server-minted single-use ticket per dial (D-16.691: Electron ignores a rewritten
      // subprotocol, so the token itself can never reach the upgrade)
      ticket: async (webContentsId) => {
        try {
          const minted = await mintRemoteTicket(auth, { profile });
          return reservations.reserveTicketDial(wsUrl, minted.ticket, webContentsId);
        } catch (error) {
          if (error instanceof DialError && error.kind === "unauthorized") {
            throw new IpcError("unauthorized", error.message);
          }
          throw new IpcError(
            "ticket-failed",
            error instanceof Error ? redactSecrets(error.message) : "the server gave no ticket",
          );
        }
      },
    };
    supervisor.stop();
    current = { attach: null, connection };
    return connection;
  };

  const orchestrator = new BootOrchestrator({
    resolve: async ({ repair }) => {
      await context.shellPath; // R08.16: PATH merged before anything is resolved or spawned
      return resolveBackend({
        env: process.env,
        argv: electron.argv,
        platform: context.platform,
        packaged: context.packaged,
        home: paths.home,
        homeDir: electron.homeDir,
        devCheckoutRoot,
        appExecPath: electron.execPath,
        isWsl: electron.isWsl,
        repair,
        log: line,
      });
    },
    install: () => context.machinery.guard("running the installer", () => installer.run())(),
    attachLocal,
    savedRemote: () => remote.saved(),
    connectRemote,
    waitForUpdate: async () => {
      await electron.updateGate?.waitForUpdate();
    },
    updateRunning: () => electron.updateGate?.updateRunning() ?? false,
    broadcast: (progress) => broadcast("indus:boot:progress", progress),
    onConnection: (connection) => {
      if (connection === null) {
        current = null;
        supervisor.stop();
      }
      broadcast("indus:connection:changed", connection?.descriptor ?? null);
    },
    sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    hooks: bootHooks(process.env),
    log: line,
  });

  const target = (): ProxyTarget | null => {
    const connection = orchestrator.current();
    return connection === null
      ? null
      : {
          origin: connection.rest.origin,
          token: connection.rest.token,
          profile: connection.descriptor.profile,
          ...(connection.rest.headers === undefined ? {} : { headers: connection.rest.headers }),
        };
  };
  const proxy = new ApiProxy({ target, log: line });
  const request = async (payload: ApiRequest) => {
    const connection = orchestrator.current();
    try {
      const result = await proxy.request(payload);
      if (connection?.descriptor.mode === "remote") liveness.recordSuccess(connection.rest.origin);
      return result;
    } catch (error) {
      if (
        connection?.descriptor.mode === "remote" &&
        error instanceof IpcError &&
        (error.code === "unreachable" || error.code === "timeout") &&
        liveness.recordFailure(connection.rest.origin).dead
      ) {
        line("[remote] the remote route failed three times within a minute; reconnecting");
        orchestrator.reconnect();
      }
      throw error;
    }
  };

  // -- channels ----------------------------------------------------------------------------------

  registry.handle("indus:boot:get", () => orchestrator.progress());
  registry.handle("indus:boot:retry", ({ action }) => orchestrator.retry(action));
  registry.handle("indus:boot:choose", ({ choice }) => {
    const mapped =
      choice === "install-local"
        ? "install-local"
        : choice === "connect-remote"
          ? "connect-remote"
          : "reset";
    return orchestrator.choose(mapped);
  });
  registry.handle("indus:installer:cancel", () => ({ accepted: installer.cancel() }));

  registry.handle("indus:connection:get", async ({ profile }) => {
    try {
      return (await orchestrator.start(profile ?? options.launchProfile?.() ?? null)).descriptor;
    } catch (error) {
      throw asIpcError(error);
    }
  });
  registry.handle("indus:connection:ticket", async ({ profile }, { senderId }) => {
    const connection = orchestrator.current();
    if (
      connection === null ||
      (profile !== undefined && connection.descriptor.profile !== profile)
    ) {
      throw new IpcError("not-connected", "Indus is not connected for that profile");
    }
    return connection.ticket(senderId);
  });

  const remoteInput = (input: { url: string; token: string }) => {
    try {
      return { url: normalizeRemoteUrl(input.url), token: input.token };
    } catch (error) {
      throw new IpcError(
        "bad-request",
        error instanceof RemoteUrlError ? error.message : "that is not a web address",
      );
    }
  };
  // The first-run chooser's single remote (P20-11) is the registry's primary since P24-29.
  registry.handle("indus:remote:get", () => connections.primaryRemote());
  registry.handle("indus:remote:test", (input) => remote.test(remoteInput(input)));
  registry.handle("indus:remote:save", async (input) => {
    const checked = remoteInput(input);
    const result = await remote.save(checked);
    if (result.saved) {
      connections.adoptRemote(checked.url, checked.token);
      orchestrator.applyConnectionChange();
    }
    return result;
  });
  registry.handle("indus:remote:forget", () => {
    remote.forget();
    connections.forgetPrimaryRemote();
    orchestrator.applyConnectionChange();
    return { ok: true as const };
  });

  registry.handle("indus:api", (payload) => request(payload));
  registry.on("indus:work:active", ({ items }, { senderId }) => quitGuard.report(senderId, items));

  // -- Electron hooks ----------------------------------------------------------------------------

  electron.webRequest.onBeforeSendHeaders(
    { urls: ["ws://*/*", "wss://*/*"] },
    (details, callback) => {
      const headers = reservations.rewrite(details);
      callback(headers === null ? {} : { requestHeaders: headers });
    },
  );

  electron.app.on("web-contents-created", (_event, contents) => {
    contents.once("destroyed", () => quitGuard.forget(contents.id));
  });

  let quitConfirmed = process.env.INDUS_DESKTOP_SKIP_QUIT_PROMPT === "1";
  electron.app.on("before-quit", (event) => {
    if (quitConfirmed) {
      void sshBackends.closeAll(); // our tunnels end with the app; the master persists its 300 s
      return;
    }
    const owned = installer.running() ? [{ id: "installer", label: "Installing Indus" }] : [];
    const prompt = quitPrompt(quitGuard.work(), owned);
    if (prompt === null) {
      void sshBackends.closeAll();
      return;
    }
    event.preventDefault();
    void electron
      .showMessageBox({
        type: prompt.informational ? "info" : "warning",
        message: prompt.message,
        detail: prompt.detail,
        buttons: prompt.buttons,
        defaultId: 0,
        cancelId: 0,
      })
      .then(({ response }) => {
        if (response !== 1) return;
        quitConfirmed = true;
        if (installer.running()) installer.cancel();
        electron.app.quit();
      });
  });

  electron.app.on("child-process-gone", (_event, details) => {
    const markerFile = path.join(paths.userData, GPU_MARKER);
    let marker: { version: string } | null = null;
    try {
      marker = JSON.parse(fs.readFileSync(markerFile, "utf8")) as { version: string };
    } catch {
      marker = null;
    }
    if (
      gpuCrashDecision(details, context.appVersion, marker, electron.argv) !==
      "relaunch-without-gpu"
    )
      return;
    line(
      `[gpu] the GPU process crashed (${details.reason}, ${details.exitCode}); relaunching once without the GPU`,
    );
    try {
      fs.mkdirSync(paths.userData, { recursive: true });
      fs.writeFileSync(
        markerFile,
        `${JSON.stringify({ version: context.appVersion, at: Date.now() })}\n`,
      );
    } catch {
      return; // without the marker the relaunch would not be bounded
    }
    electron.app.relaunch({ args: relaunchArgs(electron.argv) });
    electron.app.exit(0);
  });

  electron.powerMonitor.on("resume", () => {
    resumeGate.noteResume();
    for (const win of windows.windows()) {
      void resumeGate.request(String(win.webContents.id), async () => {
        const connection = orchestrator.current();
        if (connection?.descriptor.mode !== "remote") return;
        try {
          const response = await fetch(`${connection.rest.origin}/api/status`, {
            signal: AbortSignal.timeout(REMOTE_PROBE_TIMEOUT_MS),
          });
          await response.body?.cancel();
          liveness.recordSuccess(connection.rest.origin);
        } catch {
          if (liveness.recordFailure(connection.rest.origin).dead) orchestrator.reconnect();
        }
      });
    }
  });

  // reap what a crashed earlier run left behind (lock holder only)
  void ledger.reapOrphans().then(
    ({ killed }) => {
      if (killed.length > 0) line(`[orphans] reaped ${killed.length} leftover process(es)`);
    },
    (error: unknown) =>
      line(`[orphans] not reaped: ${error instanceof Error ? error.message : String(error)}`),
  );

  installConnectionsIpc(registry, connections, {
    onChange: () => orchestrator.applyConnectionChange(),
    openExternal: (url, contentsId) => context.externalOpener.open({ url, contentsId }),
  });

  return { connections, orchestrator, quitGuard, installer, proxy };
}
