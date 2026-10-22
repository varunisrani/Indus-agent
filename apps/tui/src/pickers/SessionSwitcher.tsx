// Session switcher card (J13): `/sessions`, Ctrl+X. Lists "+ new", the live sessions of this
// process and the stored ones, refreshes every 1.5 s, and hands the chosen action to the app.

import type { SessionRow } from "@indus/protocol";
import { Box, Text, useInput } from "ink";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { errorText } from "../cards/rpcLike.ts";
import { useTones } from "../cards/tones.ts";
import {
  buildRows,
  initialSwitcher,
  type LiveInput,
  type LiveStatus,
  NEW_ROW_ID,
  REFRESH_MS,
  relativeAge,
  type SwitcherEffect,
  type SwitcherKey,
  type SwitcherRow,
  type SwitcherState,
  switcherStep,
  switcherWidth,
  VISIBLE_ROWS,
  withRows,
} from "./sessionSwitcherState.ts";

/** What the switcher needs from the app (the session lifecycle in production). */
export interface SwitcherOps {
  live(): LiveInput[];
  stored(): Promise<SessionRow[]>;
  currentId(): string | null;
  busy(): boolean;
  open(id: string): void;
  newSession(firstPrompt: string): void;
  closeLive(id: string): Promise<void>;
  remove(id: string): Promise<string>;
}

export interface SessionSwitcherProps {
  ops: SwitcherOps;
  columns: number;
  onClose(): void;
  systemLine(text: string): void;
  active?: boolean;
  /** Refresh period (tests shorten it). */
  refreshMs?: number;
}

const STATUS_MARK: Readonly<Record<LiveStatus, string>> = {
  idle: "○",
  working: "●",
  waiting: "◐",
  starting: "…",
};

function rowText(row: SwitcherRow, draft: string, now: number): { main: string; side: string } {
  switch (row.kind) {
    case "new":
      return { main: draft ? `+ new: ${draft}` : "+ new session", side: "" };
    case "live":
      return {
        main: `${STATUS_MARK[row.status]} ${row.title}${row.current ? " (open)" : ""}`,
        side: [row.model, row.source].filter(Boolean).join(" · "),
      };
    case "stored":
      return {
        main: row.preview ? `${row.title} — ${row.preview}` : row.title,
        side: [relativeAge(row.when, now), row.model, row.source].filter(Boolean).join(" · "),
      };
  }
}

function fit(main: string, side: string, width: number): string {
  const room = Math.max(4, width - side.length - 1);
  const head = main.length > room ? `${main.slice(0, room - 1)}…` : main.padEnd(room, " ");
  return side ? `${head} ${side}` : head;
}

export function SessionSwitcher(props: SessionSwitcherProps): ReactNode {
  const { ops, active = true } = props;
  const tone = useTones();
  const [state, setState] = useState<SwitcherState>(() =>
    initialSwitcher(buildRows(ops.live(), [], ops.currentId())),
  );
  const latest = useRef(state);
  const alive = useRef(true);
  const commit = (next: SwitcherState): void => {
    latest.current = next;
    if (alive.current) setState(next);
  };

  const load = async (): Promise<void> => {
    try {
      const stored = await ops.stored();
      if (!alive.current) return;
      commit(withRows(latest.current, buildRows(ops.live(), stored, ops.currentId())));
    } catch (error) {
      if (alive.current)
        commit({ ...latest.current, notice: `List not loaded: ${errorText(error)}` });
    }
  };

  // biome-ignore lint/correctness/useExhaustiveDependencies: one polling loop per mount
  useEffect(() => {
    alive.current = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = (): void => {
      void load().finally(() => {
        if (alive.current) timer = setTimeout(tick, props.refreshMs ?? REFRESH_MS);
      });
    };
    tick();
    return () => {
      alive.current = false;
      if (timer !== null) clearTimeout(timer);
    };
  }, []);

  const perform = (effect: SwitcherEffect): void => {
    switch (effect.type) {
      case "close":
        props.onClose();
        return;
      case "refresh":
        void load();
        return;
      case "new":
        props.onClose();
        ops.newSession(effect.text);
        return;
      case "open":
        props.onClose();
        ops.open(effect.id);
        return;
      case "close-live":
        void ops.closeLive(effect.id).then(() => load());
        return;
      case "delete":
        void ops.remove(effect.id).then((line) => {
          props.systemLine(line);
          return load();
        });
        return;
      case "none":
        return;
    }
  };

  useInput(
    (input, key) => {
      const name: SwitcherKey["name"] = key.upArrow
        ? "up"
        : key.downArrow
          ? "down"
          : key.return
            ? "return"
            : key.escape
              ? "escape"
              : key.backspace || key.delete
                ? "backspace"
                : key.pageUp
                  ? "pageup"
                  : key.pageDown
                    ? "pagedown"
                    : null;
      const step = switcherStep(
        latest.current,
        { input: name === null ? input : "", name, ctrl: key.ctrl, meta: key.meta },
        ops.busy(),
      );
      commit(step.state);
      perform(step.effect);
    },
    { isActive: active },
  );

  const width = switcherWidth(props.columns);
  const inner = Math.max(10, width - 4);
  const now = Date.now();
  const shown = state.rows.slice(state.offset, state.offset + VISIBLE_ROWS);
  const more = state.rows.length - state.offset - shown.length;

  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor={tone.accent}
      width={width}
      paddingX={1}
    >
      <Text color={tone.accent} bold>
        Sessions
      </Text>
      {shown.map((row) => {
        const selected = row.id === state.selected;
        const { main, side } = rowText(row, row.id === NEW_ROW_ID ? state.draft : "", now);
        const line = fit(main, side, inner - 2);
        const color = row.kind === "stored" ? tone.text : tone.accent;
        return (
          <Text key={row.id} {...(selected ? { inverse: true } : { color })}>
            {selected ? "› " : "  "}
            {line}
          </Text>
        );
      })}
      {more > 0 ? <Text color={tone.muted}>{`  … ${more} more`}</Text> : null}
      {state.notice ? <Text color={tone.warn}>{state.notice}</Text> : null}
      <Text color={tone.muted}>
        ↑↓ move · Enter open · ^N new · ^R refresh · ^D close live · d d delete · Esc
      </Text>
    </Box>
  );
}
