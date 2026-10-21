// The person's own message on screen at once (merge of P20-18 and P20-19). The daemon sends no
// event that carries a prompt's text, so the composer adds an optimistic row to the transcript
// before `prompt.submit` and settles it with the answer: bound to its turn when the turn started
// (the stored row replaces it on the next history load), removed otherwise — a queued input is
// shown by the queue panel, steering joins the running turn, a refused or uncertain one stays in
// the composer's own strip.

import type { ChatActions, SendOptions, SendOutcome } from "./busyActions.ts";

/** The transcript store's local-row calls (`TranscriptStore` implements them). */
export interface LocalRows {
  addLocalUser(sessionId: string, text: string): string;
  bindLocalUser(sessionId: string, localId: string, turnId: string): void;
  dropLocalUser(sessionId: string, localId: string): void;
}

/** `actions.send` with the optimistic row; `display` is what the person typed. */
export async function sendShown(
  actions: ChatActions,
  rows: LocalRows | null,
  text: string,
  display: string,
  options: SendOptions = {},
): Promise<SendOutcome> {
  if (rows === null || text.trim() === "") return actions.send(text, options);
  const sessionId = actions.sessionId;
  const localId = rows.addLocalUser(sessionId, display);
  let outcome: SendOutcome = "rejected";
  try {
    outcome = await actions.send(text, options);
  } finally {
    const turnId = actions.snapshot().turnId;
    if (outcome === "started" && turnId !== null) rows.bindLocalUser(sessionId, localId, turnId);
    else rows.dropLocalUser(sessionId, localId);
  }
  return outcome;
}
