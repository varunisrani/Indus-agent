import { describe, expect, it } from "vitest";
import type { ErrorItem, TranscriptItem } from "../chat/messageStream.ts";
import { failedPrompt } from "./chatSlots.tsx";

const user = (key: string, text: string, turnId: string | null): TranscriptItem => ({
  kind: "user",
  key,
  text,
  turnId,
  localId: null,
  persisted: true,
  rowId: null,
});

const failure = (key: string, turnId: string): ErrorItem => ({
  kind: "error",
  key,
  turnId,
  layer: "provider",
  message: "overloaded",
  reason: "error",
  retryable: true,
  resetsAt: null,
});

describe("Retry on a failed turn", () => {
  it("sends the failed turn's own message", () => {
    const error = failure("e2", "t2");
    const items = [
      user("u1", "first", "t1"),
      user("u2", "second", "t2"),
      error,
      user("u3", "x", null),
    ];
    expect(failedPrompt(items, error)).toBe("second");
  });

  it("falls back to the last message before the card, and to nothing", () => {
    const error = failure("e1", "t9");
    expect(failedPrompt([user("u1", "only", null), error], error)).toBe("only");
    expect(failedPrompt([error], error)).toBeNull();
  });
});
