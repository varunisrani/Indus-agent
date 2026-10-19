// Spike (P12-01, throwaway): interactive trial app on upstream Ink + React 19.
// Run: pnpm --filter @indus/tui spike:app   (q or Ctrl+C quits; s streams 10k tokens)
// Manual checklist for kitty, iTerm2/WezTerm, Windows Terminal (WSL2) and tmux is in
// docs/notes/P12/ink-spike.md.

import { writeSync } from "node:fs";
import { Box, render, Text, useApp, useInput, usePaste, useWindowSize } from "ink";
import { useEffect, useRef, useState } from "react";
import { InputFilter, type MouseEvent } from "./inputFilter.ts";
import { gateStdout } from "./outputGate.ts";
import { extendedKeyPlan, fullReset, seq, syncOutputWanted } from "./terminalModes.ts";

const env = process.env;
const keyPlan = extendedKeyPlan(env);
const ROWS = Array.from({ length: 200 }, (_, index) => `row ${String(index + 1).padStart(3, "0")}`);
const mouseListeners = new Set<(event: MouseEvent) => void>();
const filter = new InputFilter(process.stdin, (event) => {
  for (const listener of mouseListeners) listener(event);
});
const gate = gateStdout(process.stdout, { syncAllowed: syncOutputWanted(env) });

let unmountInk: (() => void) | undefined;
let modesReset = false;
function resetModes(): void {
  if (modesReset) return;
  modesReset = true;
  try {
    writeSync(1, fullReset());
  } catch {
    // stdout already gone; nothing left to restore
  }
}
process.on("exit", resetModes);
for (const signal of ["SIGTERM", "SIGHUP"] as const) {
  process.on(signal, () => {
    // Unmount first: Ink's own exit hook would otherwise repaint its last frame after we left
    // the alternate screen, leaving it on the main screen.
    unmountInk?.();
    resetModes();
    process.exit(signal === "SIGTERM" ? 143 : 129);
  });
}

function describeKey(input: string, key: Record<string, unknown>): string {
  const mods = ["ctrl", "meta", "shift", "super"].filter((name) => key[name] === true);
  const named = ["return", "backspace", "delete", "escape", "tab", "upArrow", "downArrow"].find(
    (name) => key[name] === true,
  );
  return [...mods, named ?? JSON.stringify(input)].join("+");
}

function useStream(active: boolean): { text: string; tokens: number; done: boolean } {
  const [state, setState] = useState({ text: "", tokens: 0, done: false });
  useEffect(() => {
    if (!active) return;
    let produced = "";
    let tokens = 0;
    const producer = setInterval(() => {
      for (let i = 0; i < 40 && tokens < 10_000; i += 1, tokens += 1) produced += `tok${tokens} `;
    }, 2);
    const flush = setInterval(() => {
      setState({ text: produced.slice(-16_000), tokens, done: tokens >= 10_000 });
      if (tokens >= 10_000) {
        clearInterval(producer);
        clearInterval(flush);
      }
    }, 16);
    return () => {
      clearInterval(producer);
      clearInterval(flush);
    };
  }, [active]);
  return state;
}

function Spike() {
  const { exit } = useApp();
  const { columns, rows } = useWindowSize();
  const [keys, setKeys] = useState<string[]>([]);
  const [pastes, setPastes] = useState<string[]>([]);
  const [offset, setOffset] = useState(0);
  const [selection, setSelection] = useState<[number, number] | null>(null);
  const [streaming, setStreaming] = useState(false);
  const anchor = useRef<number | null>(null);
  const resizes = useRef(0);
  const stream = useStream(streaming);
  const listHeight = Math.max(3, rows - 14);

  const lastSize = useRef("");
  if (lastSize.current !== `${columns}x${rows}`) {
    lastSize.current = `${columns}x${rows}`;
    resizes.current += 1;
  }

  useEffect(() => {
    process.stdout.write(seq.mouseOn + (keyPlan.otherKeys ? seq.otherKeysOn : ""));
    const onMouse = (event: MouseEvent): void => {
      if (event.kind === "wheel-up") setOffset((value) => Math.max(0, value - 3));
      if (event.kind === "wheel-down") setOffset((value) => Math.min(ROWS.length - 1, value + 3));
      const line = offset + event.row - 2;
      if (event.kind === "press" && event.button === 0) {
        anchor.current = line;
        setSelection([line, line]);
      }
      if (event.kind === "drag" && anchor.current !== null) {
        const start = anchor.current;
        setSelection([Math.min(start, line), Math.max(start, line)]);
      }
      if (event.kind === "release") anchor.current = null;
    };
    mouseListeners.add(onMouse);
    return () => {
      mouseListeners.delete(onMouse);
      process.stdout.write(seq.mouseOff + (keyPlan.otherKeys ? seq.otherKeysOff : ""));
    };
  }, [offset]);

  useInput((input, key) => {
    if ((key.ctrl && input === "c") || input === "q") {
      exit();
      return;
    }
    if (input === "s" && !key.ctrl) setStreaming(true);
    setKeys((list) =>
      [describeKey(input, key as unknown as Record<string, unknown>), ...list].slice(0, 6),
    );
  });

  usePaste((text) => {
    setPastes((list) => [`${text.length} chars in one event`, ...list].slice(0, 3));
  });

  const visible = ROWS.slice(offset, offset + listHeight);
  return (
    <Box flexDirection="column" width={columns} height={rows}>
      <Text bold>
        Ink spike — {columns}x{rows}, resizes seen {resizes.current}, frames {gate.stats.frames}
        {", "}sync {syncOutputWanted(env) ? "on" : "off"}, kitty {String(keyPlan.kitty)}, otherKeys{" "}
        {String(keyPlan.otherKeys)}
      </Text>
      <Box flexDirection="column" height={listHeight}>
        {visible.map((label, index) => {
          const line = offset + index;
          const picked = selection !== null && line >= selection[0] && line <= selection[1];
          return (
            <Text key={label} {...(picked ? { backgroundColor: "blue" } : {})}>
              {label.padEnd(Math.max(0, columns - 1))}
            </Text>
          );
        })}
      </Box>
      <Text>keys: {keys.join("  ")}</Text>
      <Text>pastes: {pastes.join("  ")}</Text>
      <Text>
        mouse reports {filter.stats.mouseReports}, otherKeys rewritten{" "}
        {filter.stats.otherKeysRewritten}
      </Text>
      <Text>
        stream: {stream.tokens} tokens{stream.done ? " (done)" : ""} — press s
      </Text>
      <Box height={6} overflow="hidden">
        <Text wrap="wrap">{stream.text.slice(-columns * 6)}</Text>
      </Box>
    </Box>
  );
}

const instance = render(<Spike />, {
  stdout: gate.stream,
  stdin: filter.stream,
  alternateScreen: true,
  exitOnCtrlC: false,
  maxFps: 60,
  kittyKeyboard: keyPlan.kitty ? { mode: "enabled" } : { mode: "disabled" },
});
unmountInk = () => instance.unmount();
await instance.waitUntilExit();
resetModes();
process.exit(0);
