import { describe, expect, it } from "vitest";
import {
  applyCompletion,
  COMPLETION_DEBOUNCE_MS,
  type CompletionContext,
  CompletionController,
  completionOnEnter,
  isSlashCommandText,
  launchCwd,
  planCompletion,
  sessionCwd,
} from "./completion.ts";
import { Composer } from "./composerStore.ts";
import { FakeClock, fakeBridge, settle } from "./testSupport.ts";

const ctx: CompletionContext = { sessionId: "s1", sessionCwd: "/work/repo", launchCwd: "/home/u" };

describe("which request", () => {
  it("routes a leading /word to slash completion", () => {
    expect(planCompletion("/mo", ctx)).toEqual({
      method: "complete.slash",
      params: { text: "/mo", session_id: "s1" },
      replaceFrom: 1,
      skillsOnly: false,
    });
    expect(planCompletion("/", ctx)?.method).toBe("complete.slash");
    expect(planCompletion("/models", ctx)?.method).toBe("complete.slash");
  });

  it("gives /model nothing: the picker owns it", () => {
    expect(planCompletion("/model", ctx)).toBeNull();
    expect(planCompletion("/model ", ctx)).toBeNull();
    expect(planCompletion("/model gpt", ctx)).toBeNull();
  });

  it("asks slash completion for skills only for a /word inside prose", () => {
    expect(planCompletion("see /rev", ctx)).toEqual({
      method: "complete.slash",
      params: { text: "/rev", session_id: "s1" },
      replaceFrom: 5,
      skillsOnly: true,
    });
    expect(planCompletion("see /", ctx)).toMatchObject({ params: { text: "/" }, replaceFrom: 5 });
    expect(planCompletion("/help /cle", ctx)).toMatchObject({
      params: { text: "/cle" },
      replaceFrom: 7,
      skillsOnly: true,
    });
    expect(planCompletion("/model /x", ctx)).toBeNull();
  });

  it("asks path completion with the session folder, never this process's folder", () => {
    expect(planCompletion("open ~/pro", ctx)).toEqual({
      method: "complete.path",
      params: { word: "~/pro", cwd: "/work/repo", session_id: "s1" },
      replaceFrom: 5,
    });
    const noSessionCwd = { ...ctx, sessionCwd: null };
    expect(planCompletion("./sr", noSessionCwd)).toMatchObject({ params: { cwd: "/home/u" } });
    const nothing = { sessionId: null, sessionCwd: null, launchCwd: null };
    expect(planCompletion("./sr", nothing)).toEqual({
      method: "complete.path",
      params: { word: "./sr" },
      replaceFrom: 0,
    });
  });

  it.each([
    ["look at @", "@"],
    ["edit src/ma", "src/ma"],
    ["C:\\Us", "C:\\Us"],
    ["../up", "../up"],
    ["/usr/lo", "/usr/lo"],
  ])("treats %s as a path word", (input, word) => {
    expect(planCompletion(input, ctx)).toMatchObject({ method: "complete.path", params: { word } });
  });

  it("asks nothing for plain prose", () => {
    expect(planCompletion("hello there", ctx)).toBeNull();
    expect(planCompletion("", ctx)).toBeNull();
  });

  it("knows slash text from a path", () => {
    expect(isSlashCommandText("/help me")).toBe(true);
    expect(isSlashCommandText("/usr/bin")).toBe(false);
    expect(isSlashCommandText("x /help")).toBe(false);
  });

  it("finds the launch and session folders", () => {
    expect(launchCwd({ INDUS_LAUNCH_CWD: "/a", PWD: "/b" })).toBe("/a");
    expect(launchCwd({ PWD: "/b" })).toBe("/b");
    expect(launchCwd({})).toBeNull();
    expect(sessionCwd({ cwd: "/s" })).toBe("/s");
    expect(sessionCwd(null)).toBeNull();
  });
});

function rig(
  handler: (params: Record<string, unknown>) => unknown,
  context: CompletionContext = ctx,
) {
  const bridge = fakeBridge({ "complete.slash": handler, "complete.path": handler });
  const composer = new Composer();
  const clock = new FakeClock();
  let blocked = false;
  const controller = new CompletionController({
    rpc: bridge.rpc,
    composer,
    clock,
    context: () => context,
    blocked: () => blocked,
  });
  return {
    bridge,
    composer,
    clock,
    controller,
    block: (on: boolean) => {
      blocked = on;
    },
  };
}

describe("debounce and staleness", () => {
  it("asks once, 60 ms after the last change", async () => {
    const r = rig(() => ({ items: [{ text: "/help", display: "/help", meta: "show help" }] }));
    r.controller.update("/h");
    r.clock.advance(30);
    r.controller.update("/he");
    r.clock.advance(COMPLETION_DEBOUNCE_MS - 1);
    expect(r.bridge.calls("complete.slash")).toEqual([]);
    r.clock.advance(1);
    await settle();
    expect(r.bridge.calls("complete.slash")).toEqual([{ text: "/he", session_id: "s1" }]);
    expect(r.composer.completion).toEqual({
      items: [{ text: "/help", display: "/help", meta: "show help" }],
      index: 0,
      replaceFrom: 1,
    });
  });

  it("drops a reply for text that changed while it was on its way", async () => {
    let release: (value: unknown) => void = () => {};
    const r = rig(() => new Promise((resolve) => (release = resolve)));
    r.controller.update("/re");
    r.clock.advance(COMPLETION_DEBOUNCE_MS);
    await settle();
    r.controller.update("hello");
    release({ items: [{ text: "/retry", display: "/retry" }] });
    await settle();
    expect(r.composer.completion.items).toEqual([]);
  });

  it("keeps only skills for an inline reference, replacing just the token", async () => {
    const r = rig(() => ({
      items: [
        { text: "/review", display: "/review", meta: "skill: review a change" },
        { text: "/retry", display: "/retry", meta: "send the last prompt again" },
        { text: "/release-kit", display: "/release-kit", meta: "skill bundle" },
      ],
      replace_from: 1,
    }));
    r.controller.update("see /re");
    r.clock.advance(COMPLETION_DEBOUNCE_MS);
    await settle();
    expect(r.composer.completion.items.map((row) => row.text)).toEqual(["/review", "/release-kit"]);
    expect(r.composer.completion.replaceFrom).toBe(5);
    expect(applyCompletion("see /re", "/review", 5)).toBe("see /review");
  });

  it("shows an unavailable row when the bridge fails", async () => {
    const r = rig(() => {
      throw new Error("no skills index");
    });
    r.controller.update("/x");
    r.clock.advance(COMPLETION_DEBOUNCE_MS);
    await settle();
    expect(r.composer.completion.items).toEqual([
      {
        text: "",
        display: "completions unavailable",
        meta: expect.stringContaining("no skills index"),
        disabled: true,
      },
    ]);
  });

  it("clears and forgets while a card blocks input, then asks again", async () => {
    const r = rig(() => ({ items: [{ text: "/help", display: "/help" }] }));
    r.controller.update("/h");
    r.clock.advance(COMPLETION_DEBOUNCE_MS);
    await settle();
    expect(r.composer.completion.items).toHaveLength(1);
    r.block(true);
    r.controller.update("/h");
    expect(r.composer.completion.items).toEqual([]);
    r.block(false);
    r.controller.update("/h");
    r.clock.advance(COMPLETION_DEBOUNCE_MS);
    await settle();
    expect(r.bridge.calls("complete.slash")).toHaveLength(2);
  });

  it("clears the list and cancels a pending ask when the text stops calling for one", async () => {
    const r = rig(() => ({ items: [] }));
    r.controller.update("/h");
    r.controller.update("plain");
    r.clock.advance(COMPLETION_DEBOUNCE_MS * 2);
    await settle();
    expect(r.bridge.calls("complete.slash")).toEqual([]);
    expect(r.clock.pending).toBe(0);
  });
});

describe("apply on enter", () => {
  it("does not double the slash", () => {
    expect(applyCompletion("/he", "/help", 1)).toBe("/help");
    expect(applyCompletion("/he", "help", 1)).toBe("/help");
    expect(applyCompletion("open ./s", "./src/", 5)).toBe("open ./src/");
  });

  it("applies only when that changes more than trailing space", () => {
    expect(completionOnEnter("/he", { text: "/help", display: "/help" }, 1)).toBe("/help");
    expect(completionOnEnter("/help", { text: "/help ", display: "/help" }, 1)).toBeNull();
    expect(
      completionOnEnter("/x", { text: "", display: "completions unavailable", disabled: true }, 1),
    ).toBeNull();
    expect(
      completionOnEnter("/new", { text: "/news", display: "/news" }, 1, (name) => name === "new"),
    ).toBeNull();
    expect(completionOnEnter("/he", undefined, 1)).toBeNull();
  });
});
