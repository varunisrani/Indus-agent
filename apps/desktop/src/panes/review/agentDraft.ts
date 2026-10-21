// The review pane's two links to the agent (P20-30):
//
// - `draftWithAgent`: "Write it for me" asks the agent for a commit message without adding a turn to
//   the conversation — `prompt.background` runs the prompt in a side session of the current one,
//   and the side session's `message.complete` carries the reply. The side session is subscribed from
//   cursor 0, so a reply that finished before the listener existed is replayed, not lost.
// - `trackTurnBase`: the "Last turn" scope compares against HEAD as it was when the session's last
//   turn started; the pane records HEAD at every `turn.started` of the session.

import type { ChatConnection, SessionEvent } from "../../gateway/connectionStore.ts";

export const DRAFT_TIMEOUT_MS = 120_000;

export type DraftConnection = Pick<ChatConnection, "request" | "onSessionEvent">;

export interface CommitContext {
  diff: string;
  untracked: string[];
  recent: string[];
}

/** The prompt that asks for a commit message (own words; the agent answers with the message only). */
export function commitMessagePrompt(context: CommitContext): string {
  const parts = [
    "Write a git commit message for the changes below. Answer with the message only: a short",
    "subject line (at most 72 characters, imperative mood), then a blank line and a brief body if",
    "the change needs one. Do not run any tools.",
  ];
  if (context.recent.length > 0) {
    parts.push("", "Recent subjects in this repository (match their style):");
    for (const subject of context.recent) parts.push(`- ${subject}`);
  }
  if (context.untracked.length > 0) {
    parts.push("", "New files (no diff shown):");
    for (const file of context.untracked) parts.push(`- ${file}`);
  }
  parts.push("", "Diff:", "```diff", context.diff.trimEnd(), "```");
  return parts.join("\n");
}

/** Strip a fenced block or quotes the agent may have wrapped the message in. */
export function cleanDraft(text: string): string {
  let out = text.trim();
  const fenced = /^```[a-z]*\n([\s\S]*?)\n```$/i.exec(out);
  if (fenced?.[1] !== undefined) out = fenced[1].trim();
  return out;
}

export async function draftWithAgent(
  connection: DraftConnection,
  sessionId: string,
  prompt: string,
  options: {
    timeoutMs?: number;
    setTimeout?: (fn: () => void, ms: number) => unknown;
    clearTimeout?: (handle: unknown) => void;
  } = {},
): Promise<string> {
  const { task_id: taskId } = await connection.request("prompt.background", {
    session_id: sessionId,
    text: prompt,
  });
  const arm = options.setTimeout ?? ((fn, ms) => setTimeout(fn, ms));
  const disarm =
    options.clearTimeout ?? ((handle) => clearTimeout(handle as ReturnType<typeof setTimeout>));
  return new Promise<string>((resolve, reject) => {
    let settled = false;
    const finish = (fn: () => void) => {
      if (settled) return;
      settled = true;
      off();
      disarm(timer);
      fn();
    };
    const off = connection.onSessionEvent(taskId, (event: SessionEvent) => {
      if (event.type !== "message.complete") return;
      const text = cleanDraft((event as { text?: string }).text ?? "");
      finish(() => (text === "" ? reject(new Error("the agent wrote no message")) : resolve(text)));
    });
    const timer = arm(
      () => finish(() => reject(new Error("the agent did not answer in time"))),
      options.timeoutMs ?? DRAFT_TIMEOUT_MS,
    );
    connection
      .request("session.subscribe", { session_id: taskId, cursor: 0 })
      .catch((error: unknown) =>
        finish(() => reject(error instanceof Error ? error : new Error(String(error)))),
      );
  });
}

/**
 * Record HEAD at each `turn.started` of `sessionId` (through `readHead`), and hand it to `onBase`.
 * Answers the stop function.
 */
export function trackTurnBase(
  connection: Pick<ChatConnection, "onSessionEvent">,
  sessionId: string,
  readHead: () => Promise<string | null>,
  onBase: (head: string) => void,
): () => void {
  return connection.onSessionEvent(sessionId, (event) => {
    if (event.type !== "turn.started") return;
    void readHead()
      .then((head) => {
        if (head !== null) onBase(head);
      })
      .catch(() => {});
  });
}
