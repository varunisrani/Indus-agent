// Yes/no card asked before destructive local commands.

import { Box, Text, useInput } from "ink";
import type { ReactNode } from "react";
import { isCtrl, type KeyFlags } from "./keys.ts";
import type { ConfirmPayload } from "./payloads.ts";
import { useTones } from "./tones.ts";

export type ConfirmStep = "confirm" | "cancel" | "none";

export function confirmStep(input: string, key: KeyFlags): ConfirmStep {
  if (key.escape || isCtrl(input, key, "c")) return "cancel";
  if (key.ctrl || key.meta) return "none";
  const letter = input.toLowerCase();
  if (key.return || letter === "y") return "confirm";
  if (letter === "n") return "cancel";
  return "none";
}

export interface ConfirmCardProps {
  request: Omit<ConfirmPayload, "confirm" | "cancel">;
  onConfirm(): void;
  onCancel(): void;
  active?: boolean;
}

export function ConfirmCard({
  request,
  onConfirm,
  onCancel,
  active = true,
}: ConfirmCardProps): ReactNode {
  const tone = useTones();
  useInput(
    (input, key) => {
      const step = confirmStep(input, key);
      if (step === "confirm") onConfirm();
      else if (step === "cancel") onCancel();
    },
    { isActive: active },
  );
  const edge = request.danger ? tone.error : tone.warn;
  return (
    <Box borderStyle="round" borderColor={edge} flexDirection="column" paddingX={1}>
      <Text bold color={edge}>
        {request.danger ? "! " : ""}
        {request.title}
      </Text>
      {request.detail ? <Text color={tone.text}>{request.detail}</Text> : null}
      <Text color={tone.muted}>
        Enter/y {request.confirmLabel ?? "go ahead"} · Esc/n{" "}
        {request.cancelLabel ?? "keep things as they are"}
      </Text>
    </Box>
  );
}
