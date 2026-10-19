// `{!command}` in a prompt (P12-19): each occurrence is replaced by that command's output right
// before the prompt leaves; a queued prompt keeps the raw text until then. If any command cannot
// run the whole send is refused — the model never sees a stand-in for output it was promised.

const PATTERN = /\{!(.+?)\}/g;

export function hasInterpolation(text: string): boolean {
  return /\{!.+?\}/.test(text);
}

/** The commands in order of appearance. */
export function interpolationCommands(text: string): string[] {
  return [...text.matchAll(PATTERN)].map((match) => match[1] ?? "");
}

/** Runs every command (concurrently) and splices the outputs in; rejects if any run fails. */
export async function interpolate(
  text: string,
  run: (command: string) => Promise<string>,
): Promise<string> {
  const matches = [...text.matchAll(PATTERN)];
  if (matches.length === 0) return text;
  const outputs = await Promise.all(matches.map((match) => run(match[1] ?? "")));
  let out = "";
  let from = 0;
  matches.forEach((match, index) => {
    const at = match.index ?? 0;
    out += text.slice(from, at) + (outputs[index] ?? "");
    from = at + match[0].length;
  });
  return out + text.slice(from);
}

/** Combined, trimmed output of a `shell.exec` reply. */
export function shellOutput(reply: { stdout?: unknown; stderr?: unknown }): string {
  return [reply.stdout, reply.stderr]
    .map((part) => (typeof part === "string" ? part.trim() : ""))
    .filter((part) => part !== "")
    .join("\n");
}
