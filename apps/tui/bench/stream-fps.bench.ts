// TUI performance budgets (P25-13, D-04.2): the real App, rendered by Ink through our output gate
// into a fake terminal, (1) streams the recorded 10,000-token reply (`long-reply` fixture) at no
// fewer frames per second than the budget, and (2) paints a resize storm in no more frames than
// the budget. Numbers come from tooling/perf/budgets.toml. Real timers: run alone
// (`pnpm --filter @indus/tui bench`).

import { EventEmitter } from "node:events";
import { IndusClient, systemClock } from "@indus/client";
import { FakeServer, type MethodHandler } from "@indus/client/testing";
import { render } from "ink";
import { createElement } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { readBudgets } from "../../../tooling/perf/read-budgets.mjs";
import { App } from "../src/app/App.tsx";
import { AppRuntime, LinkWatch } from "../src/app/appRuntime.ts";
import { RpcProvider } from "../src/app/rpc.ts";
import { uiStore } from "../src/state/uiStore.ts";
import { gateStdout } from "../src/terminal/outputGate.ts";
import { resetStores } from "../src/testing/harness.ts";
import { loadFixture } from "../src/testing/mockGateway.ts";

interface TuiBudgets {
  stream_fps: { min_fps: number; tokens: number };
  resize_storm: { events: number; max_frames: number };
}
const tui = readBudgets().tui as unknown as TuiBudgets;
type Event = { type: string } & Record<string, unknown>;
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

class FakeTty extends EventEmitter {
  readonly isTTY = true;
  columns = 100;
  rows = 40;
  bytes = 0;
  write(chunk: unknown, ...rest: unknown[]): boolean {
    this.bytes += String(chunk).length;
    const done = rest.find((value) => typeof value === "function") as (() => void) | undefined;
    done?.();
    return true;
  }
}

class FakeStdin extends EventEmitter {
  readonly isTTY = true;
  setRawMode(): this {
    return this;
  }
  setEncoding(): this {
    return this;
  }
  ref(): this {
    return this;
  }
  unref(): this {
    return this;
  }
  resume(): this {
    return this;
  }
  pause(): this {
    return this;
  }
  read(): null {
    return null;
  }
}

interface Mounted {
  server: FakeServer;
  tty: FakeTty;
  frames(): number;
  sid(): string;
  stop(): void;
}

async function mountApp(): Promise<Mounted> {
  resetStores();
  const server = new FakeServer();
  let created = 0;
  const handlers: Record<string, MethodHandler> = {
    "client.capabilities": (p) => ({ features: p.features ?? [] }),
    "commands.catalog": () => ({ commands: [] }),
    "config.get": (p) => ({ key: p.key, value: null }),
    "session.create": () => {
      created += 1;
      return { session_id: `s-bench-${created}`, created: true };
    },
    "session.list": () => ({ sessions: [] }),
    "session.close": () => ({ closed: true }),
    "prompt.submit": () => ({ accepted: true }),
  };
  for (const [method, handler] of Object.entries(handlers)) server.handle(method, handler);
  const links = new LinkWatch();
  const client = new IndusClient({ connect: links.wrap(server.connect) });
  let keys = 0;
  const runtime = new AppRuntime({
    client,
    links,
    env: {},
    clock: systemClock,
    exit: () => undefined,
    history: { entries: () => [], push: () => undefined },
    newKey: () => `key-${++keys}`,
    writeFile: () => undefined,
  });
  runtime.start();
  const tty = new FakeTty();
  const gate = gateStdout(tty as unknown as NodeJS.WriteStream, { syncOutput: true });
  const tree = createElement(RpcProvider, { rpc: runtime.rpc }, createElement(App, { runtime }));
  const app = render(tree, {
    stdout: gate.stream,
    stdin: new FakeStdin() as unknown as NodeJS.ReadStream,
    exitOnCtrlC: false,
    maxFps: 60, // as entry.tsx
    patchConsole: false,
  });
  server.ready();
  await sleep(100);
  return {
    server,
    tty,
    frames: () => gate.stats.frames,
    sid: () => String(uiStore.getState().sid),
    stop: () => {
      app.unmount();
      app.cleanup();
      runtime.dispose();
    },
  };
}

let mounted: Mounted | null = null;
afterEach(() => {
  mounted?.stop();
  mounted = null;
});

describe("TUI performance budgets", () => {
  it("streams the recorded 10,000-token reply at the budgeted frame rate (D-04.2)", async () => {
    const budget = tui.stream_fps;
    const m = await mountApp();
    mounted = m;
    const sid = m.sid();
    const events = loadFixture("long-reply").steps.flatMap((step) =>
      step.kind === "event"
        ? [{ delayMs: step.delayMs, event: { ...step.event, session_id: sid } as unknown as Event }]
        : [],
    );
    const deltas = events.filter((e) => e.event.type === "message.delta");
    const words = deltas.reduce((n, e) => n + String(e.event.text).split(/\s+/).length, 0);
    expect(words * 1.3).toBeGreaterThanOrEqual(budget.tokens * 0.6); // the recorded reply is ~10k tokens

    let started = 0;
    let streamed = 0;
    let first = 0;
    for (const { delayMs, event } of events) {
      if (delayMs > 0) await sleep(delayMs);
      if (event.type === "message.delta" && started === 0) {
        started = performance.now();
        first = m.frames();
      }
      m.server.emit(event);
      if (event.type === "message.delta") streamed = performance.now();
    }
    const frames = m.frames() - first;
    const seconds = (streamed - started) / 1000;
    const fps = frames / seconds;
    process.stdout.write(
      `PERF tui.stream_fps: ${fps.toFixed(1)} fps (${frames} frames / ${seconds.toFixed(2)} s, ${deltas.length} deltas); budget >= ${budget.min_fps}\n`,
    );
    expect(fps).toBeGreaterThanOrEqual(budget.min_fps);
  });

  it("paints a resize storm in no more frames than the budget (R05.16)", async () => {
    const budget = tui.resize_storm;
    const m = await mountApp();
    mounted = m;
    await sleep(100);
    const before = m.frames();
    // A drag-resize: the events arrive in bursts of ten, one burst every 10 ms (~100 ms in all).
    for (let i = 0; i < budget.events; i += 1) {
      m.tty.columns = i === budget.events - 1 ? 123 : 80 + (i % 40);
      m.tty.emit("resize");
      if (i % 10 === 9) await sleep(10);
    }
    await sleep(200); // the trailing reflow and its frame
    const frames = m.frames() - before;
    process.stdout.write(
      `PERF tui.resize_storm: ${frames} frames for ${budget.events} resize events; budget <= ${budget.max_frames}\n`,
    );
    expect(frames).toBeGreaterThan(0);
    expect(frames).toBeLessThanOrEqual(budget.max_frames);
  });
});
