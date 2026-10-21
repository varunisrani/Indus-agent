import { GatewayRpcError, RpcTimeout } from "@indus/client";
import { ERROR_CODES } from "@indus/protocol";
import { describe, expect, it } from "vitest";
import {
  adoptGoneDraft,
  DraftStore,
  dismissRestore,
  draftOf,
  EMPTY_DRAFTS,
  FRESH,
  setDraft,
  undoRestore,
  verifySessionGone,
} from "./goneSession.ts";

describe("gone-session draft", () => {
  it("adopts the unsent text into an empty fresh draft and shows the strip", () => {
    const state = setDraft(EMPTY_DRAFTS, "s1", "half a thought");
    const next = adoptGoneDraft(state, "s1");
    expect(draftOf(next, FRESH)).toBe("half a thought");
    expect(draftOf(next, "s1")).toBe("");
    expect(next.restored).toEqual({
      fromSessionId: "s1",
      text: "half a thought",
      adopted: true,
      previousFresh: "",
    });
  });

  it("never overwrites a non-empty fresh draft", () => {
    let state = setDraft(EMPTY_DRAFTS, "s1", "old text");
    state = setDraft(state, FRESH, "new text");
    const next = adoptGoneDraft(state, "s1");
    expect(draftOf(next, FRESH)).toBe("new text");
    expect(next.restored).toMatchObject({ adopted: false, text: "old text" });
  });

  it("adopts once: a second gone signal changes nothing", () => {
    const once = adoptGoneDraft(setDraft(EMPTY_DRAFTS, "s1", "text"), "s1");
    const cleared = setDraft(dismissRestore(once), FRESH, "");
    const input = setDraft(cleared, "s1", "late write");
    const again = adoptGoneDraft(input, "s1");
    expect(again).toBe(input);
    expect(draftOf(again, FRESH)).toBe("");
    expect(again.restored).toBeNull();
  });

  it("an empty unsent text shows no strip", () => {
    const next = adoptGoneDraft(setDraft(EMPTY_DRAFTS, "s1", "  "), "s1");
    expect(next.restored).toBeNull();
    expect(draftOf(next, FRESH)).toBe("");
  });

  it("undo puts the fresh draft back", () => {
    const adopted = adoptGoneDraft(setDraft(EMPTY_DRAFTS, "s1", "text"), "s1");
    const undone = undoRestore(adopted);
    expect(draftOf(undone, FRESH)).toBe("");
    expect(undone.restored).toBeNull();
    expect(undoRestore(undone)).toBe(undone);
  });

  it("the store notifies only on a real change", () => {
    const store = new DraftStore();
    let calls = 0;
    store.subscribe(() => {
      calls += 1;
    });
    store.update((s) => setDraft(s, "s1", "a"));
    store.update((s) => setDraft(s, "s1", "a"));
    expect(calls).toBe(1);
  });

  it("only a confirmed miss counts as gone", async () => {
    const failing = (error: unknown) =>
      (async () => {
        throw error;
      }) as never;
    const notFound = new GatewayRpcError(ERROR_CODES.SESSION_NOT_FOUND.code, "no session");
    const refused = new GatewayRpcError(ERROR_CODES.SESSION_REFUSED.code, "deleted");
    const internal = new GatewayRpcError(ERROR_CODES.INTERNAL_ERROR.code, "boom");
    expect(await verifySessionGone(failing(notFound), "s1")).toBe(true);
    expect(await verifySessionGone(failing(refused), "s1")).toBe(true);
    expect(await verifySessionGone(failing(internal), "s1")).toBe(false);
    expect(await verifySessionGone(failing(new RpcTimeout("session.history", 1)), "s1")).toBe(
      false,
    );
    expect(await verifySessionGone((async () => ({ messages: [] })) as never, "s1")).toBe(false);
  });
});
