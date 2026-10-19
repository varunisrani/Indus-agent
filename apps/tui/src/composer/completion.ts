// Composer completion (P12-18): decide which request the text calls for, ask the bridge 60 ms
// after the last change, drop answers for text that has moved on (R05.10), and apply a chosen row.
// Path requests carry the session's folder, else the folder `indus` was launched from — never this
// process's own working directory (R05.13).

import type { Clock } from "@indus/client";
import type { Rpc } from "../app/rpc.ts";
import type { CompletionRow, Composer } from "./composerStore.ts";

export const COMPLETION_DEBOUNCE_MS = 60;
export const COMPLETION_ROWS = 16;

export interface CompletionContext {
  readonly sessionId: string | null;
  /** The session's folder from `session.info`, when known. */
  readonly sessionCwd: string | null;
  /** The folder the user launched from (`INDUS_LAUNCH_CWD`, else the launch shell's `PWD`). */
  readonly launchCwd: string | null;
}

export type CompletionPlan =
  | {
      readonly method: "complete.slash";
      readonly params: { text: string; session_id?: string };
      readonly replaceFrom: number;
      /** A `/word` inside prose: only skills make sense there. */
      readonly skillsOnly: boolean;
    }
  | {
      readonly method: "complete.path";
      readonly params: { word: string; cwd?: string; session_id?: string };
      readonly replaceFrom: number;
    };

/** Text that is a slash command: a leading `/` whose first word holds no other `/`. */
export function isSlashCommandText(text: string): boolean {
  return /^\/[^\s/]*(?:\s|$)/.test(text);
}

const INLINE_SLASH = /\s\/([A-Za-z][\w-]*)?$/;
const TRAILING_WORD = /(?:^|\s)(["']?\S*)$/;

function pathLike(word: string): boolean {
  const bare = word.replace(/^["']/, "");
  if (bare === "") return false;
  return (
    /^(\.{1,2}|~)\//.test(bare) ||
    bare.startsWith("/") ||
    bare.startsWith("@") ||
    /^[A-Za-z]:[\\/]/.test(bare) ||
    bare.includes("/")
  );
}

/** Which request (if any) the composer text calls for. */
export function planCompletion(input: string, ctx: CompletionContext): CompletionPlan | null {
  const withSession = ctx.sessionId !== null ? { session_id: ctx.sessionId } : {};
  const command = isSlashCommandText(input);
  // The model picker owns `/model`; its completions would only be noise.
  if (command && /^\/model(?:\s|$)/.test(input)) return null;
  // Checked before the command shape: in `/help /rev` only the first slash runs a command, the
  // second names a skill, so the token at the caret is what wants completing.
  const inline = INLINE_SLASH.exec(input);
  if (inline !== null) {
    const query = inline[1] ?? "";
    const slashAt = input.length - query.length - 1;
    return {
      method: "complete.slash",
      params: { text: `/${query}`, ...withSession },
      replaceFrom: slashAt + 1,
      skillsOnly: true,
    };
  }
  if (command) {
    return {
      method: "complete.slash",
      params: { text: input, ...withSession },
      replaceFrom: 1,
      skillsOnly: false,
    };
  }
  const word = TRAILING_WORD.exec(input)?.[1] ?? "";
  if (!pathLike(word)) return null;
  const cwd = ctx.sessionCwd ?? ctx.launchCwd;
  return {
    method: "complete.path",
    params: { word, ...(cwd !== null ? { cwd } : {}), ...withSession },
    replaceFrom: input.length - word.length,
  };
}

/** The launch folder the launcher exported, else the launch shell's PWD; never `process.cwd()`. */
export function launchCwd(env: NodeJS.ProcessEnv = process.env): string | null {
  const value = env.INDUS_LAUNCH_CWD ?? env.PWD;
  return value && value.trim() !== "" ? value : null;
}

/** The session folder out of a `session.info` payload. */
export function sessionCwd(info: Record<string, unknown> | null): string | null {
  const cwd = info?.cwd;
  return typeof cwd === "string" && cwd !== "" ? cwd : null;
}

/** Skills are flagged by `kind` when the bridge sends one, else by a meta starting "skill". */
export function isSkillItem(item: { kind?: unknown; meta?: unknown }): boolean {
  if (typeof item.kind === "string") return item.kind === "skill";
  return typeof item.meta === "string" && /^skill\b/i.test(item.meta);
}

/** Replace from `replaceFrom` with the row; no doubled slash when the text already has one. */
export function applyCompletion(value: string, rowText: string, replaceFrom: number): string {
  const piece =
    value[replaceFrom - 1] === "/" && rowText.startsWith("/") ? rowText.slice(1) : rowText;
  return value.slice(0, replaceFrom) + piece;
}

/**
 * What Enter does with a highlighted row: the new text when taking it changes what was typed,
 * or null to submit (a change of trailing spaces only, or a command typed in full and known).
 */
export function completionOnEnter(
  value: string,
  row: CompletionRow | undefined,
  replaceFrom: number,
  isKnownCommand: (name: string) => boolean = () => false,
): string | null {
  if (row === undefined || row.disabled === true || row.text === "") return null;
  const typed = value.trim();
  if (/^\/[\w-]+$/.test(typed) && isKnownCommand(typed.slice(1))) return null;
  const next = applyCompletion(value, row.text, replaceFrom);
  return next.trimEnd() === value.trimEnd() ? null : next;
}

function toRows(reply: unknown, skillsOnly: boolean): CompletionRow[] {
  const items = (reply as { items?: unknown } | null)?.items;
  if (!Array.isArray(items)) return [];
  const rows: CompletionRow[] = [];
  for (const raw of items) {
    if (typeof raw !== "object" || raw === null) continue;
    const item = raw as { text?: unknown; display?: unknown; meta?: unknown; kind?: unknown };
    if (typeof item.text !== "string") continue;
    if (skillsOnly && !isSkillItem(item)) continue;
    rows.push({
      text: item.text,
      display: typeof item.display === "string" && item.display !== "" ? item.display : item.text,
      ...(typeof item.meta === "string" && item.meta !== "" ? { meta: item.meta } : {}),
    });
  }
  return rows;
}

export interface CompletionDeps {
  readonly rpc: Rpc;
  readonly composer: Composer;
  readonly clock: Clock;
  context(): CompletionContext;
  /** A blocking card is open: no completions. */
  blocked(): boolean;
}

/** Watches the composer text and keeps its completion list current. */
export class CompletionController {
  private seen: string | null = null;
  private timer: unknown = null;
  private readonly deps: CompletionDeps;

  constructor(deps: CompletionDeps) {
    this.deps = deps;
  }

  /** Call on every composer change (cheap when the text did not change). */
  update(input: string = this.deps.composer.text): void {
    const { composer, clock } = this.deps;
    if (this.deps.blocked()) {
      this.seen = null;
      this.cancel();
      composer.clearCompletion();
      return;
    }
    if (input === this.seen) return;
    this.seen = input;
    this.cancel();
    const plan = planCompletion(input, this.deps.context());
    if (plan === null) {
      composer.clearCompletion();
      return;
    }
    this.timer = clock.setTimeout(() => {
      this.timer = null;
      void this.ask(input, plan);
    }, COMPLETION_DEBOUNCE_MS);
  }

  dispose(): void {
    this.cancel();
  }

  private cancel(): void {
    if (this.timer !== null) this.deps.clock.clearTimeout(this.timer);
    this.timer = null;
  }

  private async ask(input: string, plan: CompletionPlan): Promise<void> {
    if (this.seen !== input) return;
    const { composer } = this.deps;
    let reply: unknown;
    try {
      reply = await this.deps.rpc.call(plan.method, plan.params);
    } catch (error) {
      if (this.seen !== input) return;
      const meta =
        error instanceof Error && error.message ? error.message : "the bridge did not answer";
      composer.setCompletion({
        items: [{ text: "", display: "completions unavailable", meta, disabled: true }],
        index: 0,
        replaceFrom: plan.replaceFrom,
      });
      return;
    }
    if (this.seen !== input) return; // the text moved on while we waited: stale
    const skillsOnly = plan.method === "complete.slash" && plan.skillsOnly;
    const serverFrom = (reply as { replace_from?: unknown } | null)?.replace_from;
    const replaceFrom =
      plan.method === "complete.slash" && !plan.skillsOnly && typeof serverFrom === "number"
        ? serverFrom
        : plan.replaceFrom;
    composer.setCompletion({ items: toRows(reply, skillsOnly), index: 0, replaceFrom });
  }
}
