// One approval request (P20-22): the command in full, the reason, and exactly the choices the
// backend allows. A choice is granted only by an explicit activation: a click whose press started
// on that same button, or the keyboard. Dragging onto a button, a press that began elsewhere, or a
// click already in flight when the card appeared (the arming delay) does nothing.

import type { ApprovalChoice, ApprovalRequest } from "@indus/protocol";
import {
  type KeyboardEvent,
  type MouseEvent,
  type PointerEvent,
  useEffect,
  useRef,
  useState,
} from "react";
import type { OpenServerRequest } from "../../gateway/serverRequests.ts";
import { type MessageKey, useT } from "../../i18n/index.tsx";

/** Clicks within this long after a card appears are ignored. */
export const ARM_DELAY_MS = 250;

const ORDER: readonly ApprovalChoice[] = ["once", "session", "always", "deny"];
const SHORTCUT: Readonly<Record<ApprovalChoice, string>> = {
  once: "o",
  session: "s",
  always: "a",
  deny: "d",
};
const LABEL: Readonly<Record<ApprovalChoice, MessageKey>> = {
  once: "approval.once",
  session: "approval.session",
  always: "approval.always",
  deny: "approval.deny",
};

/**
 * The backend's choices in its order, `deny` always present. A request that names none offers
 * once / session / deny: a permanent "always" is never offered unless the backend lists it.
 */
export function allowedChoices(request: ApprovalRequest): ApprovalChoice[] {
  const offered: readonly string[] =
    request.choices && request.choices.length > 0 ? request.choices : ["once", "session"];
  const known = offered.filter((choice): choice is ApprovalChoice =>
    (ORDER as readonly string[]).includes(choice),
  );
  return known.includes("deny") ? known : [...known, "deny"];
}

/** Arms after `armMs`; returns whether a click now may count. */
export function useArmed(armMs: number): boolean {
  const [armed, setArmed] = useState(armMs <= 0);
  useEffect(() => {
    if (armMs <= 0) return;
    const timer = setTimeout(() => setArmed(true), armMs);
    return () => clearTimeout(timer);
  }, [armMs]);
  return armed;
}

/**
 * Bring a card into view once, when it opens (W48-c). It is rendered after the transcript's rows
 * and can open below an expanded tool row, out of sight; "nearest" scrolls only as far as needed.
 */
export function useRevealOnOpen<T extends HTMLElement>() {
  const host = useRef<T>(null);
  useEffect(() => {
    host.current?.scrollIntoView?.({ block: "nearest" });
  }, []);
  return host;
}

/** Button props that fire `onActivate` only for an explicit click or keyboard activation. */
export function useExplicitPress(onActivate: () => void, armed: boolean) {
  const pressedHere = useRef(false);
  return {
    onPointerDown: (event: PointerEvent<HTMLButtonElement>) => {
      pressedHere.current = event.button === 0;
    },
    onPointerLeave: () => {
      pressedHere.current = false;
    },
    onClick: (event: MouseEvent<HTMLButtonElement>) => {
      const keyboard = event.detail === 0;
      const fromHere = pressedHere.current;
      pressedHere.current = false;
      if (!armed) return;
      if (keyboard || fromHere) onActivate();
    },
  };
}

function ChoiceButton(props: {
  choice: ApprovalChoice;
  armed: boolean;
  onChoose: (choice: ApprovalChoice) => void;
}) {
  const t = useT();
  const press = useExplicitPress(() => props.onChoose(props.choice), props.armed);
  const danger = props.choice === "deny";
  return (
    <button
      type="button"
      data-choice={props.choice}
      aria-keyshortcuts={SHORTCUT[props.choice]}
      className={`rounded px-3 py-1 text-sm border ${
        danger ? "border-danger text-danger" : "border-line"
      } hover:bg-raised focus-visible:outline-2 focus-visible:outline-focus`}
      {...press}
    >
      {t(LABEL[props.choice])}
    </button>
  );
}

export function ApprovalCard(props: { open: OpenServerRequest; armMs?: number }) {
  const t = useT();
  const request = props.open.request as ApprovalRequest;
  const choices = allowedChoices(request);
  const armed = useArmed(props.armMs ?? ARM_DELAY_MS);
  const [reason, setReason] = useState("");
  const host = useRevealOnOpen<HTMLElement>();

  const choose = (choice: ApprovalChoice) => {
    const trimmed = reason.trim();
    props.open.answer(
      choice === "deny" && trimmed !== ""
        ? { kind: "approval", choice, reason: trimmed }
        : { kind: "approval", choice },
    );
  };

  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.target instanceof HTMLInputElement) return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const choice = choices.find((c) => SHORTCUT[c] === event.key.toLowerCase());
    if (choice === undefined) return;
    event.preventDefault();
    choose(choice);
  };

  return (
    <section
      ref={host}
      aria-label={t("approval.title")}
      data-card="approval"
      data-request-id={props.open.id}
      className="rounded-lg border border-warn bg-raised p-3 space-y-2"
      onKeyDown={onKeyDown}
    >
      <header className="text-sm font-semibold">{t("approval.title")}</header>
      <p className="text-sm text-muted">{request.description}</p>
      <pre className="font-mono text-xs whitespace-pre-wrap break-all select-text bg-code rounded p-2">
        {request.command}
      </pre>
      <div className="flex flex-wrap items-center gap-2">
        {choices.map((choice) => (
          <ChoiceButton key={choice} choice={choice} armed={armed} onChoose={choose} />
        ))}
        <input
          type="text"
          aria-label={t("approval.denyReason")}
          placeholder={t("approval.denyReason")}
          className="flex-1 min-w-32 rounded border border-line bg-bg px-2 py-1 text-sm"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
        />
      </div>
      {props.open.replayed ? <p className="text-xs text-muted">{t("approval.replayed")}</p> : null}
    </section>
  );
}
