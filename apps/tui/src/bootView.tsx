// A minimal screen until the app composition (P12-27) lands: brand, status and the transcript's
// newest lines, in the live theme. Ctrl+D or Ctrl+C on it leaves through the normal exit path.

import { Box, Text, useInput } from "ink";
import type { ReactNode } from "react";
import { useTranscript } from "./state/transcriptStore.ts";
import { useTurn } from "./state/turnStore.ts";
import { useUi } from "./state/uiStore.ts";
import { useTheme } from "./theme/theme.ts";

export function BootView(): ReactNode {
  const theme = useTheme();
  const status = useUi((s) => s.statusText);
  const live = useTurn((s) => s.streamBuffer);
  const messages = useTranscript((s) => s.messages);
  const recent = messages.slice(-5);
  useInput((input, key) => {
    if (key.ctrl && (input === "d" || input === "c")) process.exit(input === "c" ? 130 : 0);
  });
  return (
    <Box flexDirection="column" paddingX={1}>
      <Text color={theme.color.accent} bold>
        {theme.brand.name}
      </Text>
      {recent.map((m) => (
        <Text key={m.id} color={m.role === "system" ? theme.color.muted : theme.color.text}>
          {m.text}
        </Text>
      ))}
      {live !== "" && <Text color={theme.color.text}>{live}</Text>}
      <Text color={theme.color.statusFg}>
        {theme.brand.prompt} {status}
      </Text>
    </Box>
  );
}
