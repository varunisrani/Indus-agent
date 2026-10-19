// Clarify card: every question listed, one open at a time; each lock goes to the server at once.

import { Box, Text, useInput } from "ink";
import { type ReactNode, useRef, useState } from "react";
import {
  type ClarifyState,
  clarifyStep,
  initialClarify,
  lockFailed,
  lockSucceeded,
} from "./clarifyState.ts";
import type { ClarifyQuestionView, LockOutcome } from "./payloads.ts";
import { useTones } from "./tones.ts";

export interface ClarifyCardProps {
  questions: readonly ClarifyQuestionView[];
  onLock(questionId: string, answer: string[]): Promise<LockOutcome>;
  onCancel(locked: Readonly<Record<string, string[]>>): void;
  active?: boolean;
}

export function ClarifyCard({
  questions,
  onLock,
  onCancel,
  active = true,
}: ClarifyCardProps): ReactNode {
  const tone = useTones();
  const [state, setState] = useState<ClarifyState>(() => initialClarify(questions));
  // Keys can arrive faster than React re-renders; the ref always holds the newest state.
  const latest = useRef(state);
  const commit = (next: ClarifyState): void => {
    latest.current = next;
    setState(next);
  };
  const closed = useRef(false);

  useInput(
    (input, key) => {
      if (closed.current) return;
      const { state: next, effect } = clarifyStep(latest.current, questions, input, key);
      commit(next);
      if (effect?.type === "cancel") {
        closed.current = true;
        onCancel(next.locked);
      } else if (effect?.type === "lock") {
        void onLock(effect.questionId, effect.answer).then(
          (outcome) => {
            if (closed.current) return;
            if ("error" in outcome) {
              commit(lockFailed(latest.current, outcome.error));
              return;
            }
            commit(lockSucceeded(latest.current, questions, effect.questionId, effect.answer));
            if (outcome.done) closed.current = true;
          },
          (error: unknown) => {
            if (!closed.current) commit(lockFailed(latest.current, String(error)));
          },
        );
      }
    },
    { isActive: active },
  );

  const accent = tone.accent;
  const muted = tone.muted;
  const answered = questions.filter((q) => state.locked[q.id] !== undefined).length;
  const open = questions[state.active];
  const lastOpen = questions.length - answered <= 1;

  return (
    <Box flexDirection="column" borderStyle="round" borderColor={accent} paddingX={1}>
      <Text bold color={accent}>
        The agent asks {questions.length === 1 ? "a question" : `${questions.length} questions`}
      </Text>
      {questions.map((question, index) => {
        const locked = state.locked[question.id];
        const isOpen = index === state.active;
        const mark = isOpen ? "›" : locked !== undefined ? "✓" : "·";
        return (
          <Box key={question.id} flexDirection="column">
            <Text bold={isOpen} color={isOpen ? tone.text : muted}>
              {mark} {question.text}
            </Text>
            {locked !== undefined && !isOpen ? (
              <Text color={locked.length > 0 ? tone.ok : muted}>
                {"    "}
                {locked.length > 0 ? locked.join(", ") : "(skipped)"}
              </Text>
            ) : null}
            {isOpen ? <QuestionBody question={question} state={state} /> : null}
          </Box>
        );
      })}
      {state.error ? (
        <Text color={tone.error}>Could not save that answer: {state.error}</Text>
      ) : null}
      <Text color={muted}>
        {answered}/{questions.length} answered ·{" "}
        {state.typing
          ? "type, Enter keeps it (empty skips)"
          : open?.multi
            ? "Space or 1-9 toggle, Enter keeps"
            : "↑↓ move, Enter or 1-9 picks"}{" "}
        · {lastOpen ? "the last answer sends all" : "Tab next question"} · Esc stop asking
      </Text>
    </Box>
  );
}

function QuestionBody({
  question,
  state,
}: {
  question: ClarifyQuestionView;
  state: ClarifyState;
}): ReactNode {
  const tone = useTones();
  const muted = tone.muted;
  const accent = tone.accent;
  const typingRow = (
    <Text color={tone.text}>
      {"    > "}
      {state.draft}
      {state.waiting ? "" : "▏"}
    </Text>
  );
  if (question.choices.length === 0) return typingRow;
  const rows = [...question.choices, "Other (type your own answer)"];
  return (
    <Box flexDirection="column">
      {rows.map((label, row) => {
        const here = row === state.cursor;
        const box =
          question.multi && row < question.choices.length
            ? state.picked.includes(label)
              ? "[x] "
              : "[ ] "
            : "";
        return (
          <Text key={label} color={here ? accent : muted} bold={here}>
            {here ? "  › " : "    "}
            {box}
            {row + 1}. {label}
          </Text>
        );
      })}
      {state.typing || (state.cursor === question.choices.length && state.draft) ? typingRow : null}
    </Box>
  );
}
