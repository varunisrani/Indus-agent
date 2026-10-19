// One masked line for a sudo password or a secret. The typed value lives in a ref only: it is never
// rendered, stored, logged or kept in history.

import { Box, Text, useInput } from "ink";
import { type ReactNode, useRef, useState } from "react";
import { isCtrl, isErase, type KeyFlags, typedText } from "./keys.ts";
import { useTones } from "./tones.ts";

export const MASK_LIMIT = 32;

/** What one key does to the hidden value. */
export type MaskedStep =
  | { type: "submit" }
  | { type: "cancel" }
  | { type: "append"; text: string }
  | { type: "erase" }
  | { type: "clear" }
  | { type: "none" };

export function maskedStep(input: string, key: KeyFlags): MaskedStep {
  if (key.escape || isCtrl(input, key, "c")) return { type: "cancel" };
  if (key.return) return { type: "submit" };
  if (isErase(key)) return { type: "erase" };
  if (isCtrl(input, key, "u")) return { type: "clear" };
  const text = typedText(input, key);
  return text ? { type: "append", text } : { type: "none" };
}

export interface MaskedCardProps {
  title: string;
  detail?: string;
  onSubmit(value: string | null): void;
  active?: boolean;
}

export function MaskedCard({ title, detail, onSubmit, active = true }: MaskedCardProps): ReactNode {
  const tone = useTones();
  const value = useRef("");
  const done = useRef(false);
  const [length, setLength] = useState(0);

  useInput(
    (input, key) => {
      if (done.current) return;
      const step = maskedStep(input, key);
      if (step.type === "submit" || step.type === "cancel") {
        done.current = true;
        const typed = value.current;
        value.current = "";
        setLength(0);
        onSubmit(step.type === "submit" && typed !== "" ? typed : null);
        return;
      }
      if (step.type === "append") value.current += step.text;
      else if (step.type === "erase") value.current = [...value.current].slice(0, -1).join("");
      else if (step.type === "clear") value.current = "";
      else return;
      setLength([...value.current].length);
    },
    { isActive: active },
  );

  const warn = tone.warn;
  return (
    <Box flexDirection="column" borderStyle="round" borderColor={warn} paddingX={1}>
      <Text bold color={warn}>
        {title}
      </Text>
      {detail ? <Text color={tone.muted}>{detail}</Text> : null}
      <Text color={tone.text}>
        {"> "}
        {"•".repeat(Math.min(length, MASK_LIMIT))}
        {length > MASK_LIMIT ? "…" : ""}▏
      </Text>
      <Text color={tone.muted}>Enter sends · Esc sends nothing</Text>
    </Box>
  );
}
