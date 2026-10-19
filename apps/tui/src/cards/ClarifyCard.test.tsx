import { describe, expect, it, vi } from "vitest";
import { ClarifyCard } from "./ClarifyCard.tsx";
import { clarifyStep, clarifySummary, initialClarify, lockSucceeded } from "./clarifyState.ts";
import { KEY, mount } from "./inkHarness.ts";
import type { ClarifyQuestionView, LockOutcome } from "./payloads.ts";

const QUESTIONS: ClarifyQuestionView[] = [
  { id: "size", text: "Which size?", choices: ["small", "medium", "large"], multi: false },
  { id: "extras", text: "Which extras?", choices: ["cheese", "olives", "basil"], multi: true },
];

/** A lock handler that records calls and reports done after every question was locked once. */
function recorder(questions = QUESTIONS) {
  const calls: { id: string; answer: string[] }[] = [];
  const onLock = vi.fn(async (id: string, answer: string[]): Promise<LockOutcome> => {
    calls.push({ id, answer });
    const locked = new Set(calls.map((c) => c.id));
    return { done: questions.every((q) => locked.has(q.id)) };
  });
  return { calls, onLock };
}

describe("clarify card on screen", () => {
  it("locks in order: a digit for the first, toggles then Enter for the multi-select", async () => {
    const { calls, onLock } = recorder();
    const app = await mount(
      <ClarifyCard questions={QUESTIONS} onLock={onLock} onCancel={() => {}} />,
    );
    expect(app.lastFrame()).toContain("Which size?");
    await app.press("2");
    expect(app.lastFrame()).toContain("› Which extras?");
    await app.press(KEY.space, KEY.down, KEY.down, KEY.space, KEY.enter);
    expect(calls).toEqual([
      { id: "size", answer: ["medium"] },
      { id: "extras", answer: ["cheese", "basil"] },
    ]);
    app.unmount();
  });

  it("Other switches to free text", async () => {
    const { calls, onLock } = recorder();
    const app = await mount(
      <ClarifyCard questions={QUESTIONS} onLock={onLock} onCancel={() => {}} />,
    );
    await app.press(KEY.down, KEY.down, KEY.down, KEY.enter, "t", "i", "n", "y", KEY.enter);
    expect(calls[0]).toEqual({ id: "size", answer: ["tiny"] });
    app.unmount();
  });

  it("empty free text skips the question", async () => {
    const { calls, onLock } = recorder();
    const app = await mount(
      <ClarifyCard questions={QUESTIONS} onLock={onLock} onCancel={() => {}} />,
    );
    await app.press(KEY.down, KEY.down, KEY.down, KEY.enter, KEY.enter);
    expect(calls[0]).toEqual({ id: "size", answer: [] });
    app.unmount();
  });

  it("Esc gives up and hands over what is locked", async () => {
    const { onLock } = recorder();
    const onCancel = vi.fn();
    const app = await mount(
      <ClarifyCard questions={QUESTIONS} onLock={onLock} onCancel={onCancel} />,
    );
    await app.press("1", KEY.escape);
    expect(onCancel).toHaveBeenCalledWith({ size: ["small"] });
    app.unmount();
  });

  it("a failed lock stays open and says so", async () => {
    const onLock = vi.fn(async (): Promise<LockOutcome> => ({ error: "bridge went away" }));
    const app = await mount(
      <ClarifyCard questions={QUESTIONS} onLock={onLock} onCancel={() => {}} />,
    );
    await app.press("3");
    expect(app.lastFrame()).toContain("bridge went away");
    expect(app.lastFrame()).toContain("› Which size?");
    app.unmount();
  });
});

describe("clarify state machine", () => {
  const start = () => initialClarify(QUESTIONS);
  const press = (state: ReturnType<typeof start>, input: string, key: object = {}) =>
    clarifyStep(state, QUESTIONS, input, key);

  it("Tab and Shift+Tab move between questions and wrap", () => {
    let state = press(start(), "", { tab: true }).state;
    expect(state.active).toBe(1);
    state = press(state, "", { tab: true }).state;
    expect(state.active).toBe(0);
    state = press(state, "", { tab: true, shift: true }).state;
    expect(state.active).toBe(1);
  });

  it("returning to a locked question shows its answer, and it can be locked again", () => {
    let state = lockSucceeded(start(), QUESTIONS, "size", ["large"]);
    expect(state.active).toBe(1);
    state = press(state, "", { tab: true }).state;
    expect(state.cursor).toBe(2);
    const again = press(state, "1");
    expect(again.effect).toEqual({ type: "lock", questionId: "size", answer: ["small"] });
  });

  it("a typed answer comes back on the Other row", () => {
    let state = lockSucceeded(start(), QUESTIONS, "size", ["huge"]);
    state = press(state, "", { tab: true }).state;
    expect(state.cursor).toBe(3);
    expect(state.draft).toBe("huge");
  });

  it("multi-select Enter with nothing toggled takes the row under the cursor", () => {
    const state = { ...start(), active: 1, typing: false };
    expect(press({ ...state, cursor: 2 }, "", { return: true }).effect).toEqual({
      type: "lock",
      questionId: "extras",
      answer: ["basil"],
    });
  });

  it("multi-select digits toggle instead of locking", () => {
    const state = { ...start(), active: 1 };
    const result = press(state, "2");
    expect(result.effect).toBeNull();
    expect(result.state.picked).toEqual(["olives"]);
  });

  it("free-text questions type at once; Esc there cancels", () => {
    const questions: ClarifyQuestionView[] = [
      { id: "why", text: "Why?", choices: [], multi: false },
    ];
    let state = initialClarify(questions);
    expect(state.typing).toBe(true);
    state = clarifyStep(state, questions, "1", {}).state;
    expect(state.draft).toBe("1");
    expect(clarifyStep(state, questions, "", { escape: true }).effect).toEqual({ type: "cancel" });
  });

  it("keys wait while a lock is in flight, but cancel still works", () => {
    const waiting = { ...start(), waiting: true };
    expect(press(waiting, "2").effect).toBeNull();
    expect(press(waiting, "c", { ctrl: true }).effect).toEqual({ type: "cancel" });
  });

  it("the timed-out summary keeps locked answers", () => {
    const text = clarifySummary(QUESTIONS, { size: ["small"] }, "timed out");
    expect(text).toContain("(timed out)");
    expect(text).toContain("Which size? → small");
    expect(text).toContain("Which extras? → no answer");
  });
});
