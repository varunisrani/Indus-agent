// One approval request (P20-22; W52-g look): the command in full and exactly the choices the
// backend allows — Reject · Always allow… ▾ · Run ↵. The menu holds the remaining choices, a
// reject with a reason, and a permanent "always" that asks once more before it applies. A choice is granted only by an explicit activation: a click whose press started
// on that same button, or the keyboard. Dragging onto a button, a press that began elsewhere, or a
// click already in flight when the card appeared (the arming delay) does nothing.

import type { ApprovalChoice, ApprovalRequest } from "@indus/protocol";
import * as Dialog from "@radix-ui/react-dialog";
import { ChevronDown, CornerDownLeft, SquareTerminal } from "lucide-react";
import {
  type FormEvent,
  type KeyboardEvent,
  type MouseEvent,
  type PointerEvent,
  useEffect,
  useRef,
  useState,
} from "react";
import type { OpenServerRequest } from "../../gateway/serverRequests.ts";
import { type MessageKey, useT } from "../../i18n/index.tsx";
import { Button, Menu, type MenuItem } from "../../ui/components.tsx";

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

/** The choice the primary button grants: `once` when offered, else the first non-deny one. */
export function primaryChoice(choices: readonly ApprovalChoice[]): ApprovalChoice | null {
  if (choices.includes("once")) return "once";
  return choices.find((choice) => choice !== "deny") ?? null;
}

function useChoicePress(
  choice: ApprovalChoice,
  armed: boolean,
  onChoose: (choice: ApprovalChoice) => void,
) {
  return useExplicitPress(() => onChoose(choice), armed);
}

export function ApprovalCard(props: {
  open: OpenServerRequest;
  armMs?: number;
  /** Position in the stack when several approvals wait ("2 / 3"). */
  index?: number;
  total?: number;
}) {
  const t = useT();
  const request = props.open.request as ApprovalRequest;
  const choices = allowedChoices(request);
  const armed = useArmed(props.armMs ?? ARM_DELAY_MS);
  const [reason, setReason] = useState("");
  const [asking, setAsking] = useState(false);
  const [confirmAlways, setConfirmAlways] = useState(false);
  const host = useRevealOnOpen<HTMLElement>();

  const choose = (choice: ApprovalChoice) => {
    const trimmed = reason.trim();
    props.open.answer(
      choice === "deny" && trimmed !== ""
        ? { kind: "approval", choice, reason: trimmed }
        : { kind: "approval", choice },
    );
  };

  const primary = primaryChoice(choices);
  const runPress = useChoicePress(primary ?? "once", armed, choose);
  const rejectPress = useChoicePress("deny", armed, choose);
  const sendRejectPress = useExplicitPress(() => choose("deny"), armed);

  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.target instanceof HTMLInputElement) return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const choice = choices.find((c) => SHORTCUT[c] === event.key.toLowerCase());
    if (choice === undefined) return;
    event.preventDefault();
    choose(choice);
  };

  const later = (run: () => void) => () => {
    if (armed) run();
  };
  const menuItems: MenuItem[] = [
    ...choices
      .filter((choice) => choice !== "deny" && choice !== primary)
      .map(
        (choice): MenuItem => ({
          id: choice,
          label: t(LABEL[choice]),
          data: { "data-choice": choice },
          run: later(() => (choice === "always" ? setConfirmAlways(true) : choose(choice))),
        }),
      ),
    {
      id: "reason",
      label: t("approval.rejectWithReason"),
      run: () => setAsking(true),
    },
    {
      id: "deny",
      label: t("approval.reject"),
      tone: "danger",
      data: { "data-choice": "deny" },
      run: later(() => choose("deny")),
    },
  ];

  const command = request.command?.trim() ?? "";
  const description = request.description?.trim() ?? "";
  const counted = (props.total ?? 1) > 1;

  return (
    <section
      ref={host}
      aria-label={t("approval.title")}
      data-card="approval"
      data-request-id={props.open.id}
      className="approval-card"
      onKeyDown={onKeyDown}
    >
      <header className="approval-head">
        <SquareTerminal aria-hidden="true" size={14} />
        <span className="approval-kind">
          {t(command !== "" ? "approval.command" : "approval.commandDetails")}
        </span>
        {command !== "" && description !== "" && (
          <span className="approval-why">{description}</span>
        )}
        {counted && (
          <span className="approval-counter">
            {t("approval.counter", { index: (props.index ?? 0) + 1, total: props.total ?? 1 })}
          </span>
        )}
      </header>
      <pre className="approval-command">{command !== "" ? command : description}</pre>
      {asking && (
        <form
          className="approval-reason"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            if (armed) choose("deny");
          }}
        >
          <input
            type="text"
            className="desk-input"
            aria-label={t("approval.denyReason")}
            placeholder={t("approval.denyReason")}
            value={reason}
            // biome-ignore lint/a11y/noAutofocus: the person just asked to type a reason
            autoFocus
            onChange={(event) => setReason(event.target.value)}
          />
          <Button variant="danger" size="sm" {...sendRejectPress}>
            {t("approval.sendReject")}
          </Button>
        </form>
      )}
      <div className="approval-actions">
        <button
          type="button"
          className="approval-reject"
          data-choice="deny"
          aria-keyshortcuts="Escape d"
          {...rejectPress}
        >
          {t("approval.reject")}
        </button>
        <Menu
          label={t("approval.moreChoices")}
          side="down"
          align="end"
          triggerClassName="approval-more"
          trigger={
            <>
              <span>{t("approval.alwaysMenu")}</span>
              <ChevronDown aria-hidden="true" size={12} />
            </>
          }
          items={menuItems}
        />
        {primary !== null && (
          <button
            type="button"
            className="approval-run"
            data-choice={primary}
            aria-keyshortcuts={`Enter ${SHORTCUT[primary]}`}
            {...runPress}
          >
            {primary === "once" ? t("approval.run") : t(LABEL[primary])}
            <CornerDownLeft aria-hidden="true" size={12} className="approval-enter" />
          </button>
        )}
      </div>
      {props.open.replayed ? <p className="approval-note">{t("approval.replayed")}</p> : null}
      <Dialog.Root open={confirmAlways} onOpenChange={setConfirmAlways}>
        <Dialog.Portal>
          <Dialog.Overlay className="overlay-backdrop" />
          <Dialog.Content className="approval-confirm" aria-describedby={undefined}>
            <Dialog.Title className="approval-confirm-title">
              {t("approval.alwaysConfirmTitle")}
            </Dialog.Title>
            <p className="approval-confirm-body">{t("approval.alwaysConfirmBody")}</p>
            <pre className="approval-command approval-confirm-command">
              {command !== "" ? command : description}
            </pre>
            <div className="approval-actions">
              <Dialog.Close asChild>
                <Button variant="ghost" size="sm">
                  {t("approval.alwaysConfirmCancel")}
                </Button>
              </Dialog.Close>
              <Button
                variant="danger"
                size="sm"
                data-choice="always"
                onClick={() => {
                  setConfirmAlways(false);
                  choose("always");
                }}
              >
                {t("approval.alwaysConfirmOk")}
              </Button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </section>
  );
}
