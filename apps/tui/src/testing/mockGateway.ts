// Replays a recorded NDJSON session through `@indus/client`'s fake server, in order, with
// injected timing. Fixtures are hand-written from our protocol models (see
// docs/notes/P12/recorded-streams.md for the line format) and checked against the generated schema.

import { readdirSync, readFileSync } from "node:fs";
import { basename, join } from "node:path";
import { FakeServer } from "@indus/client/testing";
import { EVENTS, METHODS, NOTIFICATIONS, type ServerRequest } from "@indus/protocol";
import { checkType } from "./schemaCheck.ts";

type Json = Record<string, unknown>;

export type FixtureStep =
  | { kind: "event"; delayMs: number; event: Json; line: number }
  | { kind: "notify"; delayMs: number; method: string; params: Json; line: number }
  | { kind: "ask"; delayMs: number; request: ServerRequest; line: number }
  | { kind: "await_call"; delayMs: number; method: string; line: number }
  | { kind: "result"; delayMs: number; method: string; value: unknown; line: number };

export interface Fixture {
  name: string;
  steps: FixtureStep[];
}

export const FIXTURE_DIR = join(import.meta.dirname, "fixtures");

/** The names of the shipped fixtures (file names without `.ndjson`). */
export function fixtureNames(): string[] {
  return readdirSync(FIXTURE_DIR)
    .filter((file) => file.endsWith(".ndjson"))
    .map((file) => basename(file, ".ndjson"))
    .sort();
}

function fillPlaceholders(value: unknown, n: number): unknown {
  if (typeof value === "string") return value.replaceAll("{n}", String(n));
  if (Array.isArray(value)) return value.map((item) => fillPlaceholders(item, n));
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, fillPlaceholders(v, n)]));
  }
  return value;
}

function fail(name: string, line: number, why: string): never {
  throw new Error(`${name}.ndjson:${line}: ${why}`);
}

function assertValid(name: string, line: number, value: unknown, typeName: string): void {
  const errors = checkType(value, typeName);
  if (errors.length > 0) fail(name, line, `${typeName}: ${errors.slice(0, 3).join("; ")}`);
}

/**
 * Parse fixture text: expand `repeat`, fill `join_deltas`, stamp `seq` per session, validate every
 * step against the protocol schema. Throws with `name.ndjson:line` on the first bad line.
 */
export function parseFixture(text: string, name: string): Fixture {
  const steps: FixtureStep[] = [];
  const seq = new Map<string, number>();
  const deltas = new Map<string, string>();
  const lines = text.split("\n");
  lines.forEach((raw, index) => {
    const line = index + 1;
    const trimmed = raw.trim();
    if (trimmed === "" || trimmed.startsWith("#")) return;
    let entry: Json;
    try {
      entry = JSON.parse(trimmed) as Json;
    } catch {
      fail(name, line, "not JSON");
    }
    const delayMs = typeof entry.delay_ms === "number" ? entry.delay_ms : 0;
    if ("event" in entry) {
      const count = typeof entry.repeat === "number" ? entry.repeat : 1;
      for (let n = 1; n <= count; n++) {
        const event = fillPlaceholders(entry.event, n) as Json;
        const type = String(event.type);
        const typeName = (EVENTS as Record<string, string>)[type];
        if (typeName === undefined) fail(name, line, `unknown event type ${type}`);
        if (typeof entry.join_deltas === "string") event.text = deltas.get(entry.join_deltas) ?? "";
        const sid = String(event.session_id);
        const next = (seq.get(sid) ?? 0) + 1;
        seq.set(sid, next);
        const stamped = { ...event, seq: next };
        assertValid(name, line, stamped, typeName);
        if (type === "message.delta") {
          const turn = String(event.turn_id);
          deltas.set(turn, (deltas.get(turn) ?? "") + String(event.text));
        }
        steps.push({ kind: "event", delayMs, event: stamped, line });
      }
      return;
    }
    if ("notify" in entry) {
      const notify = entry.notify as Json;
      const method = String(notify.method);
      const typeName = (NOTIFICATIONS as Record<string, string>)[method];
      if (typeName === undefined) fail(name, line, `unknown notification ${method}`);
      const params = (notify.params ?? {}) as Json;
      assertValid(name, line, params, typeName);
      steps.push({ kind: "notify", delayMs, method, params, line });
      return;
    }
    if ("ask" in entry) {
      assertValid(name, line, entry.ask, "ServerRequest");
      steps.push({ kind: "ask", delayMs, request: entry.ask as ServerRequest, line });
      return;
    }
    if ("await_call" in entry) {
      const method = String(entry.await_call);
      if (!(method in METHODS)) fail(name, line, `unknown method ${method}`);
      steps.push({ kind: "await_call", delayMs, method, line });
      return;
    }
    if ("result" in entry) {
      const result = entry.result as Json;
      const method = String(result.method);
      const spec = (METHODS as Record<string, { result: string }>)[method];
      if (spec === undefined) fail(name, line, `unknown method ${method}`);
      assertValid(name, line, result.value, spec.result);
      steps.push({ kind: "result", delayMs, method, value: result.value, line });
      return;
    }
    fail(name, line, "unknown line kind");
  });
  return { name, steps };
}

export function loadFixture(name: string): Fixture {
  return parseFixture(readFileSync(join(FIXTURE_DIR, `${name}.ndjson`), "utf8"), name);
}

export interface MockGatewayOptions {
  server?: FakeServer;
  /** Maps a line's `delay_ms` to the wait actually used (scale, clamp, or 0 for instant). */
  timing?: (delayMs: number) => number;
  /** Results for methods the fixture does not answer itself. */
  defaults?: Record<string, unknown>;
}

const DEFAULT_RESULTS: Record<string, unknown> = {
  "session.interrupt": { interrupted: true },
  "clarify.lock": { remaining: [] },
  "server_request.decline": { accepted: true },
};

/** Drives one fixture through a fake server. `run()` resolves when the last line was sent. */
export class MockGateway {
  readonly server: FakeServer;
  readonly fixture: Fixture;
  /** Answers the client gave to `ask` lines, in order. */
  readonly answers: unknown[] = [];
  /** Steps sent so far. */
  sent = 0;
  private readonly timing: (delayMs: number) => number;
  private readonly results = new Map<string, unknown>();
  private readonly waiters = new Map<string, (() => void)[]>();
  private readonly calls = new Map<string, number>();
  private stopped = false;

  constructor(fixture: Fixture, options: MockGatewayOptions = {}) {
    this.fixture = fixture;
    this.server = options.server ?? new FakeServer();
    this.timing = options.timing ?? ((ms) => ms);
    for (const [method, value] of Object.entries({ ...DEFAULT_RESULTS, ...options.defaults })) {
      this.answerWith(method, value);
    }
  }

  /** How many times the client called `method` so far. */
  callCount(method: string): number {
    return this.calls.get(method) ?? 0;
  }

  stop(): void {
    this.stopped = true;
  }

  async run(): Promise<void> {
    for (const step of this.fixture.steps) {
      if (this.stopped) return;
      const wait = this.timing(step.delayMs);
      if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait));
      if (this.stopped) return;
      await this.play(step);
      this.sent += 1;
    }
  }

  private answerWith(method: string, value: unknown): void {
    this.results.set(method, value);
    this.server.handle(method, () => {
      this.calls.set(method, (this.calls.get(method) ?? 0) + 1);
      for (const wake of this.waiters.get(method) ?? []) wake();
      this.waiters.delete(method);
      return this.results.get(method);
    });
  }

  private async play(step: FixtureStep): Promise<void> {
    switch (step.kind) {
      case "event":
        this.server.emit(step.event as { type: string } & Json);
        return;
      case "notify":
        this.server.sendRaw({ jsonrpc: "2.0", method: step.method, params: step.params });
        return;
      case "ask":
        this.answers.push(await this.server.ask(step.request));
        return;
      case "await_call": {
        if (!this.results.has(step.method)) this.answerWith(step.method, null);
        const before = this.callCount(step.method);
        if (before > 0) return;
        await new Promise<void>((resolve) => {
          const list = this.waiters.get(step.method) ?? [];
          list.push(resolve);
          this.waiters.set(step.method, list);
        });
        return;
      }
      case "result":
        this.answerWith(step.method, step.value);
        return;
    }
  }
}

/** Load a shipped fixture and wrap it in a gateway. */
export function mockGateway(name: string, options: MockGatewayOptions = {}): MockGateway {
  return new MockGateway(loadFixture(name), options);
}
