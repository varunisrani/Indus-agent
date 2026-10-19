import type { ClientEvent } from "@indus/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createEventHandler, type EventHandler } from "../events/handleEvent.ts";
import { INTERRUPT_COOLDOWN_MS } from "../events/timing.ts";
import { focusSession } from "../state/destination.ts";
import { overlayStore } from "../state/overlayStore.ts";
import { transcriptStore } from "../state/transcriptStore.ts";
import { turnStore } from "../state/turnStore.ts";
import { uiStore } from "../state/uiStore.ts";
import { resetStores } from "../testing/harness.ts";
import { loadFixture } from "../testing/mockGateway.ts";
import {
  INTERRUPTED_MARK,
  NOTHING_STREAMED_NOTE,
  TurnController,
  thinkingPreview,
  unseenTail,
} from "./controller.ts";

beforeEach(() => {
  vi.useFakeTimers();
  resetStores();
});
afterEach(() => {
  vi.useRealTimers();
});

interface Setup {
  controller: TurnController;
  handle: EventHandler;
  calls: { method: string; params: unknown }[];
}

function setup(sid: string, call?: (method: string, params: unknown) => Promise<unknown>): Setup {
  const calls: Setup["calls"] = [];
  const controller = new TurnController({
    rpc: {
      call: (method, params) => {
        calls.push({ method, params });
        return call ? call(method, params) : Promise.resolve({ interrupted: true });
      },
    },
  });
  focusSession(sid);
  return { controller, handle: createEventHandler({ controller }), calls };
}

/** Feed a fixture's events (not asks or other lines) until `stop` says so. */
function feed(
  name: string,
  handle: EventHandler,
  stop?: (event: Record<string, unknown>) => boolean,
): void {
  for (const step of loadFixture(name).steps) {
    if (step.kind !== "event") continue;
    if (stop?.(step.event)) return;
    handle(step.event as unknown as ClientEvent);
  }
}

const messages = () => transcriptStore.getState().messages;
const texts = () => messages().map((m) => m.text);

describe("segments", () => {
  it("seals commentary at tool boundaries: 2 text segments with the 3 tools attached", () => {
    const { handle } = setup("s-tools");
    feed("tools-and-diff", handle, (e) => e.type === "turn.done");
    const segments = turnStore.getState().segments;
    const textSegments = segments.filter((s) => s.kind !== "diff");
    expect(textSegments.map((s) => s.text)).toEqual([
      "I'll check the config first.",
      "Found the setting. Editing it now.",
    ]);
    expect(textSegments.map((s) => s.tools.map((t) => t.name))).toEqual([["read_file"], ["patch"]]);
    const diffs = segments.filter((s) => s.kind === "diff");
    expect(diffs).toHaveLength(1);
    expect(diffs[0]?.id.startsWith("diff")).toBe(true);
    expect(textSegments.every((s) => !s.id.startsWith("diff"))).toBe(true);
    expect(turnStore.getState().pendingTools.map((t) => t.name)).toEqual(["todo_list"]);
    expect(turnStore.getState().trail.map((t) => t.name)).toEqual([
      "read_file",
      "patch",
      "todo_list",
    ]);
  });

  it("shows only the unseen tail of the final text", () => {
    const { handle } = setup("s-tools");
    feed("tools-and-diff", handle);
    expect(texts().at(-1)).toBe(
      "Done: the timeout is now 30 seconds. Restart the service to apply it.",
    );
    expect(texts().filter((t) => t.includes("I'll check the config first."))).toHaveLength(1);
    expect(
      messages()
        .at(-1)
        ?.tools?.map((t) => t.name),
    ).toEqual(["todo_list"]);
  });

  it("drops a diff segment the final text repeats", () => {
    const { controller } = setup("s1");
    controller.start("t1", 1);
    controller.delta("t1", "Editing.");
    controller.toolStart("t1", "c1", "patch", { path: "a.txt" });
    const diff = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-x\n+y";
    controller.toolComplete("t1", {
      callId: "c1",
      name: "patch",
      ok: true,
      durationMs: 4,
      result: diff,
    });
    controller.complete(
      "t1",
      `Editing.\n\nHere is the change:\n\n\`\`\`diff\n${diff}\n\`\`\``,
      "completed",
    );
    expect(messages().filter((m) => m.kind === "diff")).toEqual([]);
    expect(messages().map((m) => m.text)).toEqual([
      "Editing.",
      `Here is the change:\n\n\`\`\`diff\n${diff}\n\`\`\``,
    ]);
  });

  it("keeps a diff segment out of the transcript when inline diffs are off", () => {
    const { controller } = setup("s1");
    uiStore.getState().patch({ inlineDiffs: false });
    controller.start("t1", 1);
    controller.toolStart("t1", "c1", "patch");
    controller.toolComplete("t1", {
      callId: "c1",
      name: "patch",
      ok: true,
      result: "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b",
    });
    expect(turnStore.getState().segments).toEqual([]);
  });

  it("seals interim commentary with the server's text", () => {
    const { controller } = setup("s1");
    controller.start("t1", 1);
    controller.delta("t1", "Looking at the");
    controller.interim("t1", "Looking at the logs now.");
    expect(turnStore.getState().segments.map((s) => s.text)).toEqual(["Looking at the logs now."]);
    controller.complete("t1", "Looking at the logs now.\n\nNothing unusual.", "completed");
    expect(texts()).toEqual(["Looking at the logs now.", "Nothing unusual."]);
  });

  it("attaches tools that ran before any text to the final reply", () => {
    const { controller } = setup("s1");
    controller.start("t1", 1);
    controller.toolStart("t1", "c1", "read_file", { path: "README.md" });
    controller.toolComplete("t1", { callId: "c1", name: "read_file", ok: true, result: "# hi" });
    controller.complete("t1", "The readme only has a heading.", "completed");
    expect(messages()).toHaveLength(1);
    expect(messages()[0]?.tools?.map((t) => `${t.name}:${t.verb}:${t.args}`)).toEqual([
      "read_file:reading:README.md",
    ]);
  });

  it("marks a failed tool as an error row", () => {
    const { controller } = setup("s1");
    controller.start("t1", 1);
    controller.toolStart("t1", "c1", "terminal", { command: "false" });
    controller.toolComplete("t1", { callId: "c1", name: "terminal", ok: false, result: "exit 1" });
    expect(turnStore.getState().trail[0]?.status).toBe("error");
  });
});

describe("failures", () => {
  it("replaces an empty failed slot with a structured card", () => {
    const { handle } = setup("s-fail");
    feed("error-turn", handle);
    const last = messages().at(-1);
    expect(last?.kind).toBe("failure");
    expect(last?.failure).toEqual({ layer: "provider", code: "provider_failed", retryable: true });
    expect(last?.text).toBe("The provider refused the request: rate limit reached.");
  });

  it("replaces a bare 'Error:' final slot too", () => {
    const { controller } = setup("s1");
    controller.start("t1", 1);
    controller.done("t1", "persistence_failed", {
      reason: "The session store is read-only.",
      retryable: false,
    });
    controller.complete("t1", "Error: OSError(30)", "persistence_failed");
    expect(messages().map((m) => [m.kind, m.failure?.layer])).toEqual([["failure", "storage"]]);
  });

  it("keeps a partial reply's own text when the turn fails after streaming", () => {
    const { controller } = setup("s1");
    controller.start("t1", 1);
    controller.delta("t1", "Half an answer");
    controller.done("t1", "provider_failed", { reason: "connection reset", retryable: true });
    controller.complete("t1", "Half an answer", "provider_failed");
    expect(messages().map((m) => m.kind ?? "text")).toEqual(["text"]);
  });
});

describe("interrupt", () => {
  it("seals at once, ignores later deltas, and takes the longer stored partial", async () => {
    const { handle, controller, calls } = setup("s-stop");
    feed("interrupt-partial", handle, (e) => e.text === "a bulge of water");
    await controller.interrupt();
    expect(calls).toEqual([
      { method: "session.interrupt", params: { session_id: "s-stop", generation: 7 } },
    ]);
    expect(texts()).toEqual([`Tides follow the moon. Its pull raises\n\n${INTERRUPTED_MARK}`]);
    expect(uiStore.getState().statusText).toBe("interrupted");
    expect(uiStore.getState().busy).toBe(false);
    let after = false;
    feed("interrupt-partial", handle, (e) => {
      if (e.text === "a bulge of water") after = true;
      return false;
    });
    expect(after).toBe(true);
    expect(texts()).toEqual([
      `Tides follow the moon. Its pull raises a bulge of water\n\n${INTERRUPTED_MARK}`,
    ]);
    vi.advanceTimersByTime(INTERRUPT_COOLDOWN_MS);
    expect(uiStore.getState().statusText).toBe("ready");
  });

  it("with queued input stays busy until the real end and writes nothing twice", async () => {
    const { handle, controller } = setup("s-stop");
    feed("interrupt-partial", handle, (e) => e.text === "a bulge of water");
    await controller.interrupt({ keepBusy: true });
    expect(uiStore.getState().busy).toBe(true);
    expect(uiStore.getState().statusText).toBe("interrupting…");
    const busyEdges: boolean[] = [];
    const stop = uiStore.subscribe((s, prev) => {
      if (s.busy !== prev.busy) busyEdges.push(s.busy);
    });
    feed("interrupt-partial", handle, (e) => e.type === "turn.done");
    expect(uiStore.getState().busy).toBe(true);
    feed("interrupt-partial", handle);
    stop();
    expect(busyEdges).toEqual([false]);
    expect(messages()).toHaveLength(1);
    expect(texts()[0]?.match(/_interrupted_/g)).toHaveLength(1);
    expect(uiStore.getState().statusText).toBe("ready");
  });

  it("writes a note when nothing streamed, then the stored reply if one arrives", async () => {
    const { controller } = setup("s1");
    controller.start("t1", 1);
    await controller.interrupt();
    expect(texts()).toEqual([NOTHING_STREAMED_NOTE]);
    controller.complete("t1", "Partial thought", "interrupted_by_user");
    expect(texts()).toEqual([NOTHING_STREAMED_NOTE, `Partial thought\n\n${INTERRUPTED_MARK}`]);
  });

  it("shows the full reply when the completion won the race", async () => {
    const { controller } = setup("s1", async () => ({ interrupted: false }));
    controller.start("t1", 1);
    controller.delta("t1", "All done in");
    const answered = await controller.interrupt();
    expect(answered).toBe(false);
    controller.complete("t1", "All done in time.", "completed");
    expect(texts()).toEqual(["All done in time."]);
  });

  it("reports a failed interrupt call only while that turn is still current", async () => {
    let fail: (error: Error) => void = () => {};
    const { controller } = setup("s1", () => new Promise((_, reject) => (fail = reject)));
    controller.start("t1", 1);
    controller.delta("t1", "x");
    const pending = controller.interrupt();
    fail(new Error("pipe closed"));
    await pending;
    expect(texts().at(-1)).toBe("Could not stop the turn: pipe closed");

    resetStores();
    const second = setup("s2", () => new Promise((_, reject) => (fail = reject)));
    second.controller.start("t9", 1);
    const late = second.controller.interrupt();
    focusSession("s-elsewhere");
    fail(new Error("pipe closed"));
    await late;
    expect(texts().some((t) => t.startsWith("Could not stop"))).toBe(false);
  });

  it("does nothing when idle or already stopped", async () => {
    const { controller, calls } = setup("s1");
    expect(await controller.interrupt()).toBe(false);
    controller.start("t1", 1);
    await controller.interrupt();
    expect(await controller.interrupt()).toBe(false);
    expect(calls).toHaveLength(1);
  });

  it("closes the turn's open approval card", async () => {
    const { controller } = setup("s1");
    controller.start("t1", 1);
    overlayStore.getState().open({
      id: "a",
      kind: "approval",
      requestId: "r",
      payload: {},
      ownsInput: true,
      turnId: "t1",
    });
    overlayStore.getState().open({ id: "m", kind: "modelPicker", payload: {}, ownsInput: true });
    await controller.interrupt();
    expect(overlayStore.getState().card?.kind).toBe("modelPicker");
  });
});

describe("late events", () => {
  it("never bring back entries of a finished turn", () => {
    const { controller } = setup("s1");
    controller.start("t1", 1);
    controller.delta("t1", "Answer.");
    controller.complete("t1", "Answer.", "completed");
    controller.toolStart("t1", "late", "delegate", { goal: "x" });
    controller.toolComplete("t1", { callId: "late", name: "delegate", ok: true, result: "ok" });
    controller.delta("t1", " more");
    controller.complete("t1", "Answer. more", "completed");
    expect(turnStore.getState().activeTools).toEqual([]);
    expect(turnStore.getState().trail).toEqual([]);
    expect(uiStore.getState().busy).toBe(false);
    expect(texts()).toEqual(["Answer."]);
  });

  it("starts an unseen turn from its first event (resume mid-turn)", () => {
    const { controller } = setup("s1");
    controller.delta("t5", "resumed text");
    expect(uiStore.getState().busy).toBe(true);
    controller.complete("t5", "resumed text", "completed");
    expect(texts()).toEqual(["resumed text"]);
  });

  it("idles and clears the approval of the finished turn but keeps the model picker", () => {
    const { controller } = setup("s1");
    controller.start("t1", 1);
    overlayStore.getState().open({ id: "m", kind: "modelPicker", payload: {}, ownsInput: true });
    overlayStore.getState().open({
      id: "a",
      kind: "approval",
      requestId: "r",
      payload: {},
      ownsInput: true,
      turnId: "t1",
    });
    controller.complete("t1", "", "completed");
    expect(overlayStore.getState().card?.kind).toBe("modelPicker");
    expect(overlayStore.getState().waiting).toEqual([]);
  });
});

describe("helpers", () => {
  it("removes sealed texts from the start of the final text, in order", () => {
    expect(unseenTail("A.\n\nB.\n\nC.", ["A.", "B."])).toBe("C.");
    expect(unseenTail("Only C.", ["A.", "B."])).toBe("Only C.");
    expect(unseenTail("A.", ["A."])).toBe("");
  });

  it("previews the last 160 characters of reasoning on one line", () => {
    const preview = thinkingPreview(`start\n${"x".repeat(300)}\nend`);
    expect(preview.length).toBe(160);
    expect(preview.endsWith(" end")).toBe(true);
    expect(preview.includes("\n")).toBe(false);
  });

  it("caps the live row to its tail", () => {
    const { controller } = setup("s1");
    controller.start("t1", 1);
    controller.delta("t1", Array.from({ length: 400 }, (_, i) => `line ${i}`).join("\n"));
    vi.advanceTimersByTime(20);
    const live = turnStore.getState().streamBuffer.split("\n");
    expect(live).toHaveLength(240);
    expect(live.at(-1)).toBe("line 399");
  });

  it("does not paint deltas while streaming display is off, but keeps them", () => {
    const { controller } = setup("s1");
    uiStore.getState().patch({ streaming: false });
    controller.start("t1", 1);
    controller.delta("t1", "quiet");
    vi.advanceTimersByTime(100);
    expect(turnStore.getState().streamBuffer).toBe("");
    controller.complete("t1", "quiet", "completed");
    expect(texts()).toEqual(["quiet"]);
  });
});
