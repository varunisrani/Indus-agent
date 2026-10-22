import { GatewayRpcError, RpcTimeout } from "@indus/client";
import { ERROR_CODES, type ErrorName } from "@indus/protocol";
import { describe, expect, it } from "vitest";
import type { SessionEvent } from "../gateway/connectionStore.ts";
import {
  ChatActions,
  MAX_AUTO_DRAIN_ATTEMPTS,
  type PreparedJournalPort,
  type Request,
  sharedActions,
} from "./busyActions.ts";

const SID = "s1";

function rpcError(name: ErrorName, message = name): GatewayRpcError {
  return new GatewayRpcError(ERROR_CODES[name].code, message);
}

type Handler = (params: Record<string, unknown>) => unknown;

/** A scripted daemon: answers calls, records them, emits events into the actions. */
class FakeDaemon {
  readonly calls: Array<{ method: string; params: Record<string, unknown> }> = [];
  readonly handlers = new Map<string, Handler>();
  private seq = 0;
  private admissions = 0;
  actions: ChatActions | null = null;

  request: Request = (async (method: string, params: unknown) => {
    const p = params as Record<string, unknown>;
    this.calls.push({ method, params: p });
    const handler = this.handlers.get(method);
    if (handler === undefined) throw rpcError("METHOD_NOT_FOUND");
    return handler(p);
  }) as Request;

  emit(event: Record<string, unknown>): void {
    this.seq += 1;
    this.actions?.handleEvent({ session_id: SID, seq: this.seq, ...event } as SessionEvent);
  }

  nextAdmission(): string {
    this.admissions += 1;
    return `adm-${this.admissions}`;
  }

  called(method: string) {
    return this.calls.filter((call) => call.method === method);
  }
}

class MemoryJournal implements PreparedJournalPort {
  readonly entries = new Map<string, { sessionId: string; state: string; payload: unknown }>();
  readonly log: string[] = [];
  async prepare(entry: { requestId: string; sessionId: string; payload: unknown }) {
    this.log.push(`prepare ${entry.requestId}`);
    this.entries.set(entry.requestId, { ...entry, state: "prepared" });
    return { ok: true };
  }
  async retire(entry: { requestId: string; receipt: { outcome: string } }) {
    this.log.push(`retire ${entry.requestId} ${entry.receipt.outcome}`);
    const found = this.entries.get(entry.requestId);
    if (found === undefined) return { retired: false };
    if (entry.receipt.outcome === "unknown") {
      found.state = "uncertain";
      return { retired: false };
    }
    this.entries.delete(entry.requestId);
    return { retired: true };
  }
  async list() {
    return {
      entries: [...this.entries].map(([requestId, entry]) => ({ requestId, ...entry })),
    };
  }
}

function setup(options: { journal?: PreparedJournalPort | null } = {}) {
  const daemon = new FakeDaemon();
  let ids = 0;
  const actions = new ChatActions({
    sessionId: SID,
    request: daemon.request,
    journal: options.journal ?? null,
    newId: () => {
      ids += 1;
      return `req-${ids}`;
    },
    sleep: async () => {},
  });
  daemon.actions = actions;
  // A daemon that runs the first input and queues the rest while it runs.
  let running = false;
  daemon.handlers.set("prompt.submit", (params) => {
    const admission = daemon.nextAdmission();
    if (running || params.when_busy === "queue") {
      const position = actions.snapshot().queue.length + 1;
      daemon.emit({ type: "admission.state", admission_id: admission, state: "queued", position });
      return { turn_id: admission, status: "queued" };
    }
    running = true;
    return { turn_id: admission, generation: 7, status: "streaming" };
  });
  daemon.handlers.set("session.interrupt", () => ({ interrupted: true }));
  daemon.handlers.set("admission.cancel", (params) => {
    daemon.emit({
      type: "admission.state",
      admission_id: params.admission_id,
      state: "terminal",
      outcome: "cancelled",
    });
    return { cancelled: true };
  });
  return { daemon, actions, setRunning: (value: boolean) => (running = value) };
}

describe("busy actions", () => {
  it("stop carries the generation of the running turn", async () => {
    const { daemon, actions } = setup();
    expect(await actions.send("hello")).toBe("started");
    daemon.emit({ type: "turn.started", turn_id: "adm-1", generation: 7 });
    expect(await actions.stop()).toBe(true);
    expect(daemon.called("session.interrupt")[0]?.params).toEqual({
      session_id: SID,
      generation: 7,
    });
  });

  it("stop waits for the submit answer instead of interrupting blindly", async () => {
    const { daemon, actions } = setup();
    let release: () => void = () => {};
    daemon.handlers.set(
      "prompt.submit",
      () =>
        new Promise((resolve) => {
          release = () => resolve({ turn_id: "adm-9", generation: 3, status: "streaming" });
        }),
    );
    const sending = actions.send("go");
    const stopping = actions.stop();
    await Promise.resolve();
    expect(daemon.called("session.interrupt")).toHaveLength(0);
    release();
    await sending;
    await stopping;
    expect(daemon.called("session.interrupt")[0]?.params.generation).toBe(3);
  });

  it("stop with a queue pauses it and Resume sends the items back in order", async () => {
    const { daemon, actions, setRunning } = setup();
    await actions.send("first");
    daemon.emit({ type: "turn.started", turn_id: "adm-1", generation: 7 });
    expect(await actions.send("second")).toBe("queued");
    expect(await actions.queue("third")).toBe("queued");
    expect(actions.snapshot().queue.map((entry) => entry.text)).toEqual(["second", "third"]);

    await actions.stop();
    const paused = actions.snapshot();
    expect(paused.paused).toBe(true);
    expect(paused.queue).toEqual([]);
    expect(paused.held.map((item) => item.text)).toEqual(["second", "third"]);
    expect(daemon.called("admission.cancel").map((call) => call.params.admission_id)).toEqual([
      "adm-2",
      "adm-3",
    ]);

    daemon.emit({ type: "turn.done", turn_id: "adm-1", reason: "interrupted" });
    setRunning(false);
    const before = daemon.called("prompt.submit").length;
    await actions.resume();
    const resent = daemon.called("prompt.submit").slice(before);
    expect(resent.map((call) => call.params.text)).toEqual(["second", "third"]);
    expect(resent.every((call) => call.params.when_busy === "queue")).toBe(true);
    expect(actions.snapshot().held).toEqual([]);
    expect(actions.snapshot().paused).toBe(false);
  });

  it("an unknown turn shows one Discard, blocks the queue and is never resubmitted", async () => {
    const { daemon, actions } = setup();
    daemon.handlers.set("admission.discard", () => ({ discarded: true }));
    daemon.emit({
      type: "admission.state",
      admission_id: "adm-lost",
      state: "unknown",
      generation: 4,
    });
    const blocked = actions.snapshot();
    expect(blocked.lost).toEqual({ admissionId: "adm-lost", generation: 4 });
    // resume is blocked while the lost turn is unresolved
    await actions.resume();
    expect(daemon.called("prompt.submit")).toHaveLength(0);
    expect(await actions.discard()).toBe(true);
    expect(daemon.called("admission.discard")[0]?.params).toEqual({
      session_id: SID,
      admission_id: "adm-lost",
      generation: 4,
    });
    expect(actions.snapshot().lost).toBeNull();
    expect(daemon.called("prompt.submit")).toHaveLength(0);
  });

  it("a lost turn without a generation asks the receipt for it", async () => {
    const { daemon, actions } = setup();
    daemon.handlers.set("admission.receipt", () => ({
      admission_id: "adm-lost",
      session_id: SID,
      state: "unknown",
      generation: 9,
    }));
    daemon.handlers.set("admission.discard", () => ({}));
    daemon.emit({ type: "admission.state", admission_id: "adm-lost", state: "unknown" });
    await actions.discard();
    expect(daemon.called("admission.discard")[0]?.params.generation).toBe(9);
  });

  it("the fifth drain failure is not attempted", async () => {
    const { daemon, actions } = setup();
    await actions.send("first");
    daemon.emit({ type: "turn.started", turn_id: "adm-1", generation: 7 });
    await actions.queue("waiting");
    await actions.stop();
    daemon.handlers.set("prompt.submit", () => {
      throw rpcError("QUEUE_FULL");
    });
    const before = daemon.called("prompt.submit").length;
    await actions.resume();
    expect(daemon.called("prompt.submit").length - before).toBe(MAX_AUTO_DRAIN_ATTEMPTS);
    const [item] = actions.snapshot().held;
    expect(item).toMatchObject({ text: "waiting", failures: 4, failed: true });
    expect(actions.snapshot().paused).toBe(true);
    // a manual Resume starts a fresh round
    await actions.resume();
    expect(daemon.called("prompt.submit").length - before).toBe(2 * MAX_AUTO_DRAIN_ATTEMPTS);
  });

  it("steer goes to the running turn with its generation; a refused steer becomes a prompt", async () => {
    const { daemon, actions, setRunning } = setup();
    await actions.send("first");
    daemon.emit({ type: "turn.started", turn_id: "adm-1", generation: 7 });
    daemon.handlers.set("session.steer", () => ({ queued: true, ticket: "t" }));
    expect(await actions.steer("focus on tests")).toBe("steered");
    expect(daemon.called("session.steer")[0]?.params).toEqual({
      session_id: SID,
      text: "focus on tests",
      generation: 7,
    });
    daemon.handlers.set("session.steer", () => ({ queued: false }));
    setRunning(false);
    expect(await actions.steer("late guidance")).toBe("started");
    expect(daemon.called("prompt.submit").at(-1)?.params.text).toBe("late guidance");
  });

  it("a busy refusal on an idle-looking session queues the same input", async () => {
    const { daemon, actions } = setup();
    let first = true;
    daemon.handlers.set("prompt.submit", (params) => {
      if (first) {
        first = false;
        throw rpcError("SESSION_BUSY");
      }
      return { turn_id: "adm-5", status: params.when_busy === "queue" ? "queued" : "streaming" };
    });
    expect(await actions.send("hi")).toBe("queued");
    const [a, b] = daemon.called("prompt.submit");
    expect(a?.params.request_id).toBe(b?.params.request_id);
    expect(b?.params.when_busy).toBe("queue");
  });

  it("journals every submit: prepare, then retire with the receipt", async () => {
    const journal = new MemoryJournal();
    const { daemon, actions } = setup({ journal });
    await actions.send("hello");
    expect(journal.log).toEqual(["prepare req-1", "retire req-1 accepted"]);
    expect(daemon.called("prompt.submit")[0]?.params.request_id).toBe("req-1");
    expect(journal.entries.size).toBe(0);
  });

  it("an uncertain answer keeps the entry and a retry reuses the request id", async () => {
    const journal = new MemoryJournal();
    const { daemon, actions } = setup({ journal });
    daemon.handlers.set("prompt.submit", () => {
      throw new RpcTimeout("prompt.submit", 1);
    });
    expect(await actions.send("maybe")).toBe("uncertain");
    expect(journal.entries.get("req-1")?.state).toBe("uncertain");
    expect(actions.snapshot().uncertain).toEqual([{ requestId: "req-1", text: "maybe" }]);
    daemon.handlers.set("prompt.submit", () => ({
      turn_id: "adm-1",
      generation: 1,
      status: "streaming",
    }));
    expect(await actions.retryUncertain("req-1")).toBe("started");
    expect(daemon.called("prompt.submit").map((call) => call.params.request_id)).toEqual([
      "req-1",
      "req-1",
    ]);
    expect(journal.entries.size).toBe(0);
    expect(actions.snapshot().uncertain).toEqual([]);
  });

  it("offers prepared entries after a restart, for this session only", async () => {
    const journal = new MemoryJournal();
    await journal.prepare({ requestId: "old-1", sessionId: SID, payload: { text: "left over" } });
    await journal.prepare({
      requestId: "old-2",
      sessionId: "other",
      payload: { text: "not mine" },
    });
    const { actions, daemon } = setup({ journal });
    expect(await actions.recoverPrepared()).toEqual([{ requestId: "old-1", text: "left over" }]);
    // never sent on its own
    expect(daemon.called("prompt.submit")).toHaveLength(0);
    await actions.dismissUncertain("old-1");
    expect(journal.entries.has("old-1")).toBe(false);
  });

  it("listing the journal mid-submit never offers the input being sent", async () => {
    const journal = new MemoryJournal();
    const { daemon, actions } = setup({ journal });
    let answer: (value: unknown) => void = () => undefined;
    daemon.handlers.set("prompt.submit", () => new Promise((resolve) => (answer = resolve)));
    const sent = actions.send("first message");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(journal.entries.size).toBe(1);
    expect(await actions.recoverPrepared()).toEqual([]);
    answer({ turn_id: "t1", generation: 1, status: "started" });
    expect(await sent).toBe("started");
    expect(await actions.recoverPrepared()).toEqual([]);
    expect(actions.snapshot().uncertain).toEqual([]);
  });

  it("one actions object per session and connection", () => {
    const connection = {};
    const make = () => setup({}).actions;
    const first = sharedActions(connection, "s1", make);
    expect(sharedActions(connection, "s1", make)).toBe(first);
    expect(sharedActions(connection, "s2", make)).not.toBe(first);
    expect(sharedActions({}, "s1", make)).not.toBe(first);
  });

  it("a journal that refuses means nothing is sent", async () => {
    const journal = new MemoryJournal();
    journal.prepare = async () => {
      throw new Error("too-large: the input is too big");
    };
    const { daemon, actions } = setup({ journal });
    expect(await actions.send("x")).toBe("rejected");
    expect(daemon.called("prompt.submit")).toHaveLength(0);
    expect(actions.snapshot().error).toContain("too-large");
  });

  it("a definite rejection retires the entry as rejected", async () => {
    const journal = new MemoryJournal();
    const { daemon, actions } = setup({ journal });
    daemon.handlers.set("prompt.submit", () => {
      throw rpcError("TEXT_EMPTY");
    });
    expect(await actions.send("   x")).toBe("rejected");
    expect(journal.log.at(-1)).toBe("retire req-1 rejected");
  });
});

// W47-a: the daemon's turn ids (`turn.started` / `turn.done`) are not the admission ids the submit
// answer and `admission.state` carry; the two arrive in either order.
describe("busy state ends with the turn, whatever order the answers arrive in", () => {
  /** The daemon starts the turn under its own id while the submit is still answering. */
  function racing(events: ReadonlyArray<Record<string, unknown>>) {
    const { daemon, actions } = setup();
    daemon.handlers.set("prompt.submit", () => {
      for (const event of events) daemon.emit(event);
      return { turn_id: "adm-1", generation: 7, status: "streaming" };
    });
    return { daemon, actions };
  }

  it("turn.done ends it when the submit answer came after turn.started", async () => {
    const { daemon, actions } = racing([
      { type: "turn.started", turn_id: "turn-a", generation: 7 },
    ]);
    expect(await actions.send("hi")).toBe("started");
    expect(actions.snapshot().running).toBe(true);
    daemon.emit({ type: "turn.done", turn_id: "turn-a", reason: "completed" });
    expect(actions.snapshot()).toMatchObject({ running: false, generation: null });
  });

  it("a submit answer that arrives after the turn ended does not bring Stop back", async () => {
    const { actions } = racing([
      { type: "turn.started", turn_id: "turn-a", generation: 7 },
      { type: "turn.done", turn_id: "turn-a", reason: "completed" },
    ]);
    expect(await actions.send("hi")).toBe("started");
    expect(actions.snapshot().running).toBe(false);
    // the optimistic row still binds to the turn the transcript knows
    expect(actions.snapshot().turnId).toBe("turn-a");
  });

  it("admission started after turn.started keeps the turn's own id", () => {
    const { daemon, actions } = setup();
    daemon.emit({ type: "turn.started", turn_id: "turn-a", generation: 7 });
    daemon.emit({
      type: "admission.state",
      admission_id: "adm-1",
      state: "started",
      generation: 7,
    });
    daemon.emit({ type: "turn.done", turn_id: "turn-a", reason: "completed" });
    expect(actions.snapshot().running).toBe(false);
  });

  it("admission started after turn.done changes nothing", () => {
    const { daemon, actions } = setup();
    daemon.emit({ type: "turn.started", turn_id: "turn-a", generation: 7 });
    daemon.emit({ type: "turn.done", turn_id: "turn-a", reason: "completed" });
    daemon.emit({
      type: "admission.state",
      admission_id: "adm-1",
      state: "started",
      generation: 7,
    });
    expect(actions.snapshot().running).toBe(false);
  });

  it("turn.done of an unseen turn ends a turn known only by its admission", async () => {
    const { daemon, actions } = setup();
    expect(await actions.send("hi")).toBe("started");
    daemon.emit({ type: "turn.done", turn_id: "turn-z", reason: "completed" });
    expect(actions.snapshot().running).toBe(false);
  });

  it("an earlier turn's end does not stop the next turn", () => {
    const { daemon, actions } = setup();
    daemon.emit({ type: "turn.started", turn_id: "turn-a", generation: 7 });
    daemon.emit({ type: "turn.started", turn_id: "turn-b", generation: 8 });
    daemon.emit({ type: "turn.done", turn_id: "turn-a", reason: "completed" });
    expect(actions.snapshot()).toMatchObject({ running: true, turnId: "turn-b", generation: 8 });
  });

  it("the shared actions hear the session's events with no composer mounted", async () => {
    const listeners = new Set<(event: SessionEvent) => void>();
    const connection = {
      onSessionEvent(sessionId: string, listener: (event: SessionEvent) => void) {
        expect(sessionId).toBe(SID);
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
    };
    const { daemon, actions } = setup();
    daemon.actions = null; // nothing feeds it but the connection
    const shared = sharedActions(connection, SID, () => actions);
    expect(await shared.send("from the fresh draft")).toBe("started");
    for (const event of [
      { type: "turn.started", turn_id: "turn-a", generation: 7 },
      { type: "turn.done", turn_id: "turn-a", reason: "completed" },
    ]) {
      for (const listener of listeners) listener({ session_id: SID, ...event } as SessionEvent);
    }
    expect(shared.snapshot().running).toBe(false);
    // asking again does not subscribe twice
    sharedActions(connection, SID, () => actions);
    expect(listeners.size).toBe(1);
  });
});
