// Pure state machine for the clarify card: which question is open, the cursor, typed text, and the
// answers already locked on the server.

import { digitOf, isCtrl, isErase, type KeyFlags, typedText } from "./keys.ts";
import type { ClarifyQuestionView } from "./payloads.ts";

export interface ClarifyState {
  /** Index of the open question. */
  active: number;
  /** Row under the cursor; `choices.length` is the free-text row. */
  cursor: number;
  typing: boolean;
  draft: string;
  /** Toggled choices of the open multi-select question. */
  picked: string[];
  /** Answers the server has locked, by question id. */
  locked: Record<string, string[]>;
  /** A lock call is in flight; keys other than cancel wait. */
  waiting: boolean;
  error: string | null;
}

export type ClarifyEffect =
  | { type: "lock"; questionId: string; answer: string[] }
  | { type: "cancel" }
  | null;

export interface ClarifyResult {
  state: ClarifyState;
  effect: ClarifyEffect;
}

const freeText = (question: ClarifyQuestionView | undefined): boolean =>
  (question?.choices.length ?? 0) === 0;

/** Cursor, text and toggles that show a question as it was answered (or fresh). */
function viewOf(
  question: ClarifyQuestionView | undefined,
  answer: readonly string[] | undefined,
): Pick<ClarifyState, "cursor" | "typing" | "draft" | "picked"> {
  const fresh = { cursor: 0, typing: freeText(question), draft: "", picked: [] as string[] };
  if (question === undefined || answer === undefined || answer.length === 0) return fresh;
  const known = answer.filter((value) => question.choices.includes(value));
  const extra = answer.filter((value) => !question.choices.includes(value)).join(", ");
  if (freeText(question)) return { ...fresh, draft: extra };
  if (question.multi) {
    return extra
      ? { cursor: question.choices.length, typing: false, draft: extra, picked: known }
      : { ...fresh, picked: known };
  }
  if (extra) return { cursor: question.choices.length, typing: false, draft: extra, picked: [] };
  return { ...fresh, cursor: Math.max(0, question.choices.indexOf(known[0] ?? "")) };
}

export function initialClarify(questions: readonly ClarifyQuestionView[]): ClarifyState {
  return {
    active: 0,
    ...viewOf(questions[0], undefined),
    locked: {},
    waiting: false,
    error: null,
  };
}

function openQuestion(
  state: ClarifyState,
  questions: readonly ClarifyQuestionView[],
  index: number,
): ClarifyState {
  const question = questions[index];
  return {
    ...state,
    active: index,
    ...viewOf(question, question ? state.locked[question.id] : undefined),
    error: null,
  };
}

const lock = (state: ClarifyState, questionId: string, answer: string[]): ClarifyResult => ({
  state: { ...state, waiting: true, error: null },
  effect: { type: "lock", questionId, answer },
});

const keep = (state: ClarifyState): ClarifyResult => ({ state, effect: null });

const toggle = (picked: readonly string[], choice: string): string[] =>
  picked.includes(choice) ? picked.filter((value) => value !== choice) : [...picked, choice];

/** Apply one key. */
export function clarifyStep(
  state: ClarifyState,
  questions: readonly ClarifyQuestionView[],
  input: string,
  key: KeyFlags,
): ClarifyResult {
  const question = questions[state.active];
  if (isCtrl(input, key, "c")) return { state, effect: { type: "cancel" } };
  if (key.escape) {
    if (state.typing && !freeText(question)) return keep({ ...state, typing: false });
    return { state, effect: { type: "cancel" } };
  }
  if (state.waiting || question === undefined) return keep(state);
  if (key.tab) {
    const count = questions.length;
    const next = (state.active + (key.shift ? count - 1 : 1)) % count;
    return keep(openQuestion(state, questions, next));
  }

  if (state.typing) {
    if (key.return) {
      const text = state.draft.trim();
      const answer = question.multi
        ? [...state.picked, ...(text ? [text] : [])]
        : text
          ? [text]
          : [];
      return lock(state, question.id, answer);
    }
    if (isErase(key)) return keep({ ...state, draft: state.draft.slice(0, -1) });
    if (isCtrl(input, key, "u")) return keep({ ...state, draft: "" });
    const text = typedText(input, key);
    return text ? keep({ ...state, draft: state.draft + text }) : keep(state);
  }

  const { choices } = question;
  const otherRow = choices.length;
  if (key.upArrow) return keep({ ...state, cursor: Math.max(0, state.cursor - 1) });
  if (key.downArrow) return keep({ ...state, cursor: Math.min(otherRow, state.cursor + 1) });
  if (key.return) {
    if (state.cursor === otherRow) return keep({ ...state, typing: true });
    const current = choices[state.cursor];
    if (current === undefined) return keep(state);
    if (question.multi) {
      return lock(state, question.id, state.picked.length > 0 ? [...state.picked] : [current]);
    }
    return lock(state, question.id, [current]);
  }
  if (question.multi && input === " " && !key.ctrl && state.cursor < otherRow) {
    const current = choices[state.cursor];
    return current === undefined
      ? keep(state)
      : keep({ ...state, picked: toggle(state.picked, current) });
  }
  const digit = digitOf(input, key);
  if (digit !== null && digit <= choices.length) {
    const choice = choices[digit - 1] as string;
    if (question.multi) {
      return keep({ ...state, cursor: digit - 1, picked: toggle(state.picked, choice) });
    }
    return lock(state, question.id, [choice]);
  }
  return keep(state);
}

/** The server locked `answer`: record it and open the next question still unanswered. */
export function lockSucceeded(
  state: ClarifyState,
  questions: readonly ClarifyQuestionView[],
  questionId: string,
  answer: readonly string[],
): ClarifyState {
  const locked = { ...state.locked, [questionId]: [...answer] };
  const base = { ...state, locked, waiting: false, error: null };
  const count = questions.length;
  for (let step = 1; step <= count; step += 1) {
    const index = (state.active + step) % count;
    const candidate = questions[index];
    if (candidate && locked[candidate.id] === undefined)
      return openQuestion(base, questions, index);
  }
  return openQuestion(base, questions, state.active);
}

export const lockFailed = (state: ClarifyState, message: string): ClarifyState => ({
  ...state,
  waiting: false,
  error: message,
});

/** Transcript record of a clarify card: each question with its answer, and how it ended. */
export function clarifySummary(
  questions: readonly ClarifyQuestionView[],
  locked: Readonly<Record<string, readonly string[]>>,
  marker: string,
): string {
  const lines = [`Questions from the agent (${marker}):`];
  for (const question of questions) {
    const answer = locked[question.id];
    const shown =
      answer === undefined ? "no answer" : answer.length === 0 ? "skipped" : answer.join(", ");
    lines.push(`  ${question.text} → ${shown}`);
  }
  return lines.join("\n");
}
