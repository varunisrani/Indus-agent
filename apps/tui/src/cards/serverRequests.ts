// The renderer's answers to server requests: each one becomes a card on the overlay store and the
// channel gets a promise that settles when the person decides, or with the kind's refusal when the
// server withdraws the request. Kinds that arrive as id-bearing request frames (desktop-only
// surfaces) have no handler, so the channel answers them -32601 at once (R05.3).

import { declineAnswer, type HandlerTable, type IncomingRequest } from "@indus/client";
import type {
  ApprovalAnswer,
  ClarifyAnswer,
  ClarifyLockResult,
  ClarifyRequest,
  SecretAnswer,
  ServerRequest,
  SudoAnswer,
} from "@indus/protocol";
import type { StoreApi } from "zustand";
import { type CardKind, type OverlayState, overlayStore } from "../state/overlayStore.ts";
import { type UiState, uiStore } from "../state/uiStore.ts";
import { approvalChoices } from "./ApprovalCard.tsx";
import { clarifySummary } from "./clarifyState.ts";
import type {
  ApprovalPayload,
  ClarifyPayload,
  ClarifyQuestionView,
  MaskedPayload,
} from "./payloads.ts";
import { errorText, type RpcLike } from "./rpcLike.ts";

/** The kinds a person answers here (the preview kinds are a desktop window's, never a card). */
export type CardRequestKind = Exclude<ServerRequest["kind"], "preview.act" | "preview.read">;

export const STATUS_TEXT: Readonly<Record<CardRequestKind, string>> = {
  approval: "approval needed",
  clarify: "the agent has a question",
  sudo: "password needed",
  secret: "secret needed",
};

/** Said when a masked card closes because the agent stopped waiting. */
export const WITHDRAWN_LINE = {
  sudo: "The password prompt closed: the agent stopped waiting. The command that needed it did not run.",
  secret: "The secret prompt closed: the agent stopped waiting. Nothing was stored.",
} as const;

export interface ServerRequestDeps {
  rpc: RpcLike;
  ringBell(): void;
  /** `display.bell_on_prompt`; read when a card opens. */
  bellOnPrompt(): boolean;
  /** Append a system line to the transcript. */
  systemLine(text: string): void;
  overlay?: StoreApi<OverlayState>;
  ui?: StoreApi<UiState>;
}

export interface ServerRequestCards {
  /** Install with `RpcChannel.setHandlers`. */
  readonly handlers: HandlerTable;
  /** The clarify tool completed while its card is still open: record it as timed out, close it. */
  flushOpenClarify(): void;
  /** Settle every open or waiting card with its refusal (turn end, interrupt, exit). */
  settleAll(): void;
  /** Requests open or waiting for their turn on screen. */
  readonly openCount: number;
}

interface Entry {
  readonly id: string;
  readonly kind: ServerRequest["kind"];
  /** Settle with the kind's refusal. */
  refuse(): void;
  /** Called when the clarify tool finished with this card still open. */
  abandon?(): void;
  show(): void;
}

/** Usable clarify questions: an id and some text; choices without blanks. */
export function clarifyQuestions(request: ClarifyRequest): ClarifyQuestionView[] {
  const list = Array.isArray(request.questions) ? request.questions : [];
  return list
    .filter(
      (q) =>
        typeof q?.id === "string" &&
        q.id !== "" &&
        typeof q.text === "string" &&
        q.text.trim() !== "",
    )
    .map((q) => ({
      id: q.id,
      text: q.text.trim(),
      choices: (Array.isArray(q.choices) ? q.choices : []).filter(
        (c): c is string => typeof c === "string" && c.trim() !== "",
      ),
      multi: q.multi_select === true,
    }));
}

export function createServerRequestCards(deps: ServerRequestDeps): ServerRequestCards {
  const overlay = deps.overlay ?? overlayStore;
  const ui = deps.ui ?? uiStore;
  const entries: Entry[] = [];
  let shown: Entry | null = null;

  const cardIdOf = (requestId: string): string => `request:${requestId}`;

  const showNext = (): void => {
    if (shown !== null) return;
    const next = entries[0];
    if (next === undefined) return;
    shown = next;
    next.show();
  };

  function ask<A>(
    meta: IncomingRequest,
    kind: CardRequestKind,
    cardKind: CardKind,
    refusal: () => A,
    build: (finish: (answer: A) => void) => unknown,
    withdrawn?: () => void,
  ): Promise<A> {
    return new Promise<A>((resolve) => {
      if (meta.signal.aborted) {
        resolve(refusal());
        return;
      }
      const cardId = cardIdOf(meta.id);
      let settled = false;
      const finish = (answer: A): void => {
        if (settled) return;
        settled = true;
        meta.signal.removeEventListener("abort", onAbort);
        const at = entries.indexOf(entry);
        if (at >= 0) entries.splice(at, 1);
        if (shown === entry) {
          shown = null;
          overlay.getState().close(cardId);
        }
        resolve(answer);
        showNext();
      };
      const onAbort = (): void => {
        if (shown === entry) withdrawn?.();
        finish(refusal());
      };
      const entry: Entry = {
        id: meta.id,
        kind,
        refuse: () => finish(refusal()),
        show: () => {
          overlay.getState().open({
            id: cardId,
            kind: cardKind,
            requestId: meta.id,
            payload: build(finish),
            ownsInput: true,
          });
          ui.getState().patch({ statusText: STATUS_TEXT[kind] });
          if (!meta.replayed && deps.bellOnPrompt()) deps.ringBell();
        },
      };
      if (withdrawn) {
        entry.abandon = () => {
          withdrawn();
          finish(refusal());
        };
      }
      meta.signal.addEventListener("abort", onAbort, { once: true });
      entries.push(entry);
      showNext();
    });
  }

  const handlers: HandlerTable = {
    // No id-bearing request kinds are answered here: the channel refuses them with -32601.
    rpc: {},
    serverRequests: {
      approval: (request, meta) =>
        ask<ApprovalAnswer>(
          meta,
          "approval",
          "approval",
          () => ({ kind: "approval", choice: "deny" }),
          (finish): ApprovalPayload => ({
            command: typeof request.command === "string" ? request.command : "",
            description: typeof request.description === "string" ? request.description : "",
            choices: approvalChoices(request as Parameters<typeof approvalChoices>[0]),
            choose: (choice) => finish({ kind: "approval", choice }),
          }),
        ),

      clarify: (request, meta) => {
        const questions = clarifyQuestions(request);
        if (questions.length === 0) return declineAnswer(request) as ClarifyAnswer;
        const locked: Record<string, string[]> = {};
        // The tool may finish while the last answer's lock is still on its way back; that is an
        // answer, not a timeout, so the verdict waits for the lock's reply.
        let locking = 0;
        let abandoned = false;
        const full = (given: Readonly<Record<string, string[]>>): ClarifyAnswer => ({
          kind: "clarify",
          answers: Object.fromEntries(questions.map((q) => [q.id, [...(given[q.id] ?? [])]])),
        });
        return ask<ClarifyAnswer>(
          meta,
          "clarify",
          "clarify",
          () => full(locked),
          (finish): ClarifyPayload => ({
            questions,
            lock: async (questionId, answer) => {
              locking += 1;
              try {
                const result = (await deps.rpc.call("clarify.lock", {
                  session_id: request.session_id,
                  request_id: request.request_id,
                  question_id: questionId,
                  answer,
                })) as ClarifyLockResult | null;
                locked[questionId] = [...answer];
                const remaining = Array.isArray(result?.remaining) ? result.remaining : [];
                if (remaining.length === 0) {
                  deps.systemLine(clarifySummary(questions, locked, "answered"));
                  finish(full(locked));
                  return { done: true };
                }
                if (abandoned) deps.systemLine(clarifySummary(questions, locked, "timed out"));
                return { done: false };
              } catch (error) {
                if (abandoned) deps.systemLine(clarifySummary(questions, locked, "timed out"));
                return { error: errorText(error) };
              } finally {
                locking -= 1;
              }
            },
            cancel: (given) => {
              Object.assign(locked, given);
              deps.systemLine(clarifySummary(questions, locked, "cancelled"));
              finish(full(locked));
            },
          }),
          () => {
            if (locking > 0) abandoned = true;
            else deps.systemLine(clarifySummary(questions, locked, "timed out"));
          },
        );
      },

      sudo: (request, meta) =>
        ask<SudoAnswer>(
          meta,
          "sudo",
          "sudo",
          () => ({ kind: "sudo", password: null }),
          (finish): MaskedPayload => ({
            title: "Administrator password needed",
            detail: typeof request.reason === "string" ? request.reason : "",
            submit: (value) => finish({ kind: "sudo", password: value }),
          }),
          () => deps.systemLine(WITHDRAWN_LINE.sudo),
        ),

      secret: (request, meta) =>
        ask<SecretAnswer>(
          meta,
          "secret",
          "secret",
          () => ({ kind: "secret", value: null }),
          (finish): MaskedPayload => ({
            title: request.prompt?.trim() || `A value for ${request.name} is needed`,
            detail: `Kept in your secret store as ${request.name}`,
            submit: (value) => finish({ kind: "secret", value }),
          }),
          () => deps.systemLine(WITHDRAWN_LINE.secret),
        ),
    },
  };

  return {
    handlers,
    flushOpenClarify: () => {
      const open = shown;
      if (open?.kind === "clarify") open.abandon?.();
    },
    settleAll: () => {
      for (const entry of [...entries]) entry.refuse();
    },
    get openCount() {
      return entries.length;
    },
  };
}
