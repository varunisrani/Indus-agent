// Test helpers for the composer, submission and key-routing tests (slice R3): a fake bridge
// behind the real `@indus/client` channel, a hand-driven clock, and a small Ink render harness
// over fake terminal streams.

import { PassThrough, Writable } from "node:stream";
import { type Clock, RpcChannel } from "@indus/client";
import { FakeServer, type MethodHandler } from "@indus/client/testing";
import { render } from "ink";
import type { ReactElement } from "react";
import type { Rpc } from "../app/rpc.ts";
import { captureDestination, isCurrent } from "../state/destination.ts";
import { uiStore } from "../state/uiStore.ts";
import { PromptQueue } from "../submit/queue.ts";
import { Submitter } from "../submit/submission.ts";
import { Composer } from "./composerStore.ts";

export interface FakeBridge {
  readonly server: FakeServer;
  readonly rpc: Rpc;
  /** Params of every call to `method` so far, in order. */
  calls(method: string): Record<string, unknown>[];
}

export function fakeBridge(handlers: Record<string, MethodHandler> = {}): FakeBridge {
  const server = new FakeServer();
  for (const [method, handler] of Object.entries(handlers)) server.handle(method, handler);
  const channel = new RpcChannel({ timeoutMs: 5_000 });
  channel.attach(server.connect());
  const rpc: Rpc = {
    call: (method, params) => channel.call(method, (params ?? {}) as object),
    answer: async () => {},
  };
  const calls = (method: string): Record<string, unknown>[] =>
    server.received
      .filter((frame) => frame.method === method)
      .map((frame) => (frame.params ?? {}) as Record<string, unknown>);
  return { server, rpc, calls };
}

/** Lets every queued microtask and the fake server's hops run. */
export async function settle(rounds = 20): Promise<void> {
  for (let i = 0; i < rounds; i += 1) await new Promise<void>((resolve) => setImmediate(resolve));
}

export class FakeClock implements Clock {
  current = 0;
  private timers: { at: number; run: () => void; id: number }[] = [];
  private nextId = 1;

  now(): number {
    return this.current;
  }

  setTimeout(run: () => void, ms: number): unknown {
    const id = this.nextId;
    this.nextId += 1;
    this.timers.push({ at: this.current + ms, run, id });
    return id;
  }

  clearTimeout(handle: unknown): void {
    this.timers = this.timers.filter((timer) => timer.id !== handle);
  }

  /** Move time forward, firing due timers in order. */
  advance(ms: number): void {
    const until = this.current + ms;
    for (;;) {
      const due = this.timers.filter((timer) => timer.at <= until).sort((a, b) => a.at - b.at)[0];
      if (due === undefined) break;
      this.timers = this.timers.filter((timer) => timer.id !== due.id);
      this.current = due.at;
      due.run();
    }
    this.current = until;
  }

  get pending(): number {
    return this.timers.length;
  }
}

/** Puts the ui store back to a known state between tests. */
export function resetUi(
  patch: Parameters<ReturnType<typeof uiStore.getState>["patch"]>[0] = {},
): void {
  uiStore.getState().patch({
    sid: "s1",
    storedSid: "s1",
    revision: 0,
    info: null,
    busy: false,
    statusText: "",
    busyInputMode: "queue",
    pasteLines: 5,
    pasteChars: 2000,
    ...patch,
  });
}

export interface InkHarness {
  /** The last frame Ink wrote, ANSI escapes removed. */
  frame(): string;
  /** Type raw terminal bytes. */
  type(bytes: string): Promise<void>;
  unmount(): void;
}

// biome-ignore lint/suspicious/noControlCharactersInRegex: frames are full of escape sequences
const ANSI = /\u001b\[[0-9;?]*[ -/]*[@-~]/g;

/** Renders `element` into fake terminal streams (debug mode: every frame is written whole). */
export async function renderInk(element: ReactElement): Promise<InkHarness> {
  const frames: string[] = [];
  const stdout = Object.assign(
    new Writable({
      write(chunk, _encoding, done) {
        frames.push(String(chunk));
        done();
      },
    }),
    { columns: 80, rows: 24, isTTY: false },
  );
  const stdin = Object.assign(new PassThrough(), {
    isTTY: true,
    setRawMode: () => undefined,
    ref: () => undefined,
    unref: () => undefined,
  });
  const instance = render(element, {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    debug: true,
    exitOnCtrlC: false,
    patchConsole: false,
  });
  await settle();
  return {
    frame: () => (frames.at(-1) ?? "").replace(ANSI, ""),
    type: async (bytes) => {
      stdin.write(bytes);
      await settle();
    },
    unmount: () => instance.unmount(),
  };
}

export interface SubmitRig {
  readonly submitter: Submitter;
  readonly composer: Composer;
  readonly queue: PromptQueue;
  readonly clock: FakeClock;
  readonly bridge: FakeBridge;
  readonly system: string[];
  readonly user: string[];
  readonly slash: { command: string; display: string }[];
  readonly interrupts: { keepBusy: boolean }[];
  readonly history: string[];
}

/** A Submitter over a fake bridge, a fresh composer and queue, and the shared ui store. */
export function submitRig(handlers: Record<string, MethodHandler> = {}): SubmitRig {
  const bridge = fakeBridge(handlers);
  const composer = new Composer();
  const queue = new PromptQueue();
  const clock = new FakeClock();
  const system: string[] = [];
  const user: string[] = [];
  const slash: { command: string; display: string }[] = [];
  const interrupts: { keepBusy: boolean }[] = [];
  const history: string[] = [];
  const submitter = new Submitter({
    rpc: bridge.rpc,
    composer,
    queue,
    history: { push: (text) => history.push(text) },
    ui: uiStore,
    clock,
    capture: captureDestination,
    isCurrent,
    runSlash: (command, display) => slash.push({ command, display }),
    interrupt: (options) => interrupts.push(options),
    system: (text) => system.push(text),
    userLine: (text) => user.push(text),
  });
  return { submitter, composer, queue, clock, bridge, system, user, slash, interrupts, history };
}
