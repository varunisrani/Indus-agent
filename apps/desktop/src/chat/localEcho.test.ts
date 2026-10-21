import { describe, expect, it } from "vitest";
import { ChatActions, type Request } from "./busyActions.ts";
import { type LocalRows, sendShown } from "./localEcho.ts";
import { rpcError } from "./testFakes.ts";

function rows() {
  const log: string[] = [];
  const port: LocalRows = {
    addLocalUser: (sessionId, text) => {
      log.push(`add ${sessionId} ${text}`);
      return "l1";
    },
    bindLocalUser: (sessionId, localId, turnId) =>
      log.push(`bind ${sessionId} ${localId} ${turnId}`),
    dropLocalUser: (sessionId, localId) => log.push(`drop ${sessionId} ${localId}`),
  };
  return { log, port };
}

function actions(answer: () => unknown): ChatActions {
  const request = (async () => answer()) as unknown as Request;
  return new ChatActions({ sessionId: "s1", request, journal: null });
}

describe("the person's own message on screen", () => {
  it("shows the row at once and binds it to the turn that started", async () => {
    const { log, port } = rows();
    const run = actions(() => ({ turn_id: "t1", generation: 1, status: "started" }));
    expect(await sendShown(run, port, "hi @file", "hi")).toBe("started");
    expect(log).toEqual(["add s1 hi", "bind s1 l1 t1"]);
  });

  it("removes the row when the input was queued or refused", async () => {
    const queued = rows();
    const run = actions(() => ({ turn_id: "a1", status: "queued" }));
    expect(await sendShown(run, queued.port, "later", "later")).toBe("queued");
    expect(queued.log).toEqual(["add s1 later", "drop s1 l1"]);

    const refused = rows();
    const failing = actions(() => {
      throw rpcError("SESSION_NOT_FOUND");
    });
    expect(await sendShown(failing, refused.port, "gone", "gone")).toBe("rejected");
    expect(refused.log).toEqual(["add s1 gone", "drop s1 l1"]);
  });

  it("an empty message or no transcript sends as before", async () => {
    const { log, port } = rows();
    expect(
      await sendShown(
        actions(() => ({})),
        port,
        "  ",
        "  ",
      ),
    ).toBe("empty");
    expect(log).toEqual([]);
  });
});
