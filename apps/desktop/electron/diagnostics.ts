// Diagnostics and logs (P20-39): the logs channels, renderer error forwarding into desktop.log,
// the clipboard bridge and "Copy Diagnostics".
//
// The diagnostics bundle is plain text assembled in main — app and runtime versions, platform,
// build stamp, paths (home shown as `~`), which INDUS_* variables are set (names only), window
// count, recent renderer errors and the recent log — and passed whole through the secret redactor
// (patterns plus the literal values main holds) before it reaches the clipboard. It never crosses
// the bridge: the renderer learns only `{ copied, bytes }`.

import fs from "node:fs";
import path from "node:path";
import type { MenuItem } from "./appMenu.ts";
import type { MainContext } from "./context.ts";
import type { LogLevel } from "./log.ts";
import { redactKnown } from "./redact.ts";

/** Renderer errors written per window per minute before the rest are suppressed. */
export const RENDERER_ERRORS_PER_MINUTE = 20;
export const RENDERER_ERROR_WINDOW_MS = 60_000;
/** Stack lines kept per renderer error. */
export const RENDERER_STACK_LINES = 20;
/** Renderer error summaries kept for the diagnostics bundle. */
export const RENDERER_ERRORS_KEPT = 50;
/** Log lines in a diagnostics bundle (newest kept), and the bundle's byte cap. */
export const DIAGNOSTICS_LOG_LINES = 300;
export const DIAGNOSTICS_MAX_BYTES = 256 * 1024;
export const BUILD_STAMP_FILE = "desktop-build-stamp.json";

export interface RendererErrorReport {
  message: string;
  stack?: string | undefined;
  source?: string | undefined;
}

interface Budget {
  start: number;
  count: number;
  suppressedNoted: boolean;
}

/** Writes renderer errors to the log at ERROR, at most 20 per window per minute. */
export class RendererErrors {
  private readonly write: (message: string, level: LogLevel) => void;
  private readonly now: () => number;
  private readonly budgets = new Map<number, Budget>();
  private readonly kept: string[] = [];

  constructor(options: { write: (message: string, level: LogLevel) => void; now?: () => number }) {
    this.write = options.write;
    this.now = options.now ?? Date.now;
  }

  /** Record one report from window `senderId`; returns false when it was suppressed. */
  record(senderId: number, report: RendererErrorReport): boolean {
    const now = this.now();
    let budget = this.budgets.get(senderId);
    if (budget === undefined || now - budget.start >= RENDERER_ERROR_WINDOW_MS) {
      budget = { start: now, count: 0, suppressedNoted: false };
      this.budgets.set(senderId, budget);
    }
    budget.count += 1;
    if (budget.count > RENDERER_ERRORS_PER_MINUTE) {
      if (!budget.suppressedNoted) {
        budget.suppressedNoted = true;
        this.write(
          `[renderer] window ${senderId}: more errors this minute; suppressed until it ends`,
          "WARNING",
        );
      }
      return false;
    }
    const source = (report.source ?? "renderer").replace(/\s+/g, " ").slice(0, 120);
    const message = report.message.replace(/\s+/g, " ").trim().slice(0, 2000) || "(no message)";
    const head = `[renderer] ${source} (window ${senderId}): ${message}`;
    const stack = (report.stack ?? "")
      .split(/\r?\n/)
      .map((line) => line.trimEnd())
      .filter((line) => line.trim() !== "")
      .slice(0, RENDERER_STACK_LINES)
      .map((line) => `    ${line.trim()}`);
    this.write([head, ...stack].join("\n"), "ERROR");
    this.kept.push(head);
    if (this.kept.length > RENDERER_ERRORS_KEPT) this.kept.shift();
    return true;
  }

  /** Recent error headlines (oldest first), for the diagnostics bundle. */
  recent(): string[] {
    return [...this.kept];
  }

  /** Drop a closed window's budget. */
  forget(senderId: number): void {
    this.budgets.delete(senderId);
  }
}

export interface DiagnosticsFacts {
  appVersion: string;
  platform: string;
  arch: string;
  packaged: boolean;
  dev: boolean;
  /** `process.versions` subset: electron, chrome, node. */
  versions: Readonly<Record<string, string | undefined>>;
  locale?: string;
}

export interface DiagnosticsInput {
  facts: DiagnosticsFacts;
  /** The parsed build stamp, or null (source checkout or unreadable). */
  stamp: unknown;
  paths: Readonly<Record<string, string>>;
  homeDir: string;
  env: Readonly<Record<string, string | undefined>>;
  windowCount: number;
  rendererErrors: readonly string[];
  logLines: readonly string[];
  /** Extra sections from other modules (boot state, connection kind, …); redacted like the rest. */
  sections?: ReadonlyArray<{ title: string; lines: readonly string[] }>;
  /** Literal secret values main holds (tokens it minted); masked wherever they appear. */
  knownSecrets?: Iterable<string>;
  now: Date;
}

function tildify(text: string, homeDir: string): string {
  if (homeDir.length < 2) return text;
  return text.split(homeDir).join("~");
}

function stampLines(stamp: unknown): string[] {
  if (stamp === null || typeof stamp !== "object") return ["none (source checkout)"];
  const record = stamp as Record<string, unknown>;
  return ["version", "commit", "channel", "updateOwner", "platform", "builtAt"].map(
    (key) => `${key}: ${String(record[key] ?? "?")}`,
  );
}

/** The diagnostics text: assembled, home replaced by `~`, redacted, capped. */
export function buildDiagnostics(input: DiagnosticsInput): string {
  const { facts } = input;
  const section = (title: string, lines: readonly string[]) =>
    [`## ${title}`, ...(lines.length === 0 ? ["(none)"] : lines), ""].join("\n");
  const indusVars = Object.keys(input.env)
    .filter((key) => key.startsWith("INDUS_") && input.env[key] !== undefined)
    .sort();
  const head = [
    "# Indus desktop diagnostics",
    `generated: ${input.now.toISOString()}`,
    "",
    section("App", [
      `version: ${facts.appVersion}`,
      `packaged: ${facts.packaged}`,
      `dev: ${facts.dev}`,
      `platform: ${facts.platform} ${facts.arch}`,
      `electron: ${facts.versions.electron ?? "?"}`,
      `chrome: ${facts.versions.chrome ?? "?"}`,
      `node: ${facts.versions.node ?? "?"}`,
      ...(facts.locale === undefined ? [] : [`locale: ${facts.locale}`]),
      `windows: ${input.windowCount}`,
    ]),
    section("Build stamp", stampLines(input.stamp)),
    section(
      "Paths",
      Object.entries(input.paths).map(([key, value]) => `${key}: ${value}`),
    ),
    section("Environment (names only)", indusVars),
    ...(input.sections ?? []).map((s) => section(s.title, s.lines)),
    section("Renderer errors", input.rendererErrors),
  ].join("\n");

  let logLines = input.logLines.slice(-DIAGNOSTICS_LOG_LINES);
  const finish = (lines: readonly string[]) =>
    redactKnown(
      tildify(`${head}\n${section("Recent log", lines)}`, input.homeDir),
      input.knownSecrets ?? [],
    );
  let text = finish(logLines);
  while (Buffer.byteLength(text) > DIAGNOSTICS_MAX_BYTES && logLines.length > 0) {
    logLines = logLines.slice(Math.ceil(logLines.length / 4));
    text = finish(logLines);
  }
  if (Buffer.byteLength(text) > DIAGNOSTICS_MAX_BYTES) {
    text = Buffer.from(text).subarray(0, DIAGNOSTICS_MAX_BYTES).toString("utf8");
  }
  return text;
}

/** The third-party notices file the packager puts beside the archive (W46, F-2). */
export const NOTICES_FILE = "THIRD_PARTY_NOTICES.md";
/** The notices are about 150 KB; anything far past that is not our file. */
export const NOTICES_MAX_BYTES = 4 * 1024 * 1024;

/** The packaged notices text, or null (a development run, a missing, oversized or unreadable
 * file). The renderer shows it as plain text. */
export function readNotices(resourcesPath: string | null): string | null {
  if (resourcesPath === null) return null;
  try {
    const file = path.join(resourcesPath, NOTICES_FILE);
    const stat = fs.statSync(file);
    if (!stat.isFile() || stat.size > NOTICES_MAX_BYTES) return null;
    return fs.readFileSync(file, "utf8");
  } catch {
    return null;
  }
}

/** The build stamp beside the packaged archive, or null. */
export function readBuildStamp(resourcesPath: string | null): unknown {
  if (resourcesPath === null) return null;
  try {
    return JSON.parse(fs.readFileSync(path.join(resourcesPath, BUILD_STAMP_FILE), "utf8"));
  } catch {
    return null;
  }
}

/** Electron's clipboard (its text calls may answer synchronously or with a promise). */
export interface ClipboardLike {
  readText(): string | Promise<string>;
  writeText(text: string): void | Promise<void>;
}

export interface DiagnosticsDeps {
  clipboard: ClipboardLike;
  /** Show a file in the OS file manager (`shell.showItemInFolder`). */
  revealPath(file: string): void;
  facts: Omit<DiagnosticsFacts, "appVersion" | "platform" | "packaged" | "dev">;
  homeDir: string;
  env: Readonly<Record<string, string | undefined>>;
  /** `process.resourcesPath` when packaged, else null. */
  resourcesPath: string | null;
  knownSecrets?: () => Iterable<string>;
  sections?: () => ReadonlyArray<{ title: string; lines: readonly string[] }>;
  now?: () => Date;
}

export interface DiagnosticsHandle {
  rendererErrors: RendererErrors;
  /** Build the bundle and put it on the clipboard (the Help menu and the bridge call this). */
  copyDiagnostics(): Promise<{ copied: boolean; bytes: number }>;
  /** Items for the app menu's Help submenu. */
  menuItems: MenuItem[];
}

/** Register the logs, clipboard and diagnostics channels (P20-39) and the notices (W46, F-2). */
export function installDiagnostics(context: MainContext, deps: DiagnosticsDeps): DiagnosticsHandle {
  const { registry, log } = context;
  const now = deps.now ?? (() => new Date());
  const rendererErrors = new RendererErrors({
    write: (message, level) => log.write(message, level),
    now: () => now().getTime(),
  });

  const copyDiagnostics = async () => {
    try {
      log.flush();
      const text = buildDiagnostics({
        facts: {
          ...deps.facts,
          appVersion: context.appVersion,
          platform: context.platform,
          packaged: context.packaged,
          dev: context.dev,
        },
        stamp: readBuildStamp(deps.resourcesPath),
        paths: {
          home: context.paths.home,
          userData: context.paths.userData,
          desktopLog: context.paths.desktopLog,
        },
        homeDir: deps.homeDir,
        env: deps.env,
        windowCount: context.windows.windows().length,
        rendererErrors: rendererErrors.recent(),
        logLines: log.recent(),
        ...(deps.sections === undefined ? {} : { sections: deps.sections() }),
        ...(deps.knownSecrets === undefined ? {} : { knownSecrets: deps.knownSecrets() }),
        now: now(),
      });
      await deps.clipboard.writeText(text);
      log.write("[diagnostics] copied to the clipboard");
      return { copied: true, bytes: Buffer.byteLength(text) };
    } catch (error) {
      log.write(
        `[diagnostics] copy failed: ${error instanceof Error ? error.message : String(error)}`,
        "WARNING",
      );
      return { copied: false, bytes: 0 };
    }
  };

  const revealLog = () => {
    log.flush();
    deps.revealPath(context.paths.desktopLog);
  };

  registry.handle("indus:logs:recent", ({ limit }) => ({ lines: log.recent(limit) }));
  registry.handle("indus:logs:reveal", () => {
    revealLog();
    return { ok: true as const };
  });
  registry.on("indus:logs:renderer-error", (report, { senderId }) => {
    rendererErrors.record(senderId, report);
  });
  registry.handle("indus:diagnostics:copy", () => copyDiagnostics());
  registry.handle("indus:about:notices", () => ({ text: readNotices(deps.resourcesPath) }));
  registry.handle("indus:clipboard:read-text", async () => ({
    text: await deps.clipboard.readText(),
  }));
  registry.handle("indus:clipboard:write-text", async ({ text }) => {
    await deps.clipboard.writeText(text);
    return { ok: true as const };
  });

  return {
    rendererErrors,
    copyDiagnostics,
    menuItems: [
      { label: "Copy Diagnostics", click: () => void copyDiagnostics() },
      { label: "Show Log File", click: revealLog },
    ],
  };
}
