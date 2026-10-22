// A clarify batch (P20-22): one card for every question, each answered and locked on its own with
// `clarify.lock`. A locked question is read-only; once the daemon answers that none remain, the
// request is complete and settles from the daemon's side (the store then drops the card). Without
// a connection to lock through, the whole batch is answered at once when the last question is set.

import type { ClarifyQuestion, ClarifyRequest } from "@indus/protocol";
import { useState } from "react";
import type { OpenServerRequest } from "../../gateway/serverRequests.ts";
import { useT } from "../../i18n/index.tsx";
import { errorMessage } from "../rpcErrors.ts";
import { useArmed, useExplicitPress, useRevealOnOpen } from "./ApprovalCard.tsx";

/** Lock one answer; resolves with the question ids still open. */
export type ClarifyLock = (questionId: string, answer: string[]) => Promise<string[]>;

function QuestionRow(props: {
  question: ClarifyQuestion;
  locked: string[] | undefined;
  armed: boolean;
  onLock: (answer: string[]) => void;
}) {
  const t = useT();
  const { question, locked } = props;
  const [picked, setPicked] = useState<string[]>([]);
  const [text, setText] = useState("");
  const choices = question.choices ?? [];
  const multi = question.multi_select === true;
  const answer =
    choices.length > 0
      ? [...picked, ...(text.trim() ? [text.trim()] : [])]
      : [text.trim()].filter(Boolean);
  const send = useExplicitPress(() => props.onLock(answer), props.armed);
  const skip = useExplicitPress(() => props.onLock([]), props.armed);

  if (locked !== undefined) {
    return (
      <li data-question={question.id} data-locked="true" className="text-sm">
        <div className="font-medium">{question.text}</div>
        <div className="text-muted">
          {locked.length > 0 ? locked.join(", ") : t("clarify.skipped")}
        </div>
      </li>
    );
  }

  const toggle = (choice: string) => {
    if (multi) {
      setPicked((now) =>
        now.includes(choice) ? now.filter((c) => c !== choice) : [...now, choice],
      );
    } else {
      setPicked((now) => (now[0] === choice ? [] : [choice]));
    }
  };

  return (
    <li data-question={question.id} className="space-y-1 text-sm">
      <div className="font-medium">{question.text}</div>
      {choices.length > 0 ? (
        <fieldset className="m-0 flex flex-wrap gap-1 border-0 p-0" aria-label={question.text}>
          {choices.map((choice) => (
            <button
              key={choice}
              type="button"
              aria-pressed={picked.includes(choice)}
              className={`rounded border px-2 py-0.5 ${
                picked.includes(choice) ? "border-accent bg-accent text-accent-fg" : "border-line"
              }`}
              onClick={() => toggle(choice)}
            >
              {choice}
            </button>
          ))}
        </fieldset>
      ) : null}
      <input
        type="text"
        aria-label={t("clarify.answerFor", { question: question.text })}
        placeholder={choices.length > 0 ? t("clarify.other") : t("clarify.answer")}
        className="w-full rounded border border-line bg-bg px-2 py-1"
        value={text}
        onChange={(event) => setText(event.target.value)}
      />
      <div className="flex gap-2">
        <button
          type="button"
          className="rounded border border-line px-2 py-0.5"
          disabled={answer.length === 0}
          {...send}
        >
          {t("clarify.send")}
        </button>
        <button type="button" className="rounded border border-line px-2 py-0.5" {...skip}>
          {t("clarify.skip")}
        </button>
      </div>
    </li>
  );
}

export function ClarifyCard(props: {
  open: OpenServerRequest;
  lock: ClarifyLock | null;
  armMs?: number;
}) {
  const t = useT();
  const request = props.open.request as ClarifyRequest;
  const armed = useArmed(props.armMs ?? 250);
  const [locked, setLocked] = useState<Record<string, string[]>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const host = useRevealOnOpen<HTMLElement>();

  const lockOne = async (questionId: string, answer: string[]) => {
    if (busy.has(questionId)) return;
    setError(null);
    if (props.lock === null) {
      const next = { ...locked, [questionId]: answer };
      setLocked(next);
      if (request.questions.every((q) => q.id in next)) {
        props.open.answer({ kind: "clarify", answers: next });
      }
      return;
    }
    setBusy((now) => new Set(now).add(questionId));
    try {
      await props.lock(questionId, answer);
      setLocked((now) => ({ ...now, [questionId]: answer }));
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy((now) => {
        const next = new Set(now);
        next.delete(questionId);
        return next;
      });
    }
  };

  return (
    <section
      ref={host}
      aria-label={t("clarify.title")}
      data-card="clarify"
      data-request-id={props.open.id}
      className="rounded-lg border border-accent bg-raised p-3 space-y-2"
    >
      <header className="text-sm font-semibold">{t("clarify.title")}</header>
      <ol className="space-y-3 list-none p-0 m-0">
        {request.questions.map((question) => (
          <QuestionRow
            key={question.id}
            question={question}
            locked={locked[question.id]}
            armed={armed}
            onLock={(answer) => void lockOne(question.id, answer)}
          />
        ))}
      </ol>
      {error !== null ? (
        <p role="alert" className="text-xs text-danger">
          {error}
        </p>
      ) : null}
    </section>
  );
}
