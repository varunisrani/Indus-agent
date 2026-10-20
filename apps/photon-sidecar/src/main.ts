// Entry point of the Photon sidecar. Settings come from the environment the Python adapter builds
// (never argv, so neither the token nor the project secret shows in a process list); lifecycle
// events go to stdout as one JSON object per line, never with message text, a number or a secret.
// Exit codes: 0 stopped (signal, parent gone), 1 failed at run time, 2 bad settings (nothing bound).

import { pathToFileURL } from "node:url";
import { createSidecarServer, isLoopbackHost, listen } from "./http.ts";
import {
  LinkError,
  type LinkProvider,
  type LinkState,
  loadProvider,
  ProviderMissing,
  type SpectrumLink,
} from "./link.ts";
import { Relay } from "./relay.ts";

export const VERSION = "1";
export const DEFAULT_PORT = 8789;
const MIN_TOKEN = 32;
const PARENT_CHECK_MS = 500;
const RETRY_FIRST_MS = 1000;
const RETRY_MAX_MS = 30_000;

export interface SidecarConfig {
  token: string;
  host: string;
  port: number;
  profile: string;
  parentPid: number | null;
  provider: string;
  projectId: string;
  projectSecret: string;
}

export type Env = Readonly<Record<string, string | undefined>>;

/** The settings, or the name of the first bad one. */
export function readConfig(env: Env): SidecarConfig | { error: string } {
  const token = env.INDUS_PHOTON_TOKEN ?? "";
  if (token.length < MIN_TOKEN) return { error: "token" };
  const host = (env.INDUS_PHOTON_HOST ?? "127.0.0.1").trim();
  if (!isLoopbackHost(host)) return { error: "host" };
  const port = Number(env.INDUS_PHOTON_PORT ?? String(DEFAULT_PORT));
  if (!Number.isInteger(port) || port < 0 || port > 65535) return { error: "port" };
  const parent = Number(env.INDUS_PHOTON_PARENT_PID ?? "");
  return {
    token,
    host,
    port,
    profile: env.INDUS_PHOTON_PROFILE ?? "",
    parentPid: Number.isInteger(parent) && parent > 1 ? parent : null,
    provider: env.INDUS_PHOTON_PROVIDER ?? "",
    projectId: (env.PHOTON_PROJECT_ID ?? "").trim(),
    projectSecret: env.PHOTON_PROJECT_SECRET ?? "",
  };
}

export function alive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "EPERM";
  }
}

export interface RunDeps {
  out: (line: string) => void;
  provider?: LinkProvider;
  /** Resolves when the process should stop (signals); the parent watch is added on top. */
  stopSignal?: Promise<void>;
  heartbeatMs?: number;
  retryFirstMs?: number;
}

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/** Run the sidecar until a stop; returns the exit code. */
export async function run(env: Env, deps: RunDeps): Promise<number> {
  const emit = (event: Record<string, unknown>): void => deps.out(JSON.stringify(event));
  const config = readConfig(env);
  if ("error" in config) {
    emit({ event: "error", error: `bad_${config.error}` });
    return 2;
  }
  process.umask(0o077);
  let provider = deps.provider;
  if (provider === undefined) {
    try {
      provider = await loadProvider(config.provider);
    } catch (error) {
      // Fail closed: no Spectrum client ships with the sidecar, so without one nothing is bound.
      emit({
        event: "error",
        error: error instanceof ProviderMissing ? "no_provider" : "provider",
      });
      return 2;
    }
  }
  if (!config.projectId || !config.projectSecret) {
    emit({ event: "error", error: "credentials" });
    return 2;
  }

  const relay = new Relay();
  let link: SpectrumLink | null = null;
  let stopping = false;
  const setState = (state: LinkState, detail = ""): void => {
    if (stopping) return;
    // The detail is the provider's and could name anything: only its presence is reported.
    if (relay.setStatus(state, detail)) emit({ event: "link", state });
  };

  let finish: (code: number) => void = () => {};
  const finished = new Promise<number>((resolve) => {
    finish = resolve;
  });
  const startPpid = process.ppid;
  const watch = setInterval(() => {
    const gone = config.parentPid !== null && !alive(config.parentPid);
    if (gone || process.ppid !== startPpid) finish(0);
  }, PARENT_CHECK_MS);
  void deps.stopSignal?.then(() => finish(0));

  const server = createSidecarServer({
    token: config.token,
    profile: config.profile,
    relay,
    link: () => link,
    version: VERSION,
    ...(deps.heartbeatMs === undefined ? {} : { heartbeatMs: deps.heartbeatMs }),
  });

  /** Open the link, retrying 1 s doubling to 30 s; refused credentials stop the retries. */
  const connect = async (): Promise<void> => {
    let wait = deps.retryFirstMs ?? RETRY_FIRST_MS;
    while (!stopping) {
      setState("connecting");
      try {
        const opened = await (provider as LinkProvider)({
          projectId: config.projectId,
          projectSecret: config.projectSecret,
          handlers: {
            onState: (state, detail) => setState(state, detail ?? ""),
            onMessage: (message) => {
              if (!stopping) relay.accept(message);
            },
          },
        });
        if (stopping) {
          await opened.close().catch(() => {});
          return;
        }
        link = opened;
        if (relay.status === "connecting") setState("open");
        return;
      } catch (error) {
        if (error instanceof LinkError && error.code === "auth_failed") {
          setState("auth_failed");
          return;
        }
        setState("closed");
        emit({ event: "error", error: "link" });
        await sleep(wait);
        wait = Math.min(wait * 2, RETRY_MAX_MS);
      }
    }
  };

  let code: number;
  try {
    const port = await listen(server, config.host, config.port);
    emit({ event: "listening", port });
    void connect();
    code = await finished;
  } catch {
    emit({ event: "error", error: "listen" });
    code = 1;
  } finally {
    stopping = true;
    clearInterval(watch);
    const open = link as SpectrumLink | null;
    link = null;
    await open?.close().catch(() => {});
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
  return code;
}

function signalled(): Promise<void> {
  return new Promise((resolve) => {
    process.once("SIGTERM", () => resolve());
    process.once("SIGINT", () => resolve());
  });
}

const entry = process.argv[1];
if (entry !== undefined && pathToFileURL(entry).href === import.meta.url) {
  run(process.env, {
    out: (line) => process.stdout.write(`${line}\n`),
    stopSignal: signalled(),
  }).then(
    (code) => process.exit(code),
    () => process.exit(1),
  );
}
