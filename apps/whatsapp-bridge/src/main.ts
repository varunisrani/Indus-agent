// Entry point of the WhatsApp bridge. Settings come from the environment the Python adapter builds
// (never argv, so the token never shows in a process list); lifecycle events go to stdout as one
// JSON object per line. Exit codes: 0 stopped (signal, parent gone, paired), 1 failed at run time,
// 2 bad settings (nothing was bound).

import { chmodSync, mkdirSync } from "node:fs";
import { isAbsolute } from "node:path";
import { pathToFileURL } from "node:url";
import { createBridgeServer, isLoopbackHost, listen } from "./http.ts";
import { loadProvider, ProviderMissing, type WaProvider } from "./provider.ts";
import { type LifecycleEvent, Session } from "./session.ts";

export const VERSION = "1";
const MIN_TOKEN = 32;
const PARENT_CHECK_MS = 500;

export interface BridgeConfig {
  token: string;
  host: string;
  port: number;
  authDir: string;
  profile: string;
  parentPid: number | null;
  selfChat: boolean;
  mode: "serve" | "pair";
  pairPhone: string;
  provider: string;
}

export type Env = Readonly<Record<string, string | undefined>>;

/** The settings, or the name of the first bad one. */
export function readConfig(env: Env): BridgeConfig | { error: string } {
  const token = env.INDUS_WA_TOKEN ?? "";
  if (token.length < MIN_TOKEN) return { error: "token" };
  const host = (env.INDUS_WA_HOST ?? "127.0.0.1").trim();
  if (!isLoopbackHost(host)) return { error: "host" };
  const port = Number(env.INDUS_WA_PORT ?? "");
  if (!Number.isInteger(port) || port < 0 || port > 65535) return { error: "port" };
  const authDir = env.INDUS_WA_AUTH_DIR ?? "";
  if (!isAbsolute(authDir)) return { error: "auth_dir" };
  const mode = env.INDUS_WA_MODE ?? "serve";
  if (mode !== "serve" && mode !== "pair") return { error: "mode" };
  const parent = Number(env.INDUS_WA_PARENT_PID ?? "");
  return {
    token,
    host,
    port,
    authDir,
    profile: env.INDUS_WA_PROFILE ?? "",
    parentPid: Number.isInteger(parent) && parent > 1 ? parent : null,
    selfChat: env.INDUS_WA_SELF_CHAT === "1",
    mode,
    pairPhone: (env.INDUS_WA_PAIR_PHONE ?? "").replace(/[^0-9]/g, ""),
    provider: env.INDUS_WA_PROVIDER ?? "",
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
  provider?: WaProvider;
  /** Resolves when the process should stop (signals); the parent watch is added on top. */
  stopSignal?: Promise<void>;
}

/** Run the bridge until a stop; returns the exit code. */
export async function run(env: Env, deps: RunDeps): Promise<number> {
  const emit = (event: LifecycleEvent | Record<string, unknown>): void =>
    deps.out(JSON.stringify(event));
  const config = readConfig(env);
  if ("error" in config) {
    emit({ event: "error", error: `bad_${config.error}` });
    return 2;
  }
  process.umask(0o077);
  try {
    mkdirSync(config.authDir, { recursive: true, mode: 0o700 });
    chmodSync(config.authDir, 0o700);
  } catch {
    emit({ event: "error", error: "auth_dir" });
    return 2;
  }
  let provider = deps.provider;
  if (provider === undefined) {
    try {
      provider = await loadProvider(config.provider);
    } catch (error) {
      // Fail closed: no provider ships with the bridge, so without one nothing is bound.
      emit({
        event: "error",
        error: error instanceof ProviderMissing ? "no_provider" : "provider",
      });
      return 2;
    }
  }
  const session = new Session({
    provider,
    authDir: config.authDir,
    selfChat: config.selfChat,
    mode: config.mode,
    pairPhone: config.pairPhone,
    notify: emit,
  });

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

  const server =
    config.mode === "serve"
      ? createBridgeServer(session, {
          token: config.token,
          profile: config.profile,
          version: VERSION,
        })
      : null;
  let code: number;
  try {
    if (server !== null) {
      const port = await listen(server, config.host, config.port);
      emit({ event: "listening", port });
    } else {
      void session.whenPaired().then(() => finish(0));
    }
    await session.start();
    code = await finished;
  } catch {
    emit({ event: "error", error: "listen" });
    code = 1;
  } finally {
    clearInterval(watch);
    await session.stop();
    if (server !== null) {
      server.closeAllConnections();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
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
