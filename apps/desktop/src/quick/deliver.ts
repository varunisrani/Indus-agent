// The receiving end of quick entry (P20-35): main hands an app window `{ requestId, text,
// sessionId }`; the window submits the text into that session (a new chat when null) through the
// same send path the composer uses — the session's shared `ChatActions` and the optimistic row —
// and answers main with the outcome.

import type { ChatActions, SendOutcome } from "../chat/busyActions.ts";
import { type LocalRows, sendShown } from "../chat/localEcho.ts";
import type { ChatConnection } from "../gateway/connectionStore.ts";

export interface QuickDelivery {
  requestId: string;
  text: string;
  sessionId: string | null;
}

export type DeliveryResult =
  | { ok: true; sessionId: string }
  | { ok: false; sessionId: string | null; error: string };

export interface DeliverDeps {
  connection: ChatConnection;
  rows: LocalRows | null;
  /** The session's one `ChatActions` (shared with its composer). */
  actionsFor(sessionId: string): ChatActions;
  newKey(): string;
  /** A new chat was made for the text (the window may show it). */
  onSessionCreated?(sessionId: string): void;
}

const ACCEPTED: ReadonlySet<SendOutcome> = new Set(["started", "queued", "steered"]);

const PROBLEM: Readonly<Partial<Record<SendOutcome, string>>> = {
  uncertain: "the message was sent but Indus has not confirmed it yet",
  rejected: "Indus refused the message",
  blocked: "the chat is waiting on a lost turn; open it to decide what to do",
  empty: "the message was empty",
};

export async function deliverQuickText(
  deps: DeliverDeps,
  delivery: QuickDelivery,
): Promise<DeliveryResult> {
  const text = delivery.text.trim();
  if (text === "") return { ok: false, sessionId: delivery.sessionId, error: PROBLEM.empty ?? "" };
  let sessionId = delivery.sessionId;
  if (sessionId === null) {
    try {
      const created = await deps.connection.request("session.create", {
        idempotency_key: deps.newKey(),
      });
      sessionId = created.session_id;
      deps.onSessionCreated?.(sessionId);
    } catch (error) {
      return {
        ok: false,
        sessionId: null,
        error: error instanceof Error ? error.message : "a new chat could not be started",
      };
    }
  }
  let outcome: SendOutcome;
  try {
    outcome = await sendShown(deps.actionsFor(sessionId), deps.rows, text, text);
  } catch (error) {
    return {
      ok: false,
      sessionId,
      error: error instanceof Error ? error.message : "the message could not be sent",
    };
  }
  if (ACCEPTED.has(outcome)) return { ok: true, sessionId };
  return { ok: false, sessionId, error: PROBLEM[outcome] ?? "the message could not be sent" };
}
