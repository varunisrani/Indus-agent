// What each card kind keeps in `Card.payload`. Answers leave through the callbacks; secret values
// are never part of a payload.

import type { ApprovalChoice } from "@indus/protocol";

export interface ApprovalPayload {
  command: string;
  description: string;
  choices: ApprovalChoice[];
  choose(choice: ApprovalChoice): void;
}

export interface ConfirmPayload {
  title: string;
  detail?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  confirm(): void;
  cancel(): void;
}

export interface PagerPayload {
  title?: string;
  lines: string[];
}

export interface ClarifyQuestionView {
  id: string;
  text: string;
  choices: string[];
  multi: boolean;
}

export type LockOutcome = { done: boolean } | { error: string };

export interface ClarifyPayload {
  questions: ClarifyQuestionView[];
  /** Lock one answer (an empty list skips the question). */
  lock(questionId: string, answer: string[]): Promise<LockOutcome>;
  /** Give up on the questions still open; locked answers are kept. */
  cancel(locked: Readonly<Record<string, string[]>>): void;
}

export interface MaskedPayload {
  title: string;
  detail?: string;
  /** The typed value, or null when the person cancelled or typed nothing. */
  submit(value: string | null): void;
}

export interface ModelPickerPayload {
  sessionId: string | null;
  refresh: boolean;
}
