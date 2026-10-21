// Slash commands in the desktop (P20-24).
//
// Curation hides a KNOWN list only: the built-ins the Python registry does not offer on the
// `desktop` surface (from the generated `slashCatalog.json`) plus a few terminal display toggles
// the TUI keeps to itself. Every other name — skill, quick and plugin commands, a built-in added
// later — passes both completion paths and runs when typed (R08.12).
//
// Running a line: a desktop-owned built-in runs locally; anything else goes to `slash.exec`, and
// only a "not owned" refusal falls back to `command.dispatch` (P12-24's order). A skill or `send`
// result is a prompt the host submits the normal way.

import type {
  CatalogEntry,
  CommandDispatchResult,
  CompletionItem,
  MethodName,
  MethodTypes,
  SlashExecResult,
} from "@indus/protocol";
import type { RequestOptions } from "../gateway/connectionStore.ts";
import { t } from "../i18n/index.tsx";
import { errorMessage, errorName } from "./rpcErrors.ts";
import catalogFile from "./slashCatalog.json" with { type: "json" };

export interface OfflineCommand {
  readonly name: string;
  readonly aliases: readonly string[];
  readonly description: string;
  readonly category: string;
  readonly argsHint: string;
  /** Whether the registry offers it on the desktop surface. */
  readonly desktop: boolean;
}

/** Every registry built-in, generated at build time (`scripts/dump-slash-catalog.mjs`). */
export const OFFLINE_CATALOG: readonly OfflineCommand[] = catalogFile.commands;

/** Built-ins the desktop runs itself, never sending them to the daemon. */
export const DESKTOP_LOCAL_COMMANDS = ["new", "model", "details", "reasoning", "help"] as const;
export type LocalCommandName = (typeof DESKTOP_LOCAL_COMMANDS)[number];

/** TUI-local commands that are not registry entries and mean nothing in the desktop. */
export const EXTRA_HIDDEN: readonly string[] = [
  "busy",
  "copy",
  "image",
  "logs",
  "mouse",
  "paste",
  "prompt",
  "redraw",
  "reload",
  "statusbar",
  "theme",
  "update",
];

export const MAX_ALIAS_HOPS = 4;

export function normalizeName(word: string): string {
  return word.trim().replace(/^\//, "").toLowerCase();
}

const BY_KEY = new Map<string, OfflineCommand>();
for (const command of OFFLINE_CATALOG) {
  for (const key of [command.name, ...command.aliases]) BY_KEY.set(key, command);
}
const LOCAL = new Set<string>(DESKTOP_LOCAL_COMMANDS);
const EXTRA = new Set(EXTRA_HIDDEN);

/** The canonical registry name for a name or alias (unknown names come back normalized). */
export function canonicalName(word: string): string {
  const name = normalizeName(word);
  return BY_KEY.get(name)?.name ?? name;
}

/** The desktop-owned built-in a name or alias names (`/reset` → `new`), or null. */
export function localCommandFor(word: string): LocalCommandName | null {
  const name = canonicalName(word);
  return LOCAL.has(name) ? (name as LocalCommandName) : null;
}

/** True only for a known built-in with no desktop surface. Unknown names are never hidden. */
export function isHiddenCommand(word: string): boolean {
  const name = normalizeName(word);
  if (name === "") return false;
  if (localCommandFor(name) !== null) return false;
  if (EXTRA.has(name)) return true;
  const known = BY_KEY.get(name);
  return known !== undefined && !known.desktop;
}

/** `complete.slash` answers with hidden built-ins removed (first word only). */
export function curateCompletions(items: readonly CompletionItem[]): CompletionItem[] {
  return items.filter((item) => {
    const word = item.text.trim();
    if (word.includes(" ")) return true;
    return !isHiddenCommand(word);
  });
}

/** `commands.catalog` entries with hidden built-ins removed. */
export function curateCatalog(entries: readonly CatalogEntry[]): CatalogEntry[] {
  return entries.filter((entry) => !isHiddenCommand(entry.name));
}

/** Completion from the generated catalogue, for when the daemon cannot be reached. */
export function offlineCompletions(text: string): CompletionItem[] {
  if (!text.startsWith("/") || text.includes(" ")) return [];
  const typed = normalizeName(text);
  const items: CompletionItem[] = [];
  for (const command of OFFLINE_CATALOG) {
    if (isHiddenCommand(command.name)) continue;
    if (!command.name.startsWith(typed)) continue;
    items.push({
      text: `/${command.name}`,
      display: `/${command.name}`,
      meta: command.description,
    });
  }
  for (const name of DESKTOP_LOCAL_COMMANDS) {
    if (BY_KEY.has(name) || !name.startsWith(typed)) continue;
    items.push({ text: `/${name}`, display: `/${name}`, meta: t("slash.localMeta") });
  }
  return items.sort((a, b) => a.text.localeCompare(b.text));
}

/** A command line, not a path such as `/usr/bin/env` and not a lone `/`. */
export function isSlashLine(text: string): boolean {
  const trimmed = text.trim();
  if (!trimmed.startsWith("/") || trimmed.length < 2) return false;
  const first = trimmed.split(/\s/, 1)[0] ?? "";
  return !first.slice(1).includes("/");
}

export interface ParsedSlash {
  readonly name: string;
  readonly arg: string;
}

export function parseSlash(line: string): ParsedSlash {
  const body = line.trim().replace(/^\//, "");
  const space = body.search(/\s/);
  if (space < 0) return { name: body.toLowerCase(), arg: "" };
  return { name: body.slice(0, space).toLowerCase(), arg: body.slice(space + 1).trim() };
}

type Request = <M extends MethodName>(
  method: M,
  params: MethodTypes[M]["params"],
  options?: RequestOptions,
) => Promise<MethodTypes[M]["result"]>;

export interface SlashHost {
  /** The connection's call, or null when there is none. */
  readonly request: Request | null;
  /** The session commands act on, or null for a fresh draft. */
  readonly sessionId: string | null;
  /** The five desktop-owned built-ins. */
  readonly local: Readonly<Record<LocalCommandName, (arg: string) => void | Promise<void>>>;
  /** Send `text` as an ordinary prompt; `display` is what the transcript shows. */
  submit(text: string, display: string): void;
  /** Put `text` into the composer. */
  prefill(text: string): void;
  /** A system line in the conversation. */
  show(text: string): void;
  switchSession?(sessionId: string): void;
  /** False once the person moved to another session: late results are dropped. */
  isCurrent?(): boolean;
}

export type SlashOutcome =
  | "local"
  | "hidden"
  | "output"
  | "submitted"
  | "prefilled"
  | "switched"
  | "unknown"
  | "failed"
  | "no-session"
  | "stale";

/** Run one typed line. */
export async function runSlash(line: string, host: SlashHost): Promise<SlashOutcome> {
  return route(parseSlash(line), host, 0);
}

async function route(parsed: ParsedSlash, host: SlashHost, hops: number): Promise<SlashOutcome> {
  const { name, arg } = parsed;
  if (name === "") {
    host.show(t("slash.empty"));
    return "unknown";
  }
  const local = localCommandFor(name);
  if (local !== null) {
    try {
      await host.local[local](arg);
    } catch (error) {
      host.show(t("slash.failed", { name: local, message: errorMessage(error) }));
      return "failed";
    }
    return "local";
  }
  if (isHiddenCommand(name)) {
    host.show(t("slash.notInDesktop", { name }));
    return "hidden";
  }
  if (host.request === null || host.sessionId === null) {
    host.show(t("slash.needsSession", { name }));
    return "no-session";
  }
  const stale = () => host.isCurrent !== undefined && !host.isCurrent();
  const command = arg ? `${name} ${arg}` : name;
  let exec: SlashExecResult & Partial<CommandDispatchResult>;
  try {
    exec = await host.request("slash.exec", { session_id: host.sessionId, command });
  } catch (error) {
    if (stale()) return "stale";
    const code = errorName(error);
    if (code === "SLASH_NOT_OWNED") return dispatch(parsed, host, hops, errorMessage(error));
    return reportFailure(name, error, host);
  }
  if (stale()) return "stale";
  if (typeof exec.kind === "string") {
    return settleDispatch(exec as CommandDispatchResult, parsed, host, hops);
  }
  let outcome: SlashOutcome | null = null;
  if (exec.output) {
    host.show(exec.output);
    outcome = "output";
  }
  if (exec.prefill) {
    host.prefill(exec.prefill);
    outcome = "prefilled";
  }
  if (exec.submit) {
    host.submit(exec.submit, `/${command}`);
    outcome = "submitted";
  }
  if (exec.switch_to) {
    if (host.switchSession) host.switchSession(exec.switch_to);
    else host.show(t("slash.switchAsked", { name, session: exec.switch_to }));
    outcome = "switched";
  }
  if (outcome === null) {
    host.show(t("slash.done", { name }));
    return "output";
  }
  return outcome;
}

function reportFailure(name: string, error: unknown, host: SlashHost): SlashOutcome {
  const code = errorName(error);
  if (code === "UNSUPPORTED_COMMAND") {
    host.show(t("slash.unknown", { name }));
    return "unknown";
  }
  if (code === "SLASH_WORKER_FAILED") {
    // A crash or timeout of the command's worker is reported as exactly that.
    host.show(t("slash.workerFailed", { name, message: errorMessage(error) }));
    return "failed";
  }
  host.show(t("slash.failed", { name, message: errorMessage(error) }));
  return "failed";
}

async function dispatch(
  parsed: ParsedSlash,
  host: SlashHost,
  hops: number,
  refusal: string,
): Promise<SlashOutcome> {
  const { name, arg } = parsed;
  const request = host.request;
  const sessionId = host.sessionId;
  if (request === null || sessionId === null) return "no-session";
  let result: CommandDispatchResult;
  try {
    result = await request("command.dispatch", { session_id: sessionId, name, arg });
  } catch (error) {
    if (host.isCurrent !== undefined && !host.isCurrent()) return "stale";
    if (errorName(error) === "UNSUPPORTED_COMMAND") {
      host.show(t("slash.unknownWithReason", { name, message: refusal }));
      return "unknown";
    }
    return reportFailure(name, error, host);
  }
  if (host.isCurrent !== undefined && !host.isCurrent()) return "stale";
  return settleDispatch(result, parsed, host, hops);
}

async function settleDispatch(
  result: CommandDispatchResult,
  parsed: ParsedSlash,
  host: SlashHost,
  hops: number,
): Promise<SlashOutcome> {
  const { name, arg } = parsed;
  const typed = `/${name}${arg ? ` ${arg}` : ""}`;
  const message = result.message ?? "";
  switch (result.kind) {
    case "exec":
    case "plugin":
      host.show(result.output || t("slash.done", { name }));
      return "output";
    case "alias":
      if (!result.target || hops >= MAX_ALIAS_HOPS) {
        host.show(t("slash.aliasLoop", { name }));
        return "failed";
      }
      return route({ name: normalizeName(result.target), arg }, host, hops + 1);
    case "skill":
    case "send":
      if (!message.trim()) {
        host.show(t("slash.nothingToSend", { name }));
        return "failed";
      }
      host.submit(message, typed);
      return "submitted";
    case "prefill":
      host.prefill(message);
      return "prefilled";
    default:
      host.show(t("slash.strangeAnswer", { name }));
      return "failed";
  }
}

/**
 * Slash completion for the composer: the daemon's `complete.slash` (curated), else the offline
 * catalogue when there is no connection or the call fails.
 */
export async function completeSlash(
  text: string,
  request: Request | null,
  sessionId: string | null,
  signal?: AbortSignal,
): Promise<CompletionItem[]> {
  if (request === null) return offlineCompletions(text);
  try {
    const answer = await request(
      "complete.slash",
      { text, session_id: sessionId },
      signal === undefined ? { timeoutMs: 10_000 } : { timeoutMs: 10_000, signal },
    );
    return curateCompletions(answer.items);
  } catch (error) {
    if (signal?.aborted) return [];
    if (errorName(error) !== undefined && errorName(error) !== "METHOD_NOT_FOUND") return [];
    return offlineCompletions(text);
  }
}

/** Lines for `/help`: the curated catalogue, grouped by category in its order. */
export function helpLines(entries: readonly CatalogEntry[]): string[] {
  const groups = new Map<string, string[]>();
  for (const entry of curateCatalog(entries)) {
    const rows = groups.get(entry.category) ?? [];
    rows.push(
      `/${entry.name}${entry.args_hint ? ` ${entry.args_hint}` : ""} — ${entry.description}`,
    );
    groups.set(entry.category, rows);
  }
  const lines: string[] = [];
  for (const [category, rows] of groups) {
    lines.push(`${category}:`, ...rows.map((row) => `  ${row}`));
  }
  return lines;
}
