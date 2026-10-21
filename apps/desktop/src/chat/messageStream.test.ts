import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { ClientEvent } from "@indus/client";
import type { HistoryMessage } from "@indus/protocol";
import { describe, expect, it } from "vitest";
import {
  checkFrames,
  checkStreamIntegrity,
  checkTranscriptOnce,
  persistedMessages,
  type ShownMessage,
  type StreamRecord,
} from "../../testing/oracle.ts";
import { classifyFailure, failureActions } from "./failure.ts";
import {
  type AssistantItem,
  addLocalUser,
  applyEvent,
  applyHistory,
  bindLocalUser,
  emptyTranscript,
  reconcileFinal,
  type SessionTranscript,
  type TranscriptItem,
} from "./messageStream.ts";
import { toolImages, toolOutcome, toolSummary } from "./toolSummary.ts";

const FIXTURES = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../testing/fixtures/streams",
);

type Step =
  | { op: "local_user"; local: string; text: string }
  | { op: "bind"; local: string; turn_id: string }
  | { op: "event"; event: Record<string, unknown>; replayed?: boolean }
  | { op: "hydrate"; id: string; rows: HistoryMessage[] }
  | { op: "resolve"; id: string }
  | { op: "reconnect" };

interface StreamFixture {
  name: string;
  session_id: string;
  steps: Step[];
  streamed: Record<string, StreamRecord>;
  history_after: HistoryMessage[];
  final_hydrate?: boolean;
}

function streamFixtures(): StreamFixture[] {
  return fs
    .readdirSync(FIXTURES)
    .filter((name) => name.endsWith(".json"))
    .map((name) => JSON.parse(fs.readFileSync(path.join(FIXTURES, name), "utf8")))
    .filter((fixture): fixture is StreamFixture => Array.isArray(fixture.steps));
}

/** What the screen shows: user rows, and each run of reply text between tool calls. */
function shown(items: readonly TranscriptItem[]): ShownMessage[] {
  const out: ShownMessage[] = [];
  for (const item of items) {
    if (item.kind === "user") out.push({ role: "user", text: item.text });
    if (item.kind !== "assistant") continue;
    let run = "";
    for (const part of item.parts) {
      if (part.type === "text") run += part.text;
      if (part.type === "tool" && run !== "") {
        out.push({ role: "assistant", text: run });
        run = "";
      }
    }
    if (run !== "") out.push({ role: "assistant", text: run });
  }
  return out;
}

function run(fixture: StreamFixture) {
  let t = emptyTranscript(fixture.session_id);
  const frames: ShownMessage[][] = [];
  const events: Record<string, unknown>[] = [];
  const pending = new Map<string, { generation: number; rows: HistoryMessage[] }>();
  let generation = 0;
  for (const step of fixture.steps) {
    switch (step.op) {
      case "local_user":
        t = addLocalUser(t, step.local, step.text);
        break;
      case "bind":
        t = bindLocalUser(t, step.local, step.turn_id);
        break;
      case "event":
        events.push(step.event);
        t = applyEvent(t, step.event as unknown as ClientEvent);
        break;
      case "hydrate":
        generation += 1;
        pending.set(step.id, { generation, rows: step.rows });
        break;
      case "resolve": {
        const load = pending.get(step.id);
        if (load) t = applyHistory(t, load.rows, load.generation);
        break;
      }
      case "reconnect":
        break;
    }
    frames.push(shown(t.items));
  }
  if (fixture.final_hydrate) {
    t = applyHistory(t, fixture.history_after, generation + 1);
    frames.push(shown(t.items));
  }
  return { t, frames, events };
}

describe("stream fixtures under the transcript-once oracle", () => {
  for (const fixture of streamFixtures()) {
    it(fixture.name, () => {
      const { t, frames, events } = run(fixture);
      expect(checkTranscriptOnce(persistedMessages(fixture.history_after), shown(t.items))).toEqual(
        [],
      );
      expect(checkFrames(frames)).toEqual([]);
      expect(checkStreamIntegrity(events, fixture.streamed)).toEqual([]);
      expect(t.running).toBe(false);
    });
  }

  it("the reconnect splice shows each word once before any snapshot", () => {
    const fixture = streamFixtures().find((f) => f.name === "reconnect-splice");
    if (!fixture) throw new Error("fixture missing");
    const before = { ...fixture, steps: fixture.steps.filter((step) => step.op !== "resolve") };
    const { t } = run(before);
    expect(shown(t.items)).toEqual([
      { role: "user", text: "count to seven" },
      { role: "assistant", text: "One two three four five six seven." },
    ]);
  });
});

function started(t: SessionTranscript): SessionTranscript {
  return applyEvent(t, {
    type: "turn.started",
    session_id: "s1",
    seq: 1,
    turn_id: "t1",
    generation: 1,
  } as ClientEvent);
}

describe("applyEvent", () => {
  it("ignores a repeated seq and another session's events", () => {
    const t = started(emptyTranscript("s1"));
    const delta = {
      type: "message.delta",
      session_id: "s1",
      seq: 2,
      turn_id: "t1",
      text: "x",
    } as ClientEvent;
    const once = applyEvent(t, delta);
    expect(applyEvent(once, delta)).toBe(once);
    expect(applyEvent(once, { ...delta, session_id: "s2", seq: 3 } as ClientEvent)).toBe(once);
  });

  it("opens an item for a turn joined mid-way and attaches late tool events to it", () => {
    let t = emptyTranscript("s1");
    t = applyEvent(t, {
      type: "message.delta",
      session_id: "s1",
      seq: 5,
      turn_id: "t7",
      text: "tail",
    } as ClientEvent);
    t = applyEvent(t, {
      type: "turn.done",
      session_id: "s1",
      seq: 6,
      turn_id: "t7",
      reason: "completed",
    } as ClientEvent);
    t = applyEvent(t, {
      type: "tool.completed",
      session_id: "s1",
      seq: 7,
      turn_id: "t7",
      call_id: "c9",
      name: "terminal",
      ok: true,
      duration_ms: 1,
      result: "",
    } as ClientEvent);
    expect(t.items).toHaveLength(1);
    const item = t.items[0] as AssistantItem;
    expect(item.sawStart).toBe(false);
    expect(item.parts.map((p) => p.type)).toEqual(["text", "tool"]);
  });

  it("binds the newest unbound local row when the turn starts before the submit answer", () => {
    let t = addLocalUser(emptyTranscript("s1"), "L1", "hi");
    t = started(t);
    expect(t.items.map((i) => i.kind)).toEqual(["user", "assistant"]);
    expect((t.items[0] as { turnId: string | null }).turnId).toBe("t1");
  });

  it("ends a failed turn with an error card naming the layer", () => {
    let t = started(emptyTranscript("s1"));
    t = applyEvent(t, {
      type: "turn.done",
      session_id: "s1",
      seq: 2,
      turn_id: "t1",
      reason: "provider_failed",
      failure: { reason: "HTTP 401: invalid api key", retryable: false },
    } as ClientEvent);
    const error = t.items.find((item) => item.kind === "error");
    expect(error).toMatchObject({ kind: "error", layer: "auth", retryable: false });
    expect(t.running).toBe(false);
  });

  it("an interrupted turn seals running tools as neutral and adds no error", () => {
    let t = started(emptyTranscript("s1"));
    t = applyEvent(t, {
      type: "tool.started",
      session_id: "s1",
      seq: 2,
      turn_id: "t1",
      call_id: "c1",
      name: "terminal",
    } as ClientEvent);
    t = applyEvent(t, {
      type: "turn.done",
      session_id: "s1",
      seq: 3,
      turn_id: "t1",
      reason: "interrupted_by_user",
    } as ClientEvent);
    const item = t.items[0] as AssistantItem;
    expect(item.interrupted).toBe(true);
    expect(item.parts[0]).toMatchObject({ state: "neutral" });
    expect(t.items.some((i) => i.kind === "error")).toBe(false);
  });

  it("keys notices so a later one with the same key replaces the first", () => {
    let t = emptyTranscript("s1");
    for (const [seq, text] of [
      [1, "low disk"],
      [2, "very low disk"],
    ] as const) {
      t = applyEvent(t, {
        type: "notice",
        session_id: "s1",
        seq,
        level: "warning",
        key: "disk",
        text,
      } as ClientEvent);
    }
    expect(t.items).toEqual([
      { kind: "notice", key: "notice:disk", level: "warning", text: "very low disk" },
    ]);
  });
});

describe("reconcileFinal", () => {
  const item = (texts: string[]): AssistantItem => ({
    kind: "assistant",
    key: "k",
    turnId: "t",
    parts: texts.map((text) =>
      text === "|"
        ? {
            type: "tool",
            callId: "c",
            name: "x",
            args: null,
            state: "ok",
            progress: null,
            fraction: null,
            result: "",
            durationMs: 1,
          }
        : { type: "text", text },
    ),
    streaming: true,
    persisted: false,
    interrupted: false,
    sawStart: true,
  });
  const texts = (a: AssistantItem) => a.parts.map((p) => (p.type === "text" ? p.text : "|"));

  it("adds only the missing tail", () => {
    expect(texts(reconcileFinal(item(["Hello"]), "Hello there"))).toEqual(["Hello there"]);
  });
  it("leaves a matching closing message alone", () => {
    const original = item(["Looking.", "|", "Done."]);
    expect(reconcileFinal(original, "Done.")).toBe(original);
    expect(reconcileFinal(original, "Looking.Done.")).toBe(original);
  });
  it("replaces a closing message the stream garbled", () => {
    expect(texts(reconcileFinal(item(["Looking.", "|", "Dne."]), "Done."))).toEqual([
      "Looking.",
      "|",
      "Done.",
    ]);
  });
  it("adds the final when nothing streamed", () => {
    expect(texts(reconcileFinal(item([]), "Only final"))).toEqual(["Only final"]);
  });
});

describe("applyHistory", () => {
  const rows = (n: number): HistoryMessage[] =>
    Array.from({ length: n }, (_, i) => ({
      role: i % 2 === 0 ? "user" : "assistant",
      text: `m${i}`,
      row_id: i + 1,
    }));

  it("ignores a snapshot older than the one applied", () => {
    const newer = applyHistory(emptyTranscript("s1"), rows(4), 2);
    expect(applyHistory(newer, rows(2), 1)).toBe(newer);
  });

  it("a repeated user text matches the right turn, not an earlier one", () => {
    const history: HistoryMessage[] = [
      { role: "user", text: "again", row_id: 1 },
      { role: "assistant", text: "first", row_id: 2 },
    ];
    let t = applyHistory(emptyTranscript("s1"), history, 1);
    t = addLocalUser(t, "L1", "again");
    t = started({ ...t, lastSeq: 0 });
    t = applyEvent(t, {
      type: "message.delta",
      session_id: "s1",
      seq: 2,
      turn_id: "t1",
      text: "second",
    } as ClientEvent);
    t = applyHistory(t, [...history, { role: "user", text: "again", row_id: 3 }], 2);
    expect(shown(t.items).map((m) => m.text)).toEqual(["again", "first", "again", "second"]);
  });
});

describe("tool rows", () => {
  it("summarises with a verb and a target", () => {
    expect(toolSummary("terminal", { command: "ls -la" })).toEqual({
      verb: "run",
      target: "ls -la",
    });
    expect(toolSummary("read_file", { path: "/a/b.txt" })).toEqual({
      verb: "read",
      target: "/a/b.txt",
    });
    expect(toolSummary("web_search", { query: "indus" }).verb).toBe("search");
    expect(toolSummary("mystery", null)).toEqual({ verb: "use", target: "" });
  });

  it("is red only for explicit failures; a bare exit 1 is neutral", () => {
    expect(toolOutcome(true, "")).toBe("ok");
    expect(toolOutcome(false, '{"exit_code": 1, "output": "", "error": null}')).toBe("neutral");
    expect(toolOutcome(false, "exit code 1")).toBe("neutral");
    expect(toolOutcome(false, "Error: permission denied")).toBe("failed");
    expect(toolOutcome(false, "exit code 2")).toBe("failed");
  });

  it("finds images in a result", () => {
    expect(toolImages("see indus-media://shot/1.png and data:image/png;base64,AAAA")).toEqual([
      "indus-media://shot/1.png",
      "data:image/png;base64,AAAA",
    ]);
  });
});

describe("failure layers", () => {
  it.each([
    ["provider_failed", "rate limit reached (429)", "provider"],
    ["provider_failed", "HTTP 401 unauthorized", "auth"],
    ["provider_failed", "connection reset while streaming", "streaming"],
    ["provider_failed", "could not resolve the base url", "endpoint"],
    ["persistence_failed", "", "disk"],
    ["max_iterations", "", "runtime"],
    ["provider_failed", "", "provider"],
  ])("%s / %s → %s", (reason, text, layer) => {
    expect(classifyFailure(reason, text)).toBe(layer);
  });

  it("offers sign-in for auth and reconnect for the daemon", () => {
    expect(failureActions("auth", true)).toEqual(["sign-in", "open-settings"]);
    expect(failureActions("daemon", false)).toEqual(["reconnect", "open-logs"]);
    expect(failureActions("provider", false)).not.toContain("retry");
  });
});
