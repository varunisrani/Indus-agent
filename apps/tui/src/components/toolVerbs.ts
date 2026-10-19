/** What a running tool row says it is doing. Our own wording, one verb phrase per tool. */

const EXACT: Readonly<Record<string, string>> = {
  read_file: "reading",
  write_file: "writing",
  patch: "editing",
  search_files: "searching files",
  terminal: "running",
  process_manage: "minding processes",
  execute_code: "executing",
  web_search: "searching the web",
  web_extract: "fetching",
  vision_analyze: "looking",
  image_generate: "drawing",
  todo_list: "planning",
  clarify: "asking",
  delegate_task: "delegating",
  computer_use: "operating the desktop",
  memory: "remembering",
  session_search: "searching sessions",
  skill_view: "opening a skill",
  skills_list: "listing skills",
  send_message: "sending",
  text_to_speech: "speaking",
};

const PREFIXES: readonly (readonly [string, string])[] = [
  ["browser_", "browsing"],
  ["skill", "consulting skills"],
  ["web_", "on the web"],
  ["file_", "handling files"],
];

export const FALLBACK_VERB = "working";

/** The verb for a tool name; `mcp__server__tool` names are looked up by their last part. */
export function toolVerb(name: string): string {
  const plain = name.includes("__") ? (name.split("__").at(-1) ?? name) : name;
  const exact = EXACT[plain];
  if (exact) return exact;
  for (const [prefix, verb] of PREFIXES) if (plain.startsWith(prefix)) return verb;
  return name.startsWith("mcp__") ? "calling" : FALLBACK_VERB;
}
