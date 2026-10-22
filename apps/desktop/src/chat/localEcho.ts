// The person's own message on screen at once (merge of P20-18 and P20-19). The daemon sends no
// event that carries a prompt's text, so the composer adds an optimistic row to the transcript
// before `prompt.submit` and settles it with the answer: bound to its turn when the turn started
// (the stored row replaces it on the next history load), removed otherwise — a queued input is
// shown by the queue panel, steering joins the running turn, a refused or uncertain one stays in
// the composer's own strip.
//
// The submit answer can arrive before `turn.started` (W48-c): the answer names the input by its
// admission id, the transcript holds the turn by the daemon's own id. The row is then bound to the
// admission id first and bound again once `turn.started` names the turn, so the row and its reply
// stay one group, also across a history reload.

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
    const { turnId, generation } = actions.snapshot();
    if (outcome === "started" && turnId !== null) {
      rows.bindLocalUser(sessionId, localId, turnId);
      if (typeof generation === "number" && actions.namedTurn(generation) === null) {
        bindWhenNamed(actions, rows, localId, generation);
      }
    } else {
      rows.dropLocalUser(sessionId, localId);
    }
  }
  return outcome;
}

/** Bind the row to the turn's own id once `turn.started` names `generation`; give up when that
 * turn ends unnamed or a later one runs. */
function bindWhenNamed(
  actions: ChatActions,
  rows: LocalRows,
  localId: string,
  generation: number,
): void {
  const stop = actions.subscribe(() => {
    const named = actions.namedTurn(generation);
    if (named !== null) {
      stop();
      rows.bindLocalUser(actions.sessionId, localId, named);
      return;
    }
    const now = actions.snapshot();
    if (!now.running || (now.generation !== null && now.generation !== generation)) stop();
  });
}
