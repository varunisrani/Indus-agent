// Test-only: an `AppRuntime` over `@indus/client`'s fake server, with a hand-driven clock, an
// in-memory history and every outside effect recorded.

import { IndusClient } from "@indus/client";
import { FakeRpcError, FakeServer, type MethodHandler } from "@indus/client/testing";
import { ERROR_CODES } from "@indus/protocol";
import { FakeClock, settle } from "../composer/testSupport.ts";
import { resetStores } from "../testing/harness.ts";
import { AppRuntime, LinkWatch } from "./appRuntime.ts";
import type { Env } from "./launchOptions.ts";

export interface RuntimeRig {
  server: FakeServer;
  client: IndusClient;
  runtime: AppRuntime;
  clock: FakeClock;
  exits: number[];
  files: { path: string; text: string }[];
  /** Params of every call of `method`, in order. */
  calls(method: string): Record<string, unknown>[];
  /** The fake child says ready; lets the bootstrap run. */
  ready(): Promise<void>;
  /** The fake child dies; lets the loss handling run. */
  die(): Promise<void>;
}

export interface RigOptions {
  env?: Env;
  /** The runtime's link is the daemon attach (P17-19). */
  attached?: boolean;
  handlers?: Record<string, MethodHandler>;
  timeoutMs?: number;
}

let created = 0;

/** A resume answer for `id` with the given stored rows. */
export function resumeAnswer(id: string, messages: { role: string; text: string }[] = []) {
  return { session_id: id, message_count: messages.length, last_seq: 0, title: null, messages };
}

export function runtimeRig(options: RigOptions = {}): RuntimeRig {
  resetStores();
  const server = new FakeServer();
  const defaults: Record<string, MethodHandler> = {
    "client.capabilities": (p) => ({ features: p.features ?? [] }),
    "commands.catalog": () => ({ commands: [] }),
    "config.get": (p) => ({ key: p.key, value: null }),
    "session.create": () => {
      created += 1;
      return { session_id: `s-new-${created}`, created: true };
    },
    "session.resume": (p) => resumeAnswer(String(p.session_id)),
    "session.list": () => ({ sessions: [] }),
    "session.close": () => ({ closed: true }),
    "prompt.submit": () => ({ accepted: true }),
    "session.interrupt": () => ({ interrupted: true }),
  };
  for (const [method, handler] of Object.entries({ ...defaults, ...options.handlers })) {
    server.handle(method, handler);
  }
  const clock = new FakeClock();
  const links = new LinkWatch();
  const client = new IndusClient({
    connect: links.wrap(server.connect),
    ...(options.timeoutMs === undefined ? {} : { timeoutMs: options.timeoutMs }),
  });
  const exits: number[] = [];
  const files: { path: string; text: string }[] = [];
  const entries: string[] = [];
  let keys = 0;
  const runtime = new AppRuntime({
    client,
    links,
    env: options.env ?? {},
    ...(options.attached === undefined ? {} : { attached: options.attached }),
    clock,
    exit: (code) => exits.push(code),
    history: { entries: () => entries, push: (text) => entries.push(text) },
    newKey: () => {
      keys += 1;
      return `key-${keys}`;
    },
    writeFile: (path, text) => files.push({ path, text }),
  });
  runtime.start();
  const calls = (method: string): Record<string, unknown>[] =>
    server.received
      .filter((frame) => frame.method === method)
      .map((frame) => (frame.params ?? {}) as Record<string, unknown>);
  return {
    server,
    client,
    runtime,
    clock,
    exits,
    files,
    calls,
    ready: async () => {
      server.ready();
      await settle(40);
    },
    die: async () => {
      server.die();
      await settle(40);
    },
  };
}

export const notFound = (what: string): FakeRpcError =>
  new FakeRpcError(ERROR_CODES.SESSION_NOT_FOUND.code, `no session ${what}`, {
    name: "SESSION_NOT_FOUND",
  });

export { settle };
