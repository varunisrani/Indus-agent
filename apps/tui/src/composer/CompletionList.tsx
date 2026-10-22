// The completion rows under the composer (P12-18): at most 16 visible, the window following the
// highlighted row; each row shows its label and a muted note. `rows` is the whole height the list
// may take: when the items do not all fit, one of those rows is the `n/total` count line, and a
// row never wraps (a wrapped or extra line would be squeezed by the fixed-height box and the next
// row drawn over it, leaving the end of the longer row behind).

import { Box, Text } from "ink";
import type { ReactNode } from "react";
import { useTheme } from "../theme/theme.ts";
import { COMPLETION_ROWS } from "./completion.ts";
import { type Composer, composer as sharedComposer, useComposer } from "./composerStore.ts";

export interface CompletionListProps {
  composer?: Composer;
  rows?: number;
}

/** First visible row so that `index` stays inside a window of `size` rows. */
export function windowStart(index: number, count: number, size: number): number {
  if (count <= size) return 0;
  return Math.min(Math.max(0, index - size + 1), count - size);
}

export function CompletionList(props: CompletionListProps): ReactNode {
  const target = useComposer(props.composer ?? sharedComposer);
  const theme = useTheme();
  const { items, index } = target.completion;
  if (items.length === 0) return null;
  const height = Math.max(1, props.rows ?? COMPLETION_ROWS);
  const counted = items.length > height && height > 1;
  const size = counted ? height - 1 : height;
  const start = windowStart(index, items.length, size);
  const visible = items.slice(start, start + size);
  const width = Math.max(...visible.map((row) => row.display.length));
  const accent = theme.color.accent;
  const muted = theme.color["fg-muted"];
  return (
    <Box flexDirection="column">
      {visible.map((row, offset) => {
        const picked = start + offset === index;
        return (
          <Text
            key={`${row.text}\u0000${row.display}`}
            wrap="truncate-end"
            {...(row.disabled ? { dimColor: true } : {})}
          >
            <Text inverse={picked} {...(picked && accent ? { color: accent } : {})}>
              {` ${row.display.padEnd(width)} `}
            </Text>
            {row.meta ? (
              <Text {...(muted ? { color: muted } : { dimColor: true })}> {row.meta}</Text>
            ) : null}
          </Text>
        );
      })}
      {counted ? (
        <Text {...(muted ? { color: muted } : { dimColor: true })}>
          {` ${index + 1}/${items.length}`}
        </Text>
      ) : null}
    </Box>
  );
}
