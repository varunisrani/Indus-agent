// Approval card: the redacted command wrapped to the card, numbered choices, digits to pick.

import type { ApprovalChoice } from "@indus/protocol";
import { Box, Text, useInput } from "ink";
import { type ReactNode, useState } from "react";
import { digitOf, isCtrl, type KeyFlags } from "./keys.ts";
import type { ApprovalPayload } from "./payloads.ts";
import { wrapText } from "./text.ts";
import { useTones } from "./tones.ts";

/** Canonical order; numbering always follows it. */
export const CHOICE_ORDER: readonly ApprovalChoice[] = ["once", "session", "always", "deny"];
export const PREVIEW_LINES = 10;

export const CHOICE_LABEL: Readonly<Record<ApprovalChoice, string>> = {
  once: "Allow once",
  session: "Allow for this session",
  always: "Always allow",
  deny: "Deny",
};

export interface ApprovalOffer {
  choices?: readonly string[] | null;
  smart_denied?: unknown;
  allow_permanent?: unknown;
}

/** The choices a request allows, in canonical order; deny is always present. */
export function approvalChoices(offer: ApprovalOffer): ApprovalChoice[] {
  if (Array.isArray(offer.choices) && offer.choices.length > 0) {
    const allowed = new Set<string>([...offer.choices, "deny"]);
    return CHOICE_ORDER.filter((choice) => allowed.has(choice));
  }
  if (offer.smart_denied === true) return ["once", "deny"];
  if (offer.allow_permanent === false) return CHOICE_ORDER.filter((c) => c !== "always");
  return [...CHOICE_ORDER];
}

/** Wrapped command rows for a terminal `columns` wide: the first ten and how many are hidden. */
export function commandPreview(
  command: string,
  columns: number,
): { rows: string[]; hidden: number } {
  const all = wrapText(command, Math.max(20, columns - 8));
  return { rows: all.slice(0, PREVIEW_LINES), hidden: Math.max(0, all.length - PREVIEW_LINES) };
}

export type ApprovalStep =
  | { type: "choose"; choice: ApprovalChoice }
  | { type: "move"; index: number }
  | { type: "none" };

/** One key on the approval card (R05.11: digits pick, letters do nothing). */
export function approvalStep(
  input: string,
  key: KeyFlags,
  index: number,
  choices: readonly ApprovalChoice[],
): ApprovalStep {
  if (key.escape || isCtrl(input, key, "c")) return { type: "choose", choice: "deny" };
  const digit = digitOf(input, key);
  if (digit !== null) {
    const picked = choices[digit - 1];
    return picked ? { type: "choose", choice: picked } : { type: "none" };
  }
  if (key.return) {
    const current = choices[index];
    return current ? { type: "choose", choice: current } : { type: "none" };
  }
  if (key.upArrow && index > 0) return { type: "move", index: index - 1 };
  if (key.downArrow && index < choices.length - 1) return { type: "move", index: index + 1 };
  return { type: "none" };
}

export interface ApprovalCardProps {
  request: Pick<ApprovalPayload, "command" | "description" | "choices">;
  columns: number;
  onChoose(choice: ApprovalChoice): void;
  active?: boolean;
}

export function ApprovalCard({
  request,
  columns,
  onChoose,
  active = true,
}: ApprovalCardProps): ReactNode {
  const tone = useTones();
  const [index, setIndex] = useState(0);
  const { choices } = request;

  useInput(
    (input, key) => {
      const step = approvalStep(input, key, index, choices);
      if (step.type === "choose") onChoose(step.choice);
      else if (step.type === "move") setIndex(step.index);
    },
    { isActive: active },
  );

  const warn = tone.warn;
  const muted = tone.muted;
  const preview = commandPreview(request.command, columns);
  const title = request.description.trim() || "A command wants to run";

  return (
    <Box borderStyle="double" borderColor={warn} flexDirection="column" paddingX={1}>
      <Text bold color={warn}>
        Needs your approval: {title}
      </Text>
      <Box flexDirection="column" paddingLeft={1} marginY={1}>
        {preview.rows.map((row, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: rows are positional and never reorder
          <Text key={i} color={tone.text} wrap="truncate-end">
            {row === "" ? " " : row}
          </Text>
        ))}
        {preview.hidden > 0 ? (
          <Text color={muted}>
            … {preview.hidden} more {preview.hidden === 1 ? "line" : "lines"}
          </Text>
        ) : null}
      </Box>
      {choices.map((choice, i) => (
        <Text key={choice} color={i === index ? warn : muted} bold={i === index}>
          {i === index ? "› " : "  "}
          {i + 1}. {CHOICE_LABEL[choice]}
        </Text>
      ))}
      <Text color={muted}>↑↓ move · Enter choose · 1-{choices.length} pick · Esc deny</Text>
    </Box>
  );
}
