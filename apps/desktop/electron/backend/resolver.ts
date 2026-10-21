// Which `indus` the desktop runs (P20-08). One ladder, top rung first; a candidate counts only
// after `indus --version` answers within 5 s. PATH is never a rung and a system Python is never
// adopted. Nothing usable is a value (`bootstrap-needed`), not an error: the boot orchestrator
// offers the installer then. No bundled-payload rung in 1.0 (D-16.82).
//
//   1  INDUS_DESKTOP_INDUS_ROOT   a developer checkout (its own venv)
//   2  the app's own checkout     unpackaged runs only
//   3  INDUS_DESKTOP_INDUS        an explicit command (packagers); not this app, not C:\ under WSL
//   4  <home>/indus-agent         the managed install; skipped by ignore-existing and repair
//   5  ~/.local/bin/indus         the launcher an install published (POSIX); skipped like 4
//   6  bootstrap-needed

import fs from "node:fs";
import path from "node:path";
import { type CommandRunner, type CommandSpec, runCommand } from "./process.ts";

export const VERSION_PROBE_TIMEOUT_MS = 5_000;
export const MANAGED_INSTALL_DIR = "indus-agent";
export const IGNORE_EXISTING_FLAG = "--ignore-existing";
const LAUNCHER_READ_BYTES = 4 * 1024;

export type Rung = "source-root" | "dev-checkout" | "command" | "managed" | "launcher";

export interface IndusCommand extends CommandSpec {
  readonly label: string;
  readonly rung: Rung;
  /** The install or checkout behind the command, when known. */
  readonly root: string | null;
}

export interface BootstrapHints {
  installDir: string;
  platform: string;
  packaged: boolean;
}

export type Resolution =
  | { kind: "found"; backend: IndusCommand; version: string }
  | { kind: "bootstrap-needed"; hints: BootstrapHints };

export interface ResolverFs {
  isFile(file: string): boolean;
  isDir(dir: string): boolean;
  isSymlink(file: string): boolean;
  realpath(file: string): string | null;
  readHead(file: string, bytes: number): string | null;
}

export const nodeResolverFs: ResolverFs = {
  isFile: (file) => {
    try {
      return fs.statSync(file).isFile();
    } catch {
      return false;
    }
  },
  isDir: (dir) => {
    try {
      return fs.statSync(dir).isDirectory();
    } catch {
      return false;
    }
  },
  isSymlink: (file) => {
    try {
      return fs.lstatSync(file).isSymbolicLink();
    } catch {
      return false;
    }
  },
  realpath: (file) => {
    try {
      return fs.realpathSync(file);
    } catch {
      return null;
    }
  },
  readHead: (file, bytes) => {
    let fd: number | null = null;
    try {
      fd = fs.openSync(file, "r");
      const buffer = Buffer.alloc(bytes);
      const read = fs.readSync(fd, buffer, 0, bytes, 0);
      return buffer.subarray(0, read).toString("utf8");
    } catch {
      return null;
    } finally {
      if (fd !== null) fs.closeSync(fd);
    }
  },
};

export interface ResolverOptions {
  env: Readonly<Record<string, string | undefined>>;
  argv: readonly string[];
  platform: string;
  packaged: boolean;
  /** The Indus home (`~/.indus` or INDUS_HOME). */
  home: string;
  /** The user's home directory (for `~/.local/bin`). */
  homeDir: string;
  /** The repository the unpackaged app runs from, or null. */
  devCheckoutRoot: string | null;
  /** This app's executable, which rung 3 must never pick. */
  appExecPath: string;
  isWsl: boolean;
  /** A repair was requested: skip the installed runtimes so the installer runs again. */
  repair?: boolean;
  /** `indus --version` → the version, or null. Default: run it with a 5 s deadline. */
  probe?: (command: CommandSpec) => Promise<string | null>;
  fs?: ResolverFs;
  log?: (line: string) => void;
}

/** `Indus Agent 0.20.0` or `indus 0.20.0` (first line) → `0.20.0`. */
export function parseVersionOutput(stdout: string): string | null {
  const first = stdout.split(/\r?\n/, 1)[0]?.trim() ?? "";
  const match = /^indus(?:\s+agent)?\s+v?(\d[0-9A-Za-z.+-]*)$/i.exec(first);
  return match?.[1] ?? null;
}

export function versionProbe(
  run: CommandRunner = runCommand,
  env?: Record<string, string | undefined>,
): (command: CommandSpec) => Promise<string | null> {
  return async (command) => {
    const result = await run(command, ["--version"], {
      timeoutMs: VERSION_PROBE_TIMEOUT_MS,
      ...(env === undefined ? {} : { env }),
    });
    if (result.timedOut || result.code !== 0) return null;
    return parseVersionOutput(result.stdout);
  };
}

/** A checkout of this project: `pyproject.toml` beside `src/indus/__init__.py`. */
export function isSourceRoot(root: string, files: ResolverFs = nodeResolverFs): boolean {
  return (
    files.isFile(path.join(root, "pyproject.toml")) &&
    files.isFile(path.join(root, "src", "indus", "__init__.py"))
  );
}

/**
 * The command a venv under `root` runs `indus` with. On Windows the venv's `indus.exe` is a
 * redirector the packaged interpreter refuses, so the venv's `python.exe -m indus` is used.
 */
export function venvCommand(
  root: string,
  platform: string,
  files: ResolverFs = nodeResolverFs,
): CommandSpec | null {
  const venv = path.join(root, ".venv");
  if (platform === "win32") {
    const python = path.win32.join(venv, "Scripts", "python.exe");
    return files.isFile(python) ? { command: python, args: ["-m", "indus"] } : null;
  }
  const indus = path.join(venv, "bin", "indus");
  return files.isFile(indus) ? { command: indus, args: [] } : null;
}

/** On Windows, `<venv>\Scripts\indus.exe` → `<venv>\Scripts\python.exe -m indus`. */
export function unwrapWindowsVenvLauncher(
  command: string,
  files: ResolverFs = nodeResolverFs,
): CommandSpec | null {
  const parsed = path.win32.parse(command);
  if (parsed.name.toLowerCase() !== "indus" || path.win32.basename(parsed.dir) !== "Scripts") {
    return null;
  }
  const venv = path.win32.dirname(parsed.dir);
  const python = path.win32.join(parsed.dir, "python.exe");
  if (!files.isFile(path.win32.join(venv, "pyvenv.cfg")) || !files.isFile(python)) return null;
  return { command: python, args: ["-m", "indus"] };
}

function looksLikeWindowsPath(value: string): boolean {
  return /^[A-Za-z]:[\\/]/.test(value) || value.startsWith("\\\\") || /\.exe$/i.test(value);
}

function findOnPath(
  name: string,
  env: Readonly<Record<string, string | undefined>>,
  platform: string,
  files: ResolverFs,
): string | null {
  const delimiter = platform === "win32" ? ";" : ":";
  const exts = platform === "win32" ? ["", ".exe", ".cmd", ".bat"] : [""];
  for (const dir of (env.PATH ?? env.Path ?? "").split(delimiter)) {
    if (dir === "") continue;
    for (const ext of exts) {
      const candidate = path.join(dir, `${name}${ext}`);
      if (files.isFile(candidate)) return candidate;
    }
  }
  return null;
}

/**
 * The program a published launcher runs: a symlink's target, or the first absolute path a small
 * launcher script execs (`exec "/x/.venv/bin/indus" "$@"`).
 */
export function launcherTarget(
  launcher: string,
  files: ResolverFs = nodeResolverFs,
): string | null {
  if (files.isSymlink(launcher)) {
    const target = files.realpath(launcher);
    return target !== null && files.isFile(target) ? target : null;
  }
  if (!files.isFile(launcher)) return null;
  const head = files.readHead(launcher, LAUNCHER_READ_BYTES);
  if (head === null) return null;
  for (const line of head.split(/\r?\n/)) {
    const match = /^\s*exec\s+["']?(\/[^"'\s]+)["']?/.exec(line);
    if (match?.[1] !== undefined && files.isFile(match[1])) return match[1];
  }
  return null;
}

function rootOfVenvProgram(program: string): string | null {
  const parts = program.split(path.sep);
  const at = parts.lastIndexOf(".venv");
  return at > 0 ? parts.slice(0, at).join(path.sep) : null;
}

export async function resolveBackend(options: ResolverOptions): Promise<Resolution> {
  const files = options.fs ?? nodeResolverFs;
  const probe = options.probe ?? versionProbe(runCommand, { ...options.env });
  const log = options.log ?? (() => {});
  const { env, platform } = options;

  const accept = async (
    rung: Rung,
    spec: CommandSpec,
    label: string,
    root: string | null,
  ): Promise<Resolution | null> => {
    const version = await probe(spec);
    if (version === null) {
      log(`[resolver] ${label}: the version probe failed; skipping`);
      return null;
    }
    log(`[resolver] using ${label} (indus ${version})`);
    return { kind: "found", backend: { ...spec, label, rung, root }, version };
  };

  const fromCheckout = async (rung: Rung, root: string, what: string) => {
    if (!isSourceRoot(root, files)) {
      log(`[resolver] ${what} ${root} is not an Indus checkout; skipping`);
      return null;
    }
    const spec = venvCommand(root, platform, files);
    if (spec === null) {
      log(`[resolver] ${what} ${root} has no .venv (run \`uv sync\` there); skipping`);
      return null;
    }
    return accept(rung, spec, `${what} ${root}`, root);
  };

  // 1. explicit source root
  const overrideRoot = (env.INDUS_DESKTOP_INDUS_ROOT ?? "").trim();
  if (overrideRoot !== "") {
    if (!path.isAbsolute(overrideRoot)) {
      log("[resolver] INDUS_DESKTOP_INDUS_ROOT is not an absolute path; skipping");
    } else {
      const found = await fromCheckout("source-root", path.resolve(overrideRoot), "checkout");
      if (found !== null) return found;
    }
  }

  // 2. the checkout an unpackaged app runs from
  if (!options.packaged && options.devCheckoutRoot !== null) {
    const found = await fromCheckout("dev-checkout", options.devCheckoutRoot, "dev checkout");
    if (found !== null) return found;
  }

  // 3. explicit command
  const override = (env.INDUS_DESKTOP_INDUS ?? "").trim();
  if (override !== "") {
    const found = await fromOverride(override);
    if (found !== null) return found;
  }

  const skipInstalled =
    options.repair === true ||
    env.INDUS_DESKTOP_IGNORE_EXISTING === "1" ||
    options.argv.includes(IGNORE_EXISTING_FLAG);
  const installDir = path.join(options.home, MANAGED_INSTALL_DIR);

  if (skipInstalled) {
    log(
      `[resolver] ${options.repair === true ? "repair requested" : "ignore-existing"}: skipping installed runtimes`,
    );
  } else {
    // 4. the managed install (usable without the bootstrap marker)
    const managed = venvCommand(installDir, platform, files);
    if (managed !== null) {
      const found = await accept("managed", managed, `managed install ${installDir}`, installDir);
      if (found !== null) return found;
    }
    // 5. an install found through its published launcher
    if (platform !== "win32") {
      const launcher = path.join(options.homeDir, ".local", "bin", "indus");
      const target = launcherTarget(launcher, files);
      if (target !== null) {
        const found = await accept(
          "launcher",
          { command: target, args: [] },
          `install behind ${launcher}`,
          rootOfVenvProgram(target),
        );
        if (found !== null) return found;
      }
    }
  }

  // 6. nothing usable
  log("[resolver] no usable Indus runtime; bootstrap needed");
  return {
    kind: "bootstrap-needed",
    hints: { installDir, platform, packaged: options.packaged },
  };

  async function fromOverride(value: string): Promise<Resolution | null> {
    if (options.isWsl && looksLikeWindowsPath(value)) {
      log("[resolver] INDUS_DESKTOP_INDUS names a Windows program under WSL; skipping");
      return null;
    }
    const program =
      path.isAbsolute(value) || value.includes("/") || value.includes("\\")
        ? value
        : findOnPath(value, env, platform, files);
    if (program === null || !files.isFile(program)) {
      log("[resolver] INDUS_DESKTOP_INDUS does not name a program; skipping");
      return null;
    }
    const real = files.realpath(program) ?? program;
    const self = files.realpath(options.appExecPath) ?? options.appExecPath;
    if (real === self) {
      log("[resolver] INDUS_DESKTOP_INDUS names this desktop app; skipping");
      return null;
    }
    const spec =
      (platform === "win32" ? unwrapWindowsVenvLauncher(program, files) : null) ??
      ({ command: program, args: [] } satisfies CommandSpec);
    return accept("command", spec, `command ${program}`, rootOfVenvProgram(real));
  }
}
