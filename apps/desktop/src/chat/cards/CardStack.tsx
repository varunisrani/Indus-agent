// The transcript-level card stack (P20-22): the ONE host for a session's open server requests.
// The transcript mounts it once, after its rows, so a request that arrives before its tool row and
// the tool row that appears later never remount a card: each card is keyed by its request id in a
// container whose own position in the tree does not change.
//
// Kinds the 1.0 app does not implement have no handler in the server-request store, so the client
// channel answers them `-32601` at once and the daemon offers them elsewhere.

import { useMemo } from "react";
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

  return (
    <section
      data-card-stack={props.sessionId}
      aria-live="polite"
      aria-label={t("cards.label")}
      className="space-y-2"
    >
      {cards.map((open) => {
        switch (open.kind) {
          case "approval":
            return <ApprovalCard key={open.id} open={open} {...armProps(props.armMs)} />;
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
