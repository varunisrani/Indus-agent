import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { rig } from "./harness.ts";
import { fixtureNames, loadFixture, parseFixture } from "./mockGateway.ts";
import { checkType } from "./schemaCheck.ts";

const EXPECTED = [
  "approval-mid-turn",
  "clarify-two-questions",
  "error-turn",
  "interrupt-partial",
  "long-reply",
  "plain-reply",
  "sessions-and-models",
  "tools-and-diff",
];

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("fixtures", () => {
  it("ships the eight recorded streams", () => {
    expect(fixtureNames()).toEqual(EXPECTED);
  });

  for (const name of EXPECTED) {
    it(`${name} validates against the generated protocol schema`, () => {
      const fixture = loadFixture(name);
      expect(fixture.steps.length).toBeGreaterThan(0);
      for (const step of fixture.steps) {
        if (step.kind === "ask") expect(checkType(step.request, "ServerRequest")).toEqual([]);
      }
    });
  }

  it("stamps seq per session, strictly increasing", () => {
    const seqs = loadFixture("tools-and-diff")
      .steps.filter((s) => s.kind === "event")
      .map((s) => (s.kind === "event" ? s.event.seq : 0));
    expect(seqs).toEqual(seqs.map((_, i) => i + 1));
  });

  it("expands the 10k-token reply and joins its deltas into the final text", () => {
    const steps = loadFixture("long-reply").steps;
    const deltas = steps.filter((s) => s.kind === "event" && s.event.type === "message.delta");
    expect(deltas).toHaveLength(2000);
    const final = steps.find((s) => s.kind === "event" && s.event.type === "message.complete");
    const text = final?.kind === "event" ? String(final.event.text) : "";
    expect(text.split(/\s+/).length).toBeGreaterThan(5000);
    expect(text.startsWith("Step 1: keep going.\nStep 2:")).toBe(true);
  });

  it("names the file and line of a bad step", () => {
    const bad = '{"event":{"type":"message.delta","session_id":"s","turn_id":"t"}}';
    expect(() => parseFixture(`# comment\n${bad}`, "broken")).toThrow(
      /broken\.ndjson:2: MessageDelta/,
    );
    expect(() => parseFixture('{"what":1}', "odd")).toThrow(/odd\.ndjson:1: unknown line kind/);
    expect(() => parseFixture('{"event":{"type":"nope","session_id":"s"}}', "x")).toThrow(
      /unknown event type/,
    );
  });
});

describe("replay", () => {
  for (const name of ["plain-reply", "tools-and-diff", "error-turn", "long-reply"]) {
    it(`replays ${name} in order with fake timers`, async () => {
      const r = rig(name);
      const done = r.gateway.run();
      await vi.runAllTimersAsync();
      await done;
      await vi.runAllTimersAsync();
      const fixture = r.gateway.fixture;
      const sent = fixture.steps
        .filter((s) => s.kind === "event")
        .map((s) => (s.kind === "event" ? s.event.seq : 0));
      const seen = r.events.filter((e) => "seq" in e).map((e) => (e as { seq: number }).seq);
      expect(seen).toEqual(sent);
      expect(r.events[0]?.type).toBe("gateway.ready");
    });
  }

  it("honours the delays it is given", async () => {
    const r = rig("plain-reply");
    const done = r.gateway.run();
    await vi.advanceTimersByTimeAsync(4);
    const early = r.events.length;
    await vi.advanceTimersByTimeAsync(30);
    expect(r.events.length).toBeGreaterThan(early);
    await vi.runAllTimersAsync();
    await done;
  });

  it("can run instantly with an injected timing", async () => {
    const r = rig("tools-and-diff", { timing: () => 0 });
    await r.gateway.run();
    await vi.runAllTimersAsync();
    expect(r.events.some((e) => e.type === "message.complete")).toBe(true);
  });

  it("waits for the client's answer to a server request", async () => {
    const r = rig("approval-mid-turn");
    const done = r.gateway.run();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(r.events.some((e) => e.type === "tool.completed")).toBe(false);
    const sentBefore = r.gateway.sent;
    r.client.handle({ serverRequests: { approval: () => ({ kind: "approval", choice: "once" }) } });
    await vi.runAllTimersAsync();
    await done;
    expect(r.gateway.sent).toBeGreaterThan(sentBefore);
    expect(r.gateway.answers).toEqual([{ kind: "approval", choice: "once" }]);
    expect(r.events.some((e) => e.type === "message.complete")).toBe(true);
  });

  it("waits until the client makes the awaited call", async () => {
    const r = rig("interrupt-partial");
    const done = r.gateway.run();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(r.events.some((e) => e.type === "turn.done")).toBe(false);
    await r.rpc.call("session.interrupt", { session_id: "s-stop", generation: 7 });
    await vi.runAllTimersAsync();
    await done;
    expect(r.gateway.callCount("session.interrupt")).toBe(1);
    expect(r.events.at(-1)?.type).toBe("message.complete");
  });

  it("answers methods from result lines", async () => {
    const r = rig("sessions-and-models");
    await r.gateway.run();
    const list = (await r.rpc.call("session.list", {})) as { sessions: { session_id: string }[] };
    expect(list.sessions.map((s) => s.session_id)).toEqual(["s-0003", "s-0002", "s-0001"]);
    const models = (await r.rpc.call("model.options", {})) as { model: string };
    expect(models.model).toBe("fake-small");
    const recent = (await r.rpc.call("session.most_recent", {})) as {
      session: { session_id: string };
    };
    expect(recent.session.session_id).toBe("s-0003");
  });

  it("stops between lines", async () => {
    const r = rig("long-reply");
    const done = r.gateway.run();
    await vi.advanceTimersByTimeAsync(50);
    r.gateway.stop();
    await vi.runAllTimersAsync();
    await done;
    expect(r.gateway.sent).toBeLessThan(r.gateway.fixture.steps.length);
  });
});
