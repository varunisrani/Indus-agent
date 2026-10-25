// The transcript-level card stack (P20-22): the ONE host for a session's open server requests.
// The transcript mounts it once, after its rows, so a request that arrives before its tool row and
// the tool row that appears later never remount a card: each card is keyed by its request id in a
// container whose own position in the tree does not change.
//
// Kinds the 1.0 app does not implement have no handler in the server-request store, so the client
// channel answers them `-32601` at once and the daemon offers them elsewhere.

import type { ApprovalRequest } from "@indus/protocol";
import { type RefObject, useEffect, useMemo, useRef } from "react";
import { type ChatConnection, REQUEST_TIMEOUTS } from "../../gateway/connectionStore.ts";
import { useGatewayConnection, useOpenServerRequests } from "../../gateway/react.tsx";
import type { OpenServerRequest } from "../../gateway/serverRequests.ts";
import { useT } from "../../i18n/index.tsx";
import { ApprovalCard } from "./ApprovalCard.tsx";
import { ClarifyCard, type ClarifyLock } from "./ClarifyCard.tsx";
import { SecretDialog, SudoDialog } from "./SecretDialog.tsx";

/** The server-request kinds this app answers; every other kind is refused by the channel. */
export const CARD_KINDS = ["approval", "clarify", "sudo", "secret"] as const;

export interface CardStackProps {
  sessionId: string;
  /** Open requests; defaults to the server-request store's list for the session. */
  requests?: readonly OpenServerRequest[];
  /** The connection used for clarify locks; defaults to the active one. */
  connection?: ChatConnection | null;
  /** Arming delay for explicit activations (tests pass 0). */
  armMs?: number;
}

function lockFor(connection: ChatConnection | null, open: OpenServerRequest): ClarifyLock | null {
  if (connection === null) return null;
  return async (questionId, answer) => {
    const result = await connection.request(
      "clarify.lock",
      {
        session_id: open.sessionId,
        request_id: open.id,
        question_id: questionId,
        answer,
      },
      { timeoutMs: REQUEST_TIMEOUTS.approvalRespond },
    );
    return result.remaining;
  };
}

/** Whether a key press belongs to a field (then Enter / Escape are the field's, not the card's). */
function inEditable(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  return (
    target.closest("input, textarea, select, [contenteditable='true'], [role='menu']") !== null
  );
}

/** Whether a stack is on screen (a hidden chat tab keeps its stack mounted). */
function shown(element: HTMLElement): boolean {
  return typeof element.checkVisibility === "function" ? element.checkVisibility() : true;
}

/**
 * Enter runs and Escape rejects the first waiting approval (W52-g): one listener per stack, and
 * only the first visible stack with an approval in the window answers (no double answers when
 * several chats are mounted). Focus in another field, a modifier, or an open dialog leaves the
 * key alone.
 */
function useApprovalKeys(stack: RefObject<HTMLElement | null>, active: boolean) {
  useEffect(() => {
    if (!active) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Enter" && event.key !== "Escape") return;
      if (event.defaultPrevented || event.isComposing) return;
      if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
      if (inEditable(event.target)) return;
      if (document.querySelector('[role="dialog"], [role="alertdialog"]') !== null) return;
      const host = stack.current;
      if (host === null) return;
      const first = [...document.querySelectorAll<HTMLElement>("[data-card-stack]")].find(
        (element) => element.querySelector('[data-card="approval"]') !== null && shown(element),
      );
      if (first !== host) return;
      const card = host.querySelector('[data-card="approval"]');
      const button =
        event.key === "Enter"
          ? card?.querySelector<HTMLButtonElement>(".approval-run")
          : card?.querySelector<HTMLButtonElement>(".approval-reject");
      if (button === null || button === undefined) return;
      event.preventDefault();
      button.click(); // detail 0: a keyboard activation (the arming delay still applies)
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [stack, active]);
}

export function CardStack(props: CardStackProps) {
  const t = useT();
  const fromStore = useOpenServerRequests(props.requests === undefined ? props.sessionId : null);
  const active = useGatewayConnection();
  const connection = props.connection === undefined ? active : props.connection;
  const requests = props.requests ?? fromStore;
  const cards = useMemo(
    () => requests.filter((open) => open.sessionId === props.sessionId),
    [requests, props.sessionId],
  );
  const approvals = cards.filter((open) => open.kind === "approval");
  const host = useRef<HTMLElement>(null);
  useApprovalKeys(host, approvals.length > 0);
  const firstApproval = approvals[0];
  const running =
    firstApproval === undefined
      ? ""
      : ((firstApproval.request as ApprovalRequest).command ?? "").replace(/\s+/g, " ").trim();

  return (
    <section
      ref={host}
      data-card-stack={props.sessionId}
      aria-live="polite"
      aria-label={t("cards.label")}
      className="card-stack"
      data-empty={cards.length === 0 || undefined}
    >
      {firstApproval !== undefined && (
        <p className="card-activity" data-ui="approval-activity">
          {running !== "" ? t("approval.running", { command: running }) : t("approval.waiting")}
        </p>
      )}
      {cards.map((open) => {
        switch (open.kind) {
          case "approval":
            return (
              <ApprovalCard
                key={open.id}
                open={open}
                index={approvals.indexOf(open)}
                total={approvals.length}
                {...armProps(props.armMs)}
              />
            );
          case "clarify":
            return (
              <ClarifyCard
                key={open.id}
                open={open}
                lock={lockFor(connection, open)}
                {...armProps(props.armMs)}
              />
            );
          case "sudo":
            return <SudoDialog key={open.id} open={open} />;
          case "secret":
            return <SecretDialog key={open.id} open={open} />;
          default:
            return null;
        }
      })}
    </section>
  );
}

function armProps(armMs: number | undefined): { armMs?: number } {
  return armMs === undefined ? {} : { armMs };
}
