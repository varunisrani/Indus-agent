// One running app per user data dir (R08.3). The lock is taken before any process machinery
// exists: only the instance that holds it may start, reap or claim backends. A second launch is
// inert — Electron hands its argv (deep links included) to the holder as `second-instance`, and
// the loser exits before `ready`, so none of the boot code runs in it. As a second line of
// defence every machinery entry point is wrapped by `MachineryGate.guard`, which throws in a
// loser: a refactor that let boot code run there would fail loudly instead of killing the
// holder's backend.
//
// Deep-link registration order: on macOS, registering the protocol client can make Launch
// Services relaunch the app for a pending link; doing it after the lock would make that relaunch
// a loser that flashes a Dock icon and exits. So macOS registers first, everywhere else only the
// holder registers.

export const DEEP_LINK_SCHEME = "indus";
export const DEV_DEEP_LINK_SCHEME = "indus-dev";
export const DEEP_LINK_MAX_LENGTH = 4096;

export class NotLockHolderError extends Error {
  constructor(what: string) {
    super(`${what} refused: another Indus window holds the single-instance lock`);
    this.name = "NotLockHolderError";
  }
}

export class MachineryGate {
  readonly holder: boolean;

  constructor(holder: boolean) {
    this.holder = holder;
  }

  /** Throw unless this process holds the lock. */
  assertHolder(what: string): void {
    if (!this.holder) throw new NotLockHolderError(what);
  }

  /** `fn`, refusing to run (throwing) in a process that does not hold the lock. */
  guard<A extends unknown[], R>(what: string, fn: (...args: A) => R): (...args: A) => R {
    return (...args: A): R => {
      this.assertHolder(what);
      return fn(...args);
    };
  }
}

/** The scheme this build answers: `indus` when packaged, `indus-dev` for a dev build. */
export function deepLinkScheme(dev: boolean): string {
  return dev ? DEV_DEEP_LINK_SCHEME : DEEP_LINK_SCHEME;
}

/** The last argv entry that is a link on `scheme`, or null. */
export function extractDeepLink(argv: readonly unknown[], scheme: string): string | null {
  const prefix = `${scheme}://`;
  for (let i = argv.length - 1; i >= 0; i -= 1) {
    const arg = argv[i];
    if (typeof arg !== "string") continue;
    const text = arg.trim();
    if (text.length > DEEP_LINK_MAX_LENGTH) continue;
    if (text.toLowerCase().startsWith(prefix)) return text;
  }
  return null;
}

/** The parts of Electron's `app` the lock needs. */
export interface LockApp {
  requestSingleInstanceLock(additionalData?: Record<string, unknown>): boolean;
  on(
    event: "second-instance",
    listener: (event: unknown, argv: string[], workingDirectory: string, data: unknown) => void,
  ): unknown;
  setAsDefaultProtocolClient(protocol: string, path?: string, args?: string[]): boolean;
}

/**
 * Chromium binds the lock's socket under TMPDIR (`<TMPDIR>/scoped_dirXXXXXX/SingletonSocket`);
 * a Unix socket path holds at most 107 bytes, and a longer TMPDIR aborts the launch outright.
 * Returns the TMPDIR to use while taking the lock (Linux only), or null to keep the current one.
 */
export function lockTempDir(
  env: Readonly<Record<string, string | undefined>>,
  platform: string,
): string | null {
  if (platform !== "linux") return null;
  const current = env.TMPDIR ?? "";
  if (current === "") return null;
  const socket = `${current.replace(/\/+$/, "")}/scoped_dirXXXXXX/SingletonSocket`;
  return Buffer.byteLength(socket) <= 107 ? null : "/tmp";
}

/** The parts of the file system the stale-lock check reads (injected in tests). */
export interface LockFs {
  readlink(file: string): string;
  unlink(file: string): void;
}

export interface StaleLockProbe {
  fs: LockFs;
  hostname: string;
  /** True for a live, non-zombie process. */
  isAlive(pid: number): boolean;
}

/**
 * Linux keeps the lock as a `SingletonLock` symlink to `<host>-<pid>`. A crash can leave it
 * behind pointing at a pid that is gone (or a zombie that still answers `kill(pid, 0)`), and
 * then every later launch loses. When the link names this host and a dead pid, remove it so the
 * one retry can win. Returns the dead pid, or null when the lock is not provably stale.
 */
export function clearStaleLock(lockFile: string, probe: StaleLockProbe): number | null {
  let target: string;
  try {
    target = probe.fs.readlink(lockFile);
  } catch {
    return null;
  }
  const match = /^(.*)-(\d+)$/.exec(target);
  if (match === null || match[1] !== probe.hostname) return null;
  const pid = Number(match[2]);
  if (!Number.isInteger(pid) || pid <= 0 || probe.isAlive(pid)) return null;
  try {
    probe.fs.unlink(lockFile);
  } catch {
    return null;
  }
  return pid;
}

export interface SecondLaunch {
  argv: string[];
  deepLink: string | null;
  workingDirectory: string;
}

export interface SingleInstanceOptions {
  app: LockApp;
  platform: string;
  dev: boolean;
  /** Dev builds register `process.execPath <app path>` as the handler. */
  execPath: string;
  appPath?: string;
  /** Tests and e2e runs leave the OS protocol registry alone. */
  registerProtocol: boolean;
  onSecondLaunch: (launch: SecondLaunch) => void;
  log?: (line: string) => void;
  /** Linux: `<userData>/SingletonLock` and how to judge its owner (one retry when stale). */
  staleLock?: { file: string; probe: StaleLockProbe };
}

export interface SingleInstance {
  holder: boolean;
  machinery: MachineryGate;
  scheme: string;
}

function registerProtocolClient(options: SingleInstanceOptions, scheme: string): void {
  if (!options.registerProtocol) return;
  try {
    const ok =
      options.dev && options.appPath !== undefined
        ? options.app.setAsDefaultProtocolClient(scheme, options.execPath, [options.appPath])
        : options.app.setAsDefaultProtocolClient(scheme);
    if (!ok) options.log?.(`[deep-link] the OS did not accept ${scheme}:// registration`);
  } catch {
    options.log?.(`[deep-link] ${scheme}:// registration failed`);
  }
}

/** Register the deep-link client and take the lock, in the order the platform needs. */
export function acquireSingleInstance(options: SingleInstanceOptions): SingleInstance {
  const scheme = deepLinkScheme(options.dev);
  const macFirst = options.platform === "darwin";
  if (macFirst) registerProtocolClient(options, scheme);
  const request = (): boolean => {
    try {
      return options.app.requestSingleInstanceLock({ indus: 1 });
    } catch {
      return false;
    }
  };
  let holder = request();
  if (!holder && options.staleLock !== undefined) {
    const dead = clearStaleLock(options.staleLock.file, options.staleLock.probe);
    if (dead !== null) {
      options.log?.(`[lock] removed a stale lock left by pid ${dead}; retrying once`);
      holder = request();
    }
  }
  if (holder) {
    if (!macFirst) registerProtocolClient(options, scheme);
    options.app.on("second-instance", (_event, argv, workingDirectory) => {
      const list = Array.isArray(argv) ? argv.map(String) : [];
      options.onSecondLaunch({
        argv: list,
        deepLink: extractDeepLink(list, scheme),
        workingDirectory: String(workingDirectory ?? ""),
      });
    });
  } else {
    options.log?.("[lock] another Indus instance holds the lock; handing over argv and exiting");
  }
  return { holder, machinery: new MachineryGate(holder), scheme };
}
