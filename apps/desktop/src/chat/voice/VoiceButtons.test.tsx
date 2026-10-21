// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { DraftStore, draftOf } from "../composer/goneSession.ts";
import { FakeConnection } from "../testFakes.ts";
import { draftSink, voiceTurn } from "./VoiceButtons.tsx";

describe("voice wiring", () => {
  it("dictated words land in the pinned session's draft, not the one on screen", () => {
    const store = new DraftStore();
    const sink = draftSink(store);
    store.update((state) => ({ ...state, drafts: { "session-a": "Hello", "session-b": "" } }));
    // session B is on screen now; the capture was pinned to A
    sink({ sessionId: "session-a", profile: null }, "world");
    sink({ sessionId: null, profile: null }, "fresh words");
    expect(draftOf(store.snapshot(), "session-a")).toBe("Hello world");
    expect(draftOf(store.snapshot(), "session-b")).toBe("");
    expect(draftOf(store.snapshot(), "")).toBe("fresh words");
  });

  it("a spoken turn resolves with the reply of its own turn", async () => {
    const connection = new FakeConnection();
    connection.on("prompt.submit", () => {
      // another turn's reply arrives first, then ours
      connection.emit("session-a", {
        type: "message.complete",
        turn_id: "t0",
        text: "old",
        reason: "completed",
      });
      return { turn_id: "t1", status: "streaming" };
    });
    const reply = voiceTurn(connection, "session-a", "what time is it");
    await Promise.resolve();
    connection.emit("session-a", {
      type: "message.complete",
      turn_id: "t1",
      text: "noon",
      reason: "completed",
    });
    expect(await reply).toBe("noon");
    expect(connection.called("prompt.submit")[0]?.params).toEqual({
      session_id: "session-a",
      text: "what time is it",
    });
  });
});
