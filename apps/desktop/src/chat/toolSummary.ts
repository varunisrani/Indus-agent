// Tool rows: a one-line structured summary (verb + target) and the colour rule — red only when
// the tool failed explicitly; a bare non-zero exit (exit 1 with nothing else, e.g. a search that
// found nothing) is neutral.

export type ToolVerb = "run" | "read" | "write" | "edit" | "search" | "fetch" | "use";

export interface ToolSummary {
  readonly verb: ToolVerb;
  readonly target: string;
}

const TARGET_MAX = 80;

const VERBS: ReadonlyArray<readonly [ToolVerb, RegExp, readonly string[]]> = [
  ["run", /^(terminal|shell|bash|run|exec|execute|run_command|process)/i, ["command", "cmd"]],
  ["read", /^(read|read_file|view|cat|open_file)/i, ["path", "file", "file_path"]],
  ["write", /^(write|write_file|create_file)/i, ["path", "file", "file_path"]],
  ["edit", /^(edit|patch|apply_patch|replace|str_replace)/i, ["path", "file", "file_path"]],
  ["search", /(search|grep|find|glob|lookup)/i, ["query", "pattern", "q", "regex", "glob", "path"]],
  ["fetch", /(fetch|browse|http|url|web_extract|navigate)/i, ["url", "href", "link"]],
];

function clip(text: string): string {
  const line = text.replace(/\s+/g, " ").trim();
  return line.length > TARGET_MAX ? `${line.slice(0, TARGET_MAX - 1)}…` : line;
}

function firstString(args: Readonly<Record<string, unknown>> | null, keys: readonly string[]) {
  if (args === null) return "";
  for (const key of keys) {
    const value = args[key];
    if (typeof value === "string" && value.trim() !== "") return value;
  }
  for (const value of Object.values(args)) {
    if (typeof value === "string" && value.trim() !== "") return value;
  }
  return "";
}

export function toolSummary(
  name: string,
  args: Readonly<Record<string, unknown>> | null,
): ToolSummary {
  for (const [verb, pattern, keys] of VERBS) {
    if (pattern.test(name)) return { verb, target: clip(firstString(args, keys)) };
  }
  return { verb: "use", target: clip(firstString(args, [])) };
}

const EXPLICIT_FAILURE =
  /\b(error|exception|traceback|failed|failure|denied|refused|not found|no such file|permission|timed out|cancell?ed|forbidden|fatal|panic)\b/i;
const EXIT_ONE = /(?:exit(?:ed)?(?: with)?(?: code| status)?\s*[:=]?\s*|"exit_code"\s*:\s*)1\b/i;

/** The row's colour state once the tool finished. */
export function toolOutcome(ok: boolean, result: string): "ok" | "failed" | "neutral" {
  if (ok) return "ok";
  // Empty error fields of a structured result say nothing.
  const said = result.replace(/"(error|stderr|exception)"\s*:\s*(null|""|\[\])/gi, "");
  if (EXPLICIT_FAILURE.test(said)) return "failed";
  if (EXIT_ONE.test(result)) return "neutral";
  return "failed";
}

/** Images a tool result carries (data URLs or our media scheme), for the expanded row. */
export function toolImages(result: string | null): string[] {
  if (result === null || result === "") return [];
  const found = result.match(
    /(data:image\/(?:png|jpe?g|gif|webp);base64,[A-Za-z0-9+/=]+|indus-media:\/\/[^\s"')]+)/g,
  );
  return found === null ? [] : [...new Set(found)].slice(0, 8);
}
