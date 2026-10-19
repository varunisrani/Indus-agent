// Ctrl+G / Alt+G: write the prompt in the user's own editor (P12-17). The draft (continuation
// lines included) goes to a private temp file, the renderer hands the terminal over (`suspend`,
// wired by the app to Ink's suspendTerminal plus our terminal-mode reset/restore), the editor
// runs, and on a clean exit with text the result is submitted. Any other exit leaves the draft.

import { spawnSync } from "node:child_process";
import { accessSync, constants, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";
import type { Composer } from "./composerStore.ts";

/** Tried in order on PATH when neither VISUAL nor EDITOR is set. */
export const EDITOR_FALLBACKS = ["editor", "nano", "vi"] as const;

/** Splits a command line the way a POSIX shell would for words: quotes and backslashes. */
export function splitCommand(line: string): string[] {
  const words: string[] = [];
  let word = "";
  let started = false;
  let quote: '"' | "'" | null = null;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i] ?? "";
    if (quote === "'") {
      if (ch === "'") quote = null;
      else word += ch;
      continue;
    }
    if (
      ch === "\\" &&
      i + 1 < line.length &&
      (quote === null || /["\\$`]/.test(line[i + 1] ?? ""))
    ) {
      word += line[i + 1];
      started = true;
      i += 1;
      continue;
    }
    if (quote === '"') {
      if (ch === '"') quote = null;
      else word += ch;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      started = true;
    } else if (/\s/.test(ch)) {
      if (started) words.push(word);
      word = "";
      started = false;
    } else {
      word += ch;
      started = true;
    }
  }
  if (started) words.push(word);
  return words;
}

function executable(path: string): boolean {
  try {
    accessSync(path, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

/** The editor command (without the file): VISUAL, EDITOR, then the PATH fallbacks, then vi. */
export function resolveEditor(
  env: NodeJS.ProcessEnv = process.env,
  platform: NodeJS.Platform = process.platform,
  canRun: (path: string) => boolean = executable,
): string[] {
  for (const name of ["VISUAL", "EDITOR"]) {
    const words = splitCommand(env[name] ?? "");
    if (words.length > 0) return words;
  }
  if (platform === "win32") return ["notepad.exe"];
  const dirs = (env.PATH ?? "").split(delimiter).filter((dir) => dir !== "");
  for (const name of EDITOR_FALLBACKS) {
    for (const dir of dirs) {
      const candidate = join(dir, name);
      if (canRun(candidate)) return [candidate];
    }
  }
  return ["vi"];
}

export type EditorResult =
  | { readonly status: "saved"; readonly text: string }
  | { readonly status: "aborted"; readonly code: number | null }
  | { readonly status: "failed"; readonly error: string };

export interface EditorOptions {
  /** Hands the terminal to `run` and takes it back afterwards. */
  suspend?: (run: () => void | Promise<void>) => Promise<void>;
  env?: NodeJS.ProcessEnv;
  platform?: NodeJS.Platform;
}

/** Opens `initial` in the editor; the temp folder is removed whatever happens. */
export async function editText(
  initial: string,
  options: EditorOptions = {},
): Promise<EditorResult> {
  const env = options.env ?? process.env;
  const [command, ...args] = resolveEditor(env, options.platform ?? process.platform);
  const folder = mkdtempSync(join(tmpdir(), "indus-edit-"));
  const file = join(folder, "prompt.md");
  try {
    writeFileSync(file, initial, { encoding: "utf8", mode: 0o600 });
    let code: number | null = null;
    let error: Error | undefined;
    const run = (): void => {
      const child = spawnSync(command ?? "vi", [...args, file], { stdio: "inherit", env });
      code = child.status;
      error = child.error;
    };
    if (options.suspend) await options.suspend(run);
    else run();
    if (error !== undefined) return { status: "failed", error: error.message };
    if (code !== 0) return { status: "aborted", code };
    return { status: "saved", text: readFileSync(file, "utf8") };
  } finally {
    rmSync(folder, { recursive: true, force: true });
  }
}

/**
 * The composer's Ctrl+G: edit continuation + draft, submit the saved text on a clean exit.
 * Returns what happened so the caller can print a notice for "failed".
 */
export async function editComposer(
  composer: Composer,
  submit: (text: string) => void,
  options: EditorOptions = {},
): Promise<EditorResult> {
  const result = await editText(composer.fullDraft(), options);
  if (result.status !== "saved") return result;
  const text = result.text.trimEnd();
  if (text.trim() === "") return { status: "aborted", code: 0 };
  composer.clear();
  submit(text);
  return { status: "saved", text };
}
