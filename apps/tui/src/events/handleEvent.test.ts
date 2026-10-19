import type { ClientEvent } from "@indus/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { focusSession } from "../state/destination.ts";
import { overlayStore } from "../state/overlayStore.ts";
import { transcriptStore } from "../state/transcriptStore.ts";
import { turnStore } from "../state/turnStore.ts";
import { uiStore } from "../state/uiStore.ts";
import { resetStores, rig } from "../testing/harness.ts";
import { themeStore } from "../theme/theme.ts";
import { TurnController } from "../turn/controller.ts";
import { createEventHandler, reportRpcFailure, storeSnapshot } from "./handleEvent.ts";
import { NOTICE_TTL_MS, PROTOCOL_WARNING_MS, STATUS_RESTORE_MS } from "./timing.ts";

beforeEach(() => {
  vi.useFakeTimers();
  resetStores();
});
afterEach(() => {
  vi.useRealTimers();
});

const S = "s-unit";
let seq = 0;
function ev(body: Record<string, unknown>): ClientEvent {
  seq += 1;
  return { session_id: S, seq, ...body } as unknown as ClientEvent;
}

function direct() {
  const controller = new TurnController();
  const handle = createEventHandler({ controller });
  focusSession(S);
  return { controller, handle };
}

const answers = {
  serverRequests: {
    approval: () => ({ kind: "approval" as const, choice: "once" as const }),
    clarify: () => ({
      kind: "clarify" as const,
      answers: { store: ["Postgres"], tables: ["users", "orders"] },
    }),
  },
};

describe("fixture replays end in a known state", () => {
  for (const name of [
    "plain-reply",
    "tools-and-diff",
    "approval-mid-turn",
    "clarify-two-questions",
    "error-turn",
    "long-reply",
    "sessions-and-models",
  ]) {
    it(name, async () => {
      const r = rig(name, { handlers: answers });
      const done = r.gateway.run();
      await vi.runAllTimersAsync();
      await done;
      await vi.runAllTimersAsync();
      expect(storeSnapshot()).toMatchSnapshot();
    });
  }

  it("interrupt-partial", async () => {
    const r = rig("interrupt-partial", { focus: "s-stop" });
    const done = r.gateway.run();
    await vi.advanceTimersByTimeAsync(60);
    expect(uiStore.getState().busy).toBe(true);
    void r.controller.interrupt();
    await vi.runAllTimersAsync();
    await done;
    await vi.runAllTimersAsync();
    expect(storeSnapshot()).toMatchSnapshot();
  });
});

describe("filters", () => {
  it("drops events of a session other than the focused one", () => {
    const { handle } = direct();
    handle(ev({ type: "turn.started", turn_id: "t1", generation: 1 }));
    handle({
      ...ev({ type: "message.delta", turn_id: "t1", text: "elsewhere" }),
      session_id: "s-other",
    } as ClientEvent);
    handle(ev({ type: "message.delta", turn_id: "t1", text: "here" }));
    vi.advanceTimersByTime(20);
    expect(turnStore.getState().streamBuffer).toBe("here");
  });

  it("drops a repeated seq (a replay after resume)", () => {
    const { handle } = direct();
    handle(ev({ type: "turn.started", turn_id: "t1", generation: 1 }));
    const delta = ev({ type: "message.delta", turn_id: "t1", text: "once " });
    handle(delta);
    handle(delta);
    vi.advanceTimersByTime(20);
    expect(turnStore.getState().streamBuffer).toBe("once ");
  });

  it("passes events before the session is known", () => {
    const controller = new TurnController();
    const handle = createEventHandler({ controller });
    handle(ev({ type: "session.title", title: "Early" }));
    expect(uiStore.getState().title).toBe("Early");
  });
});

describe("stream text", () => {
  it("appends raw text and never takes a rendered field", () => {
    const { handle } = direct();
    handle(ev({ type: "turn.started", turn_id: "t1", generation: 1 }));
    handle(
      ev({
        type: "message.delta",
        turn_id: "t1",
        text: "**bold** ",
        rendered: "\u001b[1mbold\u001b[0m",
      }),
    );
    handle(ev({ type: "message.delta", turn_id: "t1", text: "tail", rendered: "\u001b[2mtail" }));
    vi.advanceTimersByTime(20);
    expect(turnStore.getState().streamBuffer).toBe("**bold** tail");
    handle(ev({ type: "turn.done", turn_id: "t1", reason: "completed" }));
    handle(
      ev({
        type: "message.complete",
        turn_id: "t1",
        text: "**bold** tail",
        reason: "completed",
        rendered: "x",
      }),
    );
    expect(transcriptStore.getState().messages.map((m) => m.text)).toEqual(["**bold** tail"]);
  });

  it("trims an 85,000-character reasoning buffer to the last 60,000", () => {
    const { handle } = direct();
    handle(ev({ type: "turn.started", turn_id: "t1", generation: 1 }));
    const chunk = "r".repeat(999);
    for (let i = 0; i < 85; i++)
      handle(ev({ type: "reasoning.delta", turn_id: "t1", text: `${chunk}${i % 10}` }));
    vi.advanceTimersByTime(20);
    const reasoning = turnStore.getState().reasoning;
    expect(reasoning.length).toBeLessThanOrEqual(80_000);
    expect(reasoning.length).toBeGreaterThanOrEqual(60_000);
    expect(reasoning.endsWith(`${chunk}4`)).toBe(true);
  });

  it("trims exactly to 60,000 the moment the buffer passes 80,000", () => {
    const { handle } = direct();
    handle(ev({ type: "turn.started", turn_id: "t1", generation: 1 }));
    handle(ev({ type: "reasoning.delta", turn_id: "t1", text: "a".repeat(80_000) }));
    handle(ev({ type: "reasoning.delta", turn_id: "t1", text: "b".repeat(5_000) }));
    vi.advanceTimersByTime(20);
    expect(turnStore.getState().reasoning.length).toBe(60_000);
    expect(turnStore.getState().reasoning.endsWith("b".repeat(5_000))).toBe(true);
  });

  it("lights the reasoning indicator for 700 ms after the last piece", () => {
    const { handle } = direct();
    handle(ev({ type: "turn.started", turn_id: "t1", generation: 1 }));
    handle(ev({ type: "reasoning.delta", turn_id: "t1", text: "hmm" }));
    expect(turnStore.getState().reasoningActive).toBe(true);
    vi.advanceTimersByTime(699);
    expect(turnStore.getState().reasoningActive).toBe(true);
    vi.advanceTimersByTime(1);
    expect(turnStore.getState().reasoningActive).toBe(false);
  });
});

describe("status and activity", () => {
  it("shows a transient status and restores it after 4 s", () => {
    const { handle } = direct();
    handle(ev({ type: "turn.started", turn_id: "t1", generation: 1 }));
    handle(ev({ type: "status", kind: "retrying", text: "retrying (1 of 3)" }));
    expect(uiStore.getState().statusText).toBe("retrying (1 of 3)");
    expect(turnStore.getState().activity.map((a) => a.text)).toEqual(["retrying (1 of 3)"]);
    vi.advanceTimersByTime(STATUS_RESTORE_MS - 1);
    expect(uiStore.getState().statusText).toBe("retrying (1 of 3)");
    vi.advanceTimersByTime(1);
    expect(uiStore.getState().statusText).toBe("working…");
  });

  it("restores a goal status after 6 s and writes the goal line", () => {
    const { handle } = direct();
    handle(
      ev({
        type: "goal.update",
        status: "done",
        goal: "ship the export",
        turns_used: 3,
        max_turns: 10,
      }),
    );
    expect(uiStore.getState().statusText).toBe("goal reached");
    vi.advanceTimersByTime(5_999);
    expect(uiStore.getState().statusText).toBe("goal reached");
    vi.advanceTimersByTime(1);
    expect(uiStore.getState().statusText).toBe("ready");
    expect(transcriptStore.getState().messages.at(-1)?.text).toBe("Goal done: ship the export");
  });

  it("turns failure-looking stderr into one de-duplicated warning", () => {
    const { handle } = direct();
    handle({ type: "transport.stderr", line: "loading plugins" } as ClientEvent);
    handle({ type: "transport.stderr", line: "Traceback (most recent call last):" } as ClientEvent);
    handle({ type: "transport.stderr", line: "ValueError: bad config" } as ClientEvent);
    handle({ type: "transport.stderr", line: "ValueError: bad config" } as ClientEvent);
    const activity = turnStore.getState().activity;
    expect(activity).toHaveLength(1);
    expect(activity[0]).toMatchObject({ level: "warn", text: "bridge: ValueError: bad config" });
  });

  it("shows a protocol warning for 4 s and notes the first one", () => {
    const { handle } = direct();
    handle({ type: "transport.protocol_error", preview: "hello from a print()" } as ClientEvent);
    handle({ type: "transport.protocol_error", preview: "x".repeat(300) } as ClientEvent);
    expect(uiStore.getState().statusText).toBe("protocol warning");
    const texts = turnStore.getState().activity.map((a) => a.text);
    expect(texts.filter((t) => t.startsWith("The bridge printed"))).toHaveLength(1);
    expect(texts.at(-1)?.length).toBeLessThanOrEqual("noise: ".length + 120);
    vi.advanceTimersByTime(PROTOCOL_WARNING_MS);
    expect(uiStore.getState().statusText).toBe("ready");
  });

  it("merges usage only when a counter changed", () => {
    const { handle } = direct();
    handle(ev({ type: "session.usage", usage: { input_tokens: 5 } }));
    const first = uiStore.getState().usage;
    handle(ev({ type: "session.usage", usage: { input_tokens: 5 } }));
    expect(uiStore.getState().usage).toBe(first);
    handle(ev({ type: "session.usage", usage: { input_tokens: 9 } }));
    expect(uiStore.getState().usage).toEqual({ input_tokens: 9 });
  });
});

describe("notices", () => {
  it("holds a notice while busy and starts its timer when it shows", () => {
    const { handle } = direct();
    handle(ev({ type: "turn.started", turn_id: "t1", generation: 1 }));
    handle(ev({ type: "notice", level: "info", text: "Context is 80% full", key: "ctx" }));
    expect(uiStore.getState().notice).toBeNull();
    vi.advanceTimersByTime(NOTICE_TTL_MS * 2);
    handle(ev({ type: "turn.done", turn_id: "t1", reason: "completed" }));
    handle(ev({ type: "message.complete", turn_id: "t1", text: "ok", reason: "completed" }));
    expect(uiStore.getState().notice?.text).toBe("Context is 80% full");
    vi.advanceTimersByTime(NOTICE_TTL_MS - 1);
    expect(uiStore.getState().notice).not.toBeNull();
    vi.advanceTimersByTime(1);
    expect(uiStore.getState().notice).toBeNull();
  });

  it("clears only by matching key, and keeps errors until cleared", () => {
    const { handle } = direct();
    handle(ev({ type: "notice", level: "error", text: "Key rejected", key: "auth" }));
    handle(ev({ type: "notice", level: "info", text: "", key: "other" }));
    vi.advanceTimersByTime(NOTICE_TTL_MS * 3);
    expect(uiStore.getState().notice?.text).toBe("Key rejected");
    handle(ev({ type: "notice", level: "info", text: "", key: "auth" }));
    expect(uiStore.getState().notice).toBeNull();
  });
});

describe("server request withdrawals", () => {
  it("closes the matching card and explains a timed-out password prompt", () => {
    const { handle } = direct();
    overlayStore
      .getState()
      .open({ id: "c1", kind: "sudo", requestId: "req-1", payload: {}, ownsInput: true });
    handle(ev({ type: "server_request.cancelled", request_id: "req-1", reason: "timed_out" }));
    expect(overlayStore.getState().card).toBeNull();
    expect(transcriptStore.getState().messages.at(-1)?.text).toMatch(/password prompt timed out/);
  });

  it("closes an approval quietly when the turn was interrupted", () => {
    const { handle } = direct();
    overlayStore
      .getState()
      .open({ id: "c1", kind: "approval", requestId: "req-2", payload: {}, ownsInput: true });
    handle(ev({ type: "server_request.cancelled", request_id: "req-2", reason: "interrupted" }));
    expect(overlayStore.getState().card).toBeNull();
    expect(transcriptStore.getState().messages).toEqual([]);
  });
});

describe("lifecycle events", () => {
  it("applies the ready frame's theme and calls the bootstrap hook", () => {
    const onReady = vi.fn();
    const controller = new TurnController();
    const handle = createEventHandler({ controller, onReady });
    handle({ type: "gateway.ready", theme: "indus-light", protocol_version: 1 } as ClientEvent);
    expect(onReady).toHaveBeenCalledTimes(1);
    expect(themeStore.getState().theme.light).toBe(true);
    expect(uiStore.getState().connected).toBe(true);
  });

  it("resets the turn when the bridge exits, without writing the partial", () => {
    const { handle } = direct();
    handle(ev({ type: "turn.started", turn_id: "t1", generation: 1 }));
    handle(ev({ type: "message.delta", turn_id: "t1", text: "half" }));
    handle({ type: "transport.exit", code: 1, signal: null, stderr_last: null } as ClientEvent);
    expect(uiStore.getState().busy).toBe(false);
    expect(uiStore.getState().connected).toBe(false);
    expect(turnStore.getState().streamBuffer).toBe("");
    expect(transcriptStore.getState().messages).toEqual([]);
  });

  it("treats a failed call as a note, not a turn end", () => {
    const { handle } = direct();
    handle(ev({ type: "turn.started", turn_id: "t1", generation: 1 }));
    reportRpcFailure("Model switch", new Error("boom"));
    expect(uiStore.getState().busy).toBe(true);
    expect(transcriptStore.getState().messages.at(-1)?.text).toBe("Model switch failed: boom");
  });
});
