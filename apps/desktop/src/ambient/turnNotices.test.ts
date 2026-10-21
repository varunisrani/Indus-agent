import { describe, expect, it } from "vitest";
import { translate } from "../i18n/index.tsx";
import { CHIME_NOTES, playChime } from "./sound.ts";
import { requestNotice, tagSession, turnNotice, WorkTracker } from "./turnNotices.ts";

describe("work tracker", () => {
  it("one item per running session; ends only with its own turn", () => {
    const work = new WorkTracker();
    expect(work.onEvent({ type: "turn.started", session_id: "s1", turn_id: "t1" })).toBe(true);
    expect(work.onEvent({ type: "turn.started", session_id: "s1", turn_id: "t2" })).toBe(false);
    expect(work.onEvent({ type: "turn.done", session_id: "s1", turn_id: "t1" })).toBe(false);
    expect(work.items()).toEqual([{ id: "s1", label: "" }]);
    expect(work.onEvent({ type: "turn.done", session_id: "s1", turn_id: "t2" })).toBe(true);
    expect(work.items()).toEqual([]);
    expect(work.onEvent({ type: "message.delta", session_id: "s1" })).toBe(false);
  });
});

describe("notices", () => {
  it("a finished turn notifies only while the window is not focused", () => {
    const done = { type: "turn.done", session_id: "s1", reason: "completed" };
    expect(turnNotice(done, true, translate)).toBeNull();
    expect(turnNotice(done, false, translate)).toMatchObject({
      title: "Reply ready",
      tag: "turn:s1",
      actions: [{ id: "open", label: "Open" }],
    });
    expect(turnNotice({ ...done, reason: "interrupted_by_user" }, false, translate)).toBeNull();
    expect(
      turnNotice(
        { ...done, reason: "provider_failed", failure: { reason: "rate limited" } },
        false,
        translate,
      ),
    ).toMatchObject({ title: "Indus stopped", body: "rate limited" });
  });

  it("a waiting question notifies with its kind", () => {
    expect(requestNotice("approval", "s2", false, translate)?.body).toBe(
      "A command is waiting for your approval.",
    );
    expect(requestNotice("clarify", "s2", false, translate)?.tag).toBe("ask:s2");
    expect(requestNotice("clarify", "s2", true, translate)).toBeNull();
  });

  it("tags lead back to their session", () => {
    expect(tagSession("turn:abc")).toBe("abc");
    expect(tagSession("ask:x:y")).toBe("x:y");
    expect(tagSession("other")).toBeNull();
    expect(tagSession(null)).toBeNull();
  });
});

describe("completion chime", () => {
  it("is synthesised (no sound file): two notes, quiet", () => {
    const made: string[] = [];
    const context = {
      currentTime: 0,
      destination: {},
      createOscillator: () => ({
        type: "",
        frequency: { setValueAtTime: (value: number) => made.push(`f${value}`) },
        connect: () => undefined,
        start: () => made.push("start"),
        stop: () => undefined,
      }),
      createGain: () => ({
        gain: {
          setValueAtTime: () => undefined,
          linearRampToValueAtTime: (value: number) => made.push(`peak${value}`),
          exponentialRampToValueAtTime: () => undefined,
        },
        connect: () => undefined,
      }),
    };
    expect(playChime(context)).toBe(true);
    expect(made.filter((m) => m === "start")).toHaveLength(CHIME_NOTES.length);
    expect(made.filter((m) => m.startsWith("peak")).every((m) => Number(m.slice(4)) <= 0.1)).toBe(
      true,
    );
    expect(playChime(null)).toBe(false);
  });
});
