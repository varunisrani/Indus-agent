// Our own present-tense labels for running tools, and a short preview of a call's arguments.

const VERBS: readonly [RegExp, string][] = [
  [/^(read_file|file_read|view|cat)$/, "reading"],
  [/^(write_file|create_file)$/, "writing"],
  [/^(patch|edit_file|apply_patch|replace)$/, "editing"],
  [/^(search_files|grep|find|glob|list_files|ls)$/, "looking through files"],
  [/^(terminal|shell|run|exec|process)$/, "running"],
  [/web_search|search_web/, "searching the web"],
  [/^(web_extract|fetch|http)/, "fetching"],
  [/^browser/, "browsing"],
  [/^(todo|todo_list)$/, "planning"],
  [/^(memory|remember)/, "noting"],
  [/^(delegate|subagent)/, "handing off"],
  [/^(clarify|ask)$/, "asking"],
  [/^vision|image/, "looking"],
  [/^(skill|skills)/, "consulting skills"],
];

export function verbFor(toolName: string): string {
  const name = toolName.toLowerCase();
  for (const [pattern, verb] of VERBS) if (pattern.test(name)) return verb;
  return "working";
}

const ARG_PREVIEW_CHARS = 80;

/** One line: the most telling string argument, else compact JSON; cut to 80 characters. */
export function argsPreview(args: Record<string, unknown> | undefined): string {
  if (args === undefined) return "";
  const preferred = ["command", "path", "file_path", "query", "url", "pattern", "goal", "prompt"];
  let text = "";
  for (const key of preferred) {
    const value = args[key];
    if (typeof value === "string" && value.trim() !== "") {
      text = value;
      break;
    }
  }
  if (text === "") {
    const first = Object.values(args).find((v) => typeof v === "string" && v.trim() !== "");
    text =
      typeof first === "string" ? first : Object.keys(args).length > 0 ? JSON.stringify(args) : "";
  }
  const line = text.replace(/\s+/g, " ").trim();
  return line.length > ARG_PREVIEW_CHARS ? `${line.slice(0, ARG_PREVIEW_CHARS - 1)}…` : line;
}

/** True when a tool result is a unified diff worth showing as its own block. */
export function looksLikeDiff(text: string): boolean {
  const body = text.trimStart();
  return /^(--- \S.*\n\+\+\+ \S|diff --git |@@ -\d)/.test(body) && /\n@@ -\d/.test(`\n${body}`);
}
