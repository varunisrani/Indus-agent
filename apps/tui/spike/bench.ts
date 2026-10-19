// Spike bench (P12-01): headless numbers for D-04.2 on upstream Ink + React 19.
// Run: node apps/tui/spike/bench.ts [--json <file>]   (plain TS, no JSX, so Node runs it directly)
// Exit code 1 when the stream misses 30 fps or the resize storm writes too many frames.

import { writeFileSync } from "node:fs";
import { PassThrough, Writable } from "node:stream";
import { Box, render, Text, useInput, usePaste, useWindowSize } from "ink";
import { createElement as h, useEffect, useState } from "react";
import { InputFilter, type MouseEvent } from "./inputFilter.ts";
import { gateStdout } from "./outputGate.ts";
import { seq, syncOutputWanted } from "./terminalModes.ts";

type Row = { item: string; result: string; pass: boolean };
const rows: Row[] = [];
const numbers: Record<string, unknown> = {};
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

class FakeTty extends Writable {
  isTTY = true;
  columns = 100;
  rows = 40;
  output = "";
  override _write(chunk: Buffer | string, _enc: BufferEncoding, done: () => void): void {
    this.output += String(chunk);
    done();
  }
  resize(columns: number, rowsCount: number): void {
    this.columns = columns;
    this.rows = rowsCount;
    this.emit("resize");
  }
}

function fakeStdin(): PassThrough & { isTTY: boolean; setRawMode: (on: boolean) => void } {
  return Object.assign(new PassThrough(), { isTTY: true, setRawMode: () => undefined });
}

const count = (text: string, needle: string): number => text.split(needle).length - 1;
const asTty = (tty: FakeTty) => tty as unknown as NodeJS.WriteStream;

async function benchAltScreen(): Promise<void> {
  const tty = new FakeTty();
  const app = render(h(Text, null, "hello"), {
    stdout: asTty(tty),
    stdin: fakeStdin(),
    alternateScreen: true,
    patchConsole: false,
  });
  await sleep(20);
  app.unmount();
  await app.waitUntilExit();
  const on = tty.output.indexOf(seq.altScreenOn);
  const off = tty.output.lastIndexOf(seq.altScreenOff);
  const pass = on === 0 && off > on;
  rows.push({
    item: "(a) alt screen 1049 enter/leave",
    result: `enter at byte ${on}, leave at byte ${off} of ${tty.output.length}`,
    pass,
  });
}

async function benchKeys(): Promise<void> {
  const raw = fakeStdin();
  const seen: string[] = [];
  const filter = new InputFilter(raw, () => undefined);
  function Keys() {
    useInput((input, key) => {
      const mods = (["shift", "meta", "ctrl"] as const).filter((name) => key[name]).join("+");
      const name = key.return
        ? "enter"
        : key.backspace
          ? "backspace"
          : key.delete
            ? "delete"
            : input;
      seen.push(`${mods || "plain"}:${name}`);
    });
    return h(Text, null, "keys");
  }
  const tty = new FakeTty();
  const app = render(h(Keys), {
    stdout: asTty(tty),
    stdin: filter.stream,
    exitOnCtrlC: false,
    patchConsole: false,
    kittyKeyboard: { mode: "enabled" },
  });
  await sleep(30);
  const cases: Array<[string, string, string]> = [
    ["kitty Shift+Enter", "\u001b[13;2u", "shift:enter"],
    ["kitty Alt+Enter", "\u001b[13;3u", "meta:enter"],
    ["kitty Ctrl+Backspace", "\u001b[127;5u", "ctrl:backspace"],
    ["otherKeys Shift+Enter", "\u001b[27;2;13~", "shift:enter"],
    ["otherKeys Alt+Enter", "\u001b[27;3;13~", "meta:enter"],
    ["otherKeys Ctrl+Backspace", "\u001b[27;5;127~", "ctrl:backspace"],
    ["legacy Enter", "\r", "plain:enter"],
  ];
  const results: string[] = [];
  let ok = true;
  for (const [label, bytes, want] of cases) {
    seen.length = 0;
    raw.write(bytes);
    await sleep(40);
    const got = seen.join(",") || "(none)";
    if (got !== want) ok = false;
    results.push(`${label} → ${got}`);
  }
  app.unmount();
  numbers.keys = results;
  numbers.kittyPushWritten = tty.output.includes(seq.kittyPush(1));
  rows.push({
    item: "(b) kitty CSI-u + modifyOtherKeys decode",
    result: `${results.filter((line) => !line.includes("(none)")).length}/${cases.length} decoded as expected=${ok}; kitty push written=${String(numbers.kittyPushWritten)}`,
    pass: ok,
  });
}

async function benchPaste(): Promise<void> {
  const raw = fakeStdin();
  const pastes: number[] = [];
  let keyEvents = 0;
  const filter = new InputFilter(raw, () => undefined);
  function Paste() {
    usePaste((text) => pastes.push(text.length));
    useInput(() => {
      keyEvents += 1;
    });
    return h(Text, null, "paste");
  }
  const tty = new FakeTty();
  const app = render(h(Paste), {
    stdout: asTty(tty),
    stdin: filter.stream,
    patchConsole: false,
  });
  await sleep(30);
  const body = "x".repeat(4_998).concat("\n\n");
  raw.write(`\u001b[200~${body.slice(0, 1_700)}`);
  await sleep(5);
  raw.write(body.slice(1_700, 3_400));
  await sleep(5);
  raw.write(`${body.slice(3_400)}\u001b[201~`);
  await sleep(60);
  app.unmount();
  const pass = pastes.length === 1 && pastes[0] === 5_000 && keyEvents === 0;
  rows.push({
    item: "(c) bracketed paste 2004 in one chunk",
    result: `3 reads → ${pastes.length} paste event(s) of ${pastes.join(",")} chars; key events ${keyEvents}; 2004h written=${tty.output.includes(seq.pasteOn)}`,
    pass,
  });
}

async function benchMouse(): Promise<void> {
  const raw = fakeStdin();
  const events: MouseEvent[] = [];
  let keyEvents = 0;
  const filter = new InputFilter(raw, (event) => events.push(event));
  function Mouse() {
    useInput(() => {
      keyEvents += 1;
    });
    return h(Text, null, "mouse");
  }
  const app = render(h(Mouse), {
    stdout: asTty(new FakeTty()),
    stdin: filter.stream,
    patchConsole: false,
  });
  await sleep(30);
  // press, two drag reports (one split across reads), release, wheel up, wheel down
  raw.write("\u001b[<0;5;4M\u001b[<32;9;6M\u001b[<3");
  raw.write("2;12;7M\u001b[<0;12;7m");
  raw.write("\u001b[<64;3;3M\u001b[<65;3;3M");
  await sleep(40);
  app.unmount();
  const kinds = events.map((event) => event.kind).join(",");
  const want = "press,drag,drag,release,wheel-up,wheel-down";
  rows.push({
    item: "(d) SGR mouse 1000/1002/1006 decode",
    result: `${kinds}; leaked key events ${keyEvents}`,
    pass: kinds === want && keyEvents === 0,
  });
}

function Counter(props: { ticks: number }) {
  const [value, setValue] = useState(0);
  useEffect(() => {
    let left = props.ticks;
    const timer = setInterval(() => {
      setValue((current) => current + 1);
      left -= 1;
      if (left === 0) clearInterval(timer);
    }, 40);
    return () => clearInterval(timer);
  }, [props.ticks]);
  return h(Text, null, `frame ${value}`);
}

async function framesWith(env: Record<string, string>, gated: boolean): Promise<number> {
  const tty = new FakeTty();
  const { stream } = gateStdout(asTty(tty), { syncAllowed: syncOutputWanted(env) });
  const app = render(h(Counter, { ticks: 10 }), {
    stdout: gated ? stream : asTty(tty),
    stdin: fakeStdin(),
    patchConsole: false,
  });
  await sleep(600);
  app.unmount();
  return count(tty.output, seq.syncBegin);
}

async function benchSync(): Promise<void> {
  const rawTmux = await framesWith(
    { TMUX: "/tmp/tmux-0/default,1,0", TERM_PROGRAM: "tmux" },
    false,
  );
  const gatedTmux = await framesWith(
    { TMUX: "/tmp/tmux-0/default,1,0", TERM_PROGRAM: "tmux" },
    true,
  );
  const gatedWez = await framesWith({ TERM_PROGRAM: "WezTerm" }, true);
  numbers.sync = { rawTmux, gatedTmux, gatedWez };
  rows.push({
    item: "(e) DEC 2026 per frame, never in tmux",
    result: `raw Ink in tmux: ${rawTmux} BSU; our gate in tmux: ${gatedTmux}; our gate in WezTerm: ${gatedWez}`,
    pass: gatedTmux === 0 && gatedWez > 0,
  });
  if (rawTmux > 0) {
    rows.push({
      item: "    upstream Ink alone",
      result: "wraps every TTY frame in 2026 markers, tmux included → gate needed",
      pass: false,
    });
  }
}

interface StreamResult {
  fps: number;
  frames: number;
  seconds: number;
  meanMs: number;
  p95Ms: number;
  maxMs: number;
}

type StreamMode = "cap16k" | "viewport";

/** Incremental hard wrap: completed rows are cut once; only the open row changes per flush. */
class RowCache {
  readonly rows: string[] = [];
  private open = "";
  private readonly width: number;
  constructor(width: number) {
    this.width = width;
  }
  append(text: string): void {
    let rest = this.open + text;
    for (;;) {
      const newline = rest.indexOf("\n");
      const cut =
        newline >= 0 && newline <= this.width
          ? newline
          : rest.length > this.width
            ? this.width
            : -1;
      if (cut < 0) break;
      this.rows.push(rest.slice(0, cut));
      rest = rest.slice(cut + (cut === newline ? 1 : 0));
    }
    this.open = rest;
  }
  /** The last `count` rows with their absolute row numbers (stable React keys). */
  tail(count: number): Array<[number, string]> {
    const first = Math.max(0, this.rows.length - (count - 1));
    const done = this.rows.slice(first).map((row, index): [number, string] => [first + index, row]);
    return [...done, [this.rows.length, this.open]];
  }
}

async function streamOnce(
  mode: StreamMode,
  incremental: boolean,
  tokensPerSecond: number,
): Promise<StreamResult> {
  const perTick = Math.max(1, Math.round(tokensPerSecond / 250));
  const tty = new FakeTty();
  const gate = gateStdout(asTty(tty), { syncAllowed: true });
  const renderMs: number[] = [];
  let finished: (() => void) | undefined;
  const done = new Promise<void>((resolve) => {
    finished = resolve;
    setTimeout(resolve, 30_000);
  });
  function Stream() {
    const [view, setView] = useState<Array<[number, string]>>([]);
    useEffect(() => {
      let produced = "";
      let sent = 0;
      let tokens = 0;
      const cache = new RowCache(100);
      // One producer tick every 4 ms; `perTick` sets the token rate.
      const producer = setInterval(() => {
        for (let i = 0; i < perTick && tokens < 10_000; i += 1, tokens += 1) {
          produced += `tok${tokens} `;
        }
      }, 4);
      const flush = setInterval(() => {
        if (mode === "cap16k") {
          setView([[0, produced.slice(-16_000)]]);
        } else {
          cache.append(produced.slice(sent));
          sent = produced.length;
          setView(cache.tail(40));
        }
        if (tokens >= 10_000) {
          clearInterval(producer);
          clearInterval(flush);
          setTimeout(() => finished?.(), 50);
        }
      }, 16);
      return () => {
        clearInterval(producer);
        clearInterval(flush);
      };
    }, []);
    return h(
      Box,
      { flexDirection: "column", width: 100, height: 40, overflow: "hidden" },
      ...view.map(([key, row]) =>
        h(Text, { key, wrap: mode === "cap16k" ? "wrap" : "truncate" }, row),
      ),
    );
  }
  const started = performance.now();
  const app = render(h(Stream), {
    stdout: gate.stream,
    stdin: fakeStdin(),
    patchConsole: false,
    maxFps: 60,
    incrementalRendering: incremental,
    onRender: (metrics) => renderMs.push(metrics.renderTime),
  });
  await done;
  const seconds = (performance.now() - started) / 1000;
  app.unmount();
  const sorted = [...renderMs].sort((a, b) => a - b);
  const pick = (q: number) =>
    sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))] ?? 0;
  const frames = gate.stats.frames;
  return {
    fps: frames / seconds,
    frames,
    seconds,
    meanMs: sorted.reduce((sum, value) => sum + value, 0) / Math.max(1, sorted.length),
    p95Ms: pick(0.95),
    maxMs: sorted.at(-1) ?? 0,
  };
}

async function benchStream(): Promise<void> {
  // 5,000 tokens/s is faster than any model streams, so rendering is the bottleneck;
  // 1,000 tokens/s is a fast real model.
  const viewport = await streamOnce("viewport", false, 5_000);
  const incremental = await streamOnce("viewport", true, 5_000);
  const realistic = await streamOnce("viewport", false, 1_000);
  const capped = await streamOnce("cap16k", false, 5_000);
  numbers.stream = { viewport, incremental, realistic, capped };
  const show = (r: StreamResult) =>
    `${r.fps.toFixed(1)} fps (${r.frames} frames / ${r.seconds.toFixed(2)} s), render mean ${r.meanMs.toFixed(2)} ms p95 ${r.p95Ms.toFixed(2)} ms max ${r.maxMs.toFixed(2)} ms`;
  rows.push({
    item: "(f) 10k tokens at 5k/s, 16 ms batch, visible rows",
    result: show(viewport),
    pass: viewport.fps >= 30,
  });
  rows.push({
    item: "    same, Ink incrementalRendering on",
    result: show(incremental),
    pass: incremental.fps >= 30,
  });
  rows.push({
    item: "    same at 1k tokens/s",
    result: show(realistic),
    pass: realistic.fps >= 30,
  });
  rows.push({
    item: "    5k/s, whole 16k-char tail wrapped by Ink",
    result: show(capped),
    pass: capped.fps >= 30,
  });
}

async function stormOnce(
  coalesced: boolean,
): Promise<{ frames: number; renders: number; lastWidth: number }> {
  const tty = new FakeTty();
  const gate = gateStdout(asTty(tty), { syncAllowed: true, coalesceResize: coalesced });
  let renders = 0;
  let lastWidth = 0;
  function Sized() {
    const { columns } = useWindowSize();
    lastWidth = columns;
    return h(Box, { width: columns }, h(Text, { wrap: "wrap" }, "x".repeat(columns * 3)));
  }
  const app = render(h(Sized), {
    stdout: gate.stream,
    stdin: fakeStdin(),
    patchConsole: false,
    maxFps: 60,
    onRender: () => {
      renders += 1;
    },
  });
  await sleep(50);
  const before = gate.stats.frames;
  renders = 0;
  for (let i = 0; i < 200; i += 1) {
    tty.resize(i === 199 ? 123 : 80 + (i % 40), 40);
    await sleep(5);
  }
  await sleep(100);
  const frames = gate.stats.frames - before;
  app.unmount();
  return { frames, renders, lastWidth };
}

async function benchResize(): Promise<void> {
  const started = performance.now();
  const raw = await stormOnce(false);
  const stormMs = performance.now() - started - 150;
  const coalesced = await stormOnce(true);
  const bound = Math.floor(stormMs / 32) + 2;
  numbers.resize = { raw, coalesced, stormMs: Math.round(stormMs), bound };
  rows.push({
    item: "(g) 200-event resize storm, raw Ink",
    result: `${raw.frames} frames, ${raw.renders} renders over ~${Math.round(stormMs)} ms (bound ${bound}); final width ${raw.lastWidth}`,
    pass: raw.frames <= bound && raw.lastWidth === 123,
  });
  rows.push({
    item: "    with our 32 ms coalescer",
    result: `${coalesced.frames} frames, ${coalesced.renders} renders; final width ${coalesced.lastWidth}`,
    pass: coalesced.frames <= bound && coalesced.lastWidth === 123,
  });
}

const stages: Array<[string, () => Promise<void>]> = [
  ["alt", benchAltScreen],
  ["keys", benchKeys],
  ["paste", benchPaste],
  ["mouse", benchMouse],
  ["sync", benchSync],
  ["stream", benchStream],
  ["resize", benchResize],
];
const onlyAt = process.argv.indexOf("--only");
const only = onlyAt > 0 ? (process.argv[onlyAt + 1] ?? "").split(",") : null;
for (const [name, stage] of stages) {
  if (only && !only.includes(name)) continue;
  process.stderr.write(`bench: ${name}\n`);
  await stage();
}

const width = Math.max(...rows.map((row) => row.item.length));
process.stdout.write(`Ink spike bench — node ${process.version}, ink 8.0.0, react 19.3.0\n`);
for (const row of rows) {
  process.stdout.write(`${row.pass ? "PASS" : "FAIL"}  ${row.item.padEnd(width)}  ${row.result}\n`);
}
for (const line of (numbers.keys as string[]) ?? []) process.stdout.write(`      ${line}\n`);
const jsonAt = process.argv.indexOf("--json");
if (jsonAt > 0 && process.argv[jsonAt + 1]) {
  writeFileSync(process.argv[jsonAt + 1] as string, JSON.stringify({ rows, numbers }, null, 2));
}
const gateFailed = rows.some(
  (row) => !row.pass && (row.item.startsWith("(f)") || row.item.includes("coalescer")),
);
process.exit(gateFailed ? 1 : 0);
