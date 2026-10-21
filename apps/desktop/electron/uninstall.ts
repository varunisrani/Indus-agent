// Uninstall from Settings → About (P24-34). The app never deletes anything itself: it runs the
// CLI's own `indus uninstall` (P23-20, `docs/internals/uninstall-levels.md`) from a small helper
// script that starts after the app has quit.
//
//   level        what the person picked                      the helper runs
//   gui          the app only                                indus uninstall --gui --yes
//   keep-data    the app and the runtime, keep my data       indus uninstall --yes
//   full         everything                                  indus uninstall --full --yes
//
// The levels are offered only when ownership is confirmed from the install receipt (`summary`):
// the CLI's `--gui-summary` answers with JSON naming an install kind no other manager owns and a
// git checkout of ours (`agent_installed`), the installer left a `success` receipt under
// `<home>/logs/install_receipts/`, and the app does not run from a Nix store. Anything missing,
// unreadable or contradictory hides every level. "Everything" also needs the typed phrase
// `CONFIRM_PHRASE`, checked here in main, not only in the page.
//
// The helper waits (at most ~30 s) for this app's pid to exit, runs the command with `INDUS_HOME`
// set to the app's home, and deletes itself. Paths are single-quoted for POSIX shells; the Windows
// batch file refuses paths holding characters cmd cannot quote (`"`, `%`, `^`, `&`, `!`).

import path from "node:path";
import type { CommandSpec } from "./backend/process.ts";
import type { MainContext } from "./context.ts";
import { IpcError } from "./ipc/envelope.ts";

export type UninstallLevel = "gui" | "keep-data" | "full";

export const LEVELS: readonly UninstallLevel[] = ["gui", "keep-data", "full"];

/** The CLI arguments of each level, exactly the flags table. */
export const LEVEL_ARGS: Readonly<Record<UninstallLevel, readonly string[]>> = {
  gui: ["uninstall", "--gui", "--yes"],
  "keep-data": ["uninstall", "--yes"],
  full: ["uninstall", "--full", "--yes"],
};

export const SUMMARY_ARGS: readonly string[] = ["uninstall", "--gui-summary"];
export const CONFIRM_PHRASE = "uninstall everything";
export const SUMMARY_TIMEOUT_MS = 10_000;
export const HELPER_WAIT_SECONDS = 30;

/** Install kinds another manager owns (the CLI refuses code removal for them too). */
export const MANAGED_KINDS: Readonly<Record<string, string>> = {
  nix: "Nix installed this Indus. Remove it the way you added it (your flake or profile), then rebuild.",
  docker: "This Indus runs in a container. Remove the image and its volumes with Docker.",
  desktop:
    "This Indus is the desktop app's bundled runtime. Remove the app the way your system removes apps.",
};

export interface UninstallSummaryView {
  allowed: boolean;
  levels: UninstallLevel[];
  reason?: string;
  installKind: string;
  home: string;
  confirmPhrase: string;
}

export interface OwnershipInput {
  home: string;
  execPath: string;
  /** stdout of `indus uninstall --gui-summary`, or null when it could not run. */
  summaryText: string | null;
  /** The parsed installer receipts (`<home>/logs/install_receipts/*.json`). */
  receipts: readonly unknown[];
}

const NOT_CONFIRMED = "Indus could not confirm how it was installed, so it cannot remove itself.";

function hidden(input: OwnershipInput, reason: string, installKind = "unknown") {
  return {
    allowed: false,
    levels: [],
    reason,
    installKind,
    home: input.home,
    confirmPhrase: CONFIRM_PHRASE,
  } satisfies UninstallSummaryView;
}

/** Decide which levels to offer (see the module notes). */
export function decideOwnership(input: OwnershipInput): UninstallSummaryView {
  if (/[\\/]nix[\\/]store[\\/]/.test(input.execPath)) {
    return hidden(input, MANAGED_KINDS.nix ?? NOT_CONFIRMED, "nix");
  }
  if (input.summaryText === null) return hidden(input, NOT_CONFIRMED);
  let summary: Record<string, unknown>;
  try {
    const parsed: unknown = JSON.parse(input.summaryText);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return hidden(input, NOT_CONFIRMED);
    }
    summary = parsed as Record<string, unknown>;
  } catch {
    return hidden(input, NOT_CONFIRMED);
  }
  const kind = typeof summary.install_kind === "string" ? summary.install_kind : "unknown";
  const managed = MANAGED_KINDS[kind];
  if (managed !== undefined) return hidden(input, managed, kind);
  if (summary.agent_installed !== true) return hidden(input, NOT_CONFIRMED, kind);
  if (typeof summary.home === "string" && path.resolve(summary.home) !== path.resolve(input.home)) {
    return hidden(input, NOT_CONFIRMED, kind);
  }
  const receipted = input.receipts.some(
    (r) =>
      typeof r === "object" && r !== null && (r as { outcome?: unknown }).outcome === "success",
  );
  if (!receipted) return hidden(input, NOT_CONFIRMED, kind);
  return {
    allowed: true,
    levels: [...LEVELS],
    installKind: kind,
    home: input.home,
    confirmPhrase: CONFIRM_PHRASE,
  };
}

/** The full command line the helper runs for `level`. */
export function helperCommand(indus: CommandSpec, level: UninstallLevel): string[] {
  const args = LEVEL_ARGS[level];
  if (args === undefined) throw new Error(`unknown uninstall level ${String(level)}`);
  return [indus.command, ...indus.args, ...args];
}

function posixQuote(text: string): string {
  return `'${text.replace(/'/g, `'\\''`)}'`;
}

export function posixHelper(options: {
  pid: number;
  argv: readonly string[];
  home: string;
}): string {
  const pid = Number.isInteger(options.pid) && options.pid > 0 ? options.pid : 0;
  return [
    "#!/bin/sh",
    "# Indus Agent: runs `indus uninstall` once the desktop app has quit (P24-34).",
    `pid=${pid}`,
    "i=0",
    `while [ "$pid" -gt 0 ] && kill -0 "$pid" 2>/dev/null && [ "$i" -lt ${HELPER_WAIT_SECONDS * 2} ]; do`,
    "  sleep 0.5",
    "  i=$((i + 1))",
    "done",
    `INDUS_HOME=${posixQuote(options.home)}`,
    "export INDUS_HOME",
    `${options.argv.map(posixQuote).join(" ")}`,
    'rm -f -- "$0"',
    "",
  ].join("\n");
}

const CMD_UNSAFE = /["%^&!\r\n]/;

export function windowsHelper(options: {
  pid: number;
  argv: readonly string[];
  home: string;
}): string {
  for (const part of [options.home, ...options.argv]) {
    if (CMD_UNSAFE.test(part)) throw new Error("a path holds a character cmd cannot quote");
  }
  const pid = Number.isInteger(options.pid) && options.pid > 0 ? options.pid : 0;
  return [
    "@echo off",
    "rem Indus Agent: runs `indus uninstall` once the desktop app has quit (P24-34).",
    "setlocal",
    `set "PID=${pid}"`,
    "set /a WAITED=0",
    ":wait",
    'tasklist /NH /FI "PID eq %PID%" 2>nul | findstr /r /c:" %PID% " >nul',
    "if errorlevel 1 goto run",
    "set /a WAITED+=1",
    `if %WAITED% geq ${HELPER_WAIT_SECONDS} goto run`,
    "timeout /t 1 /nobreak >nul",
    "goto wait",
    ":run",
    `set "INDUS_HOME=${options.home}"`,
    options.argv.map((part) => `"${part}"`).join(" "),
    '(goto) 2>nul & del "%~f0"',
    "",
  ].join("\r\n");
}

export interface UninstallDeps {
  home: string;
  platform: string;
  pid: number;
  execPath: string;
  /** The `indus` the backend resolver picked, or null when there is none. */
  indus(): Promise<CommandSpec | null>;
  /** Run `indus` with `args`; stdout when it exits 0 within the deadline, else null. */
  run(indus: CommandSpec, args: readonly string[], timeoutMs: number): Promise<string | null>;
  readReceipts(): Promise<unknown[]>;
  /** Write the helper script (owner-only, executable) and return its path. */
  writeHelper(text: string, extension: ".sh" | ".cmd"): Promise<string>;
  /** Start the helper detached from the app (it outlives the app's exit). */
  spawnDetached(command: string, args: readonly string[]): void;
  quit(): void;
  log?: (line: string) => void;
}

export type RunAnswer = { ok: true } | { ok: false; code: string; message: string };

export class UninstallService {
  private started = false;
  private readonly deps: UninstallDeps;

  constructor(deps: UninstallDeps) {
    this.deps = deps;
  }

  async summary(): Promise<UninstallSummaryView> {
    const indus = await this.deps.indus();
    const summaryText =
      indus === null ? null : await this.deps.run(indus, SUMMARY_ARGS, SUMMARY_TIMEOUT_MS);
    const receipts = await this.deps.readReceipts().catch(() => []);
    return decideOwnership({
      home: this.deps.home,
      execPath: this.deps.execPath,
      summaryText,
      receipts,
    });
  }

  async run(level: UninstallLevel, typed: string | undefined): Promise<RunAnswer> {
    if (this.started) return { ok: false, code: "busy", message: "Uninstall is already starting." };
    if (!LEVELS.includes(level)) {
      return { ok: false, code: "bad-level", message: "That is not an uninstall level." };
    }
    if (level === "full" && (typed ?? "").trim() !== CONFIRM_PHRASE) {
      return {
        ok: false,
        code: "confirm",
        message: `Type "${CONFIRM_PHRASE}" to remove everything.`,
      };
    }
    // Ownership is decided again here: the page's copy may be stale.
    const view = await this.summary();
    const indus = await this.deps.indus();
    if (!view.allowed || indus === null || !view.levels.includes(level)) {
      return { ok: false, code: "not-owned", message: view.reason ?? NOT_CONFIRMED };
    }
    const argv = helperCommand(indus, level);
    const windows = this.deps.platform === "win32";
    let script: string;
    try {
      const text = windows
        ? windowsHelper({ pid: this.deps.pid, argv, home: this.deps.home })
        : posixHelper({ pid: this.deps.pid, argv, home: this.deps.home });
      script = await this.deps.writeHelper(text, windows ? ".cmd" : ".sh");
    } catch (error) {
      return {
        ok: false,
        code: "helper",
        message: error instanceof Error ? error.message : "The helper could not be written.",
      };
    }
    this.started = true;
    this.deps.log?.(`[uninstall] helper started for level ${level}`);
    if (windows) this.deps.spawnDetached("cmd.exe", ["/d", "/c", script]);
    else this.deps.spawnDetached("/bin/sh", [script]);
    this.deps.quit();
    return { ok: true };
  }
}

/** Register `indus:uninstall:summary` and `indus:uninstall:run`. */
export function installUninstall(context: MainContext, deps: UninstallDeps): UninstallService {
  const service = new UninstallService(deps);
  context.registry.handle("indus:uninstall:summary", () => service.summary());
  context.registry.handle("indus:uninstall:run", async (request) => {
    const answer = await service.run(request.level, request.typed);
    if (!answer.ok && answer.code === "bad-level")
      throw new IpcError("bad-request", answer.message);
    return answer;
  });
  return service;
}
