// An approval a member's turn put to a group room (wave 48, RS3), answered with `groups.approve`.
// It offers exactly the choices the request listed (the daemon refuses any other), in the chat
// card's order; a choice counts only for an explicit press after the arming delay (the chat card's
// rules). The member's command and description are its own data (SEC-15): shown verbatim as quoted
// text under the member's name, never rendered as markup or as the person's words. A refused or
// conflicting answer (`rooms.event_conflict`, a request no longer open) stays on the card.

import type { ApprovalChoice } from "@indus/protocol";
import { useState } from "react";
import { ARM_DELAY_MS, useArmed, useExplicitPress } from "../chat/cards/ApprovalCard.tsx";
import { type MessageKey, useT } from "../i18n/index.tsx";
import type { RoomApproval } from "./GroupRoom.tsx";

const ORDER: readonly ApprovalChoice[] = ["once", "session", "always", "deny"];
const LABEL: Readonly<Record<ApprovalChoice, MessageKey>> = {
  once: "approval.once",
  session: "approval.session",
  always: "approval.always",
  deny: "approval.deny",
};

/** The request's own choices that this card knows, in the card's order; nothing is added. */
export function allowedRoomChoices(offered: readonly string[]): ApprovalChoice[] {
  return ORDER.filter((choice) => offered.includes(choice));
}

function Choice(props: {
  choice: ApprovalChoice;
  armed: boolean;
  busy: boolean;
  onChoose: (choice: ApprovalChoice) => void;
}) {
  const t = useT();
  const press = useExplicitPress(() => props.onChoose(props.choice), props.armed && !props.busy);
  return (
    <button
      type="button"
      data-choice={props.choice}
      disabled={props.busy}
      className={`button ${props.choice === "deny" ? "button-secondary" : "button-primary"}`}
      {...press}
    >
      {t(LABEL[props.choice])}
    </button>
  );
}

export function RoomApprovalCard(props: {
  ask: RoomApproval;
  onAnswer: (choice: ApprovalChoice, reason: string) => Promise<void>;
  armMs?: number | undefined;
}) {
  const t = useT();
  const { ask } = props;
  const armed = useArmed(props.armMs ?? ARM_DELAY_MS);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState("");
  const choices = ask.choices.filter((c): c is ApprovalChoice =>
    (ORDER as readonly string[]).includes(c),
  );

  const choose = async (choice: ApprovalChoice) => {
    setBusy(true);
    setRefused("");
    try {
      await props.onAnswer(choice, reason.trim());
    } catch (failure) {
      setRefused(failure instanceof Error ? failure.message : t("manage.failed"));
    } finally {
      setBusy(false);
    }
  };

  const title = t("manage.room.approval.title", { member: ask.member });
  return (
    <fieldset className="manage-card room-approval" data-request={ask.requestId}>
      <legend>{title}</legend>
      {ask.roomName !== "" && (
        <p className="manage-muted">{t("manage.room.approval.room", { room: ask.roomName })}</p>
      )}
      <p className="manage-muted">{t("manage.room.approval.quoted", { member: ask.member })}</p>
      {ask.command !== "" && <pre className="room-approval-command">{ask.command}</pre>}
      {ask.description !== "" && (
        <blockquote className="room-approval-description">{ask.description}</blockquote>
      )}
      {choices.includes("deny") && (
        <input
          aria-label={t("approval.denyReason")}
          value={reason}
          maxLength={2000}
          onChange={(e) => setReason(e.target.value)}
        />
      )}
      <div className="manage-inline">
        {choices.map((choice) => (
          <Choice
            key={choice}
            choice={choice}
            armed={armed}
            busy={busy}
            onChoose={(c) => void choose(c)}
          />
        ))}
        {choices.length === 0 && (
          <span className="manage-muted">{t("manage.room.approval.noChoices")}</span>
        )}
      </div>
      {refused !== "" && (
        <p className="manage-error" role="alert">
          {t("manage.room.approval.refused", { reason: refused })}
        </p>
      )}
    </fieldset>
  );
}
