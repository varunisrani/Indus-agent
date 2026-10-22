import type { ClientEvent } from "@indus/client";
import type { HistoryMessage } from "@indus/protocol";
import { describe, expect, it } from "vitest";
import type { SessionEvent } from "../gateway/connectionStore.ts";
import { ChatActions, type Request } from "./busyActions.ts";
import { type LocalRows, sendShown } from "./localEcho.ts";
import {
  addLocalUser,
  applyEvent,
  applyHistory,
  assistantText,
  bindLocalUser,
  dropLocalUser,
  emptyTranscript,
  type SessionTranscript,
} from "./messageStream.ts";
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

// W48-c: the submit answer names the input by its admission id; `turn.started` names the turn by
// the daemon's own id. The row must end up bound to the turn's own id whichever comes first.
describe("the optimistic row follows the turn's own id", () => {
  const started = (turnId: string, generation: number) =>
    ({ type: "turn.started", session_id: "s1", turn_id: turnId, generation }) as SessionEvent;

  it("binds again to the turn's id when turn.started arrives after the answer", async () => {
    const { log, port } = rows();
    const run = actions(() => ({ turn_id: "adm-1", generation: 3, status: "started" }));
    expect(await sendShown(run, port, "hi", "hi")).toBe("started");
    expect(log).toEqual(["add s1 hi", "bind s1 l1 adm-1"]);
    run.handleEvent(started("turn-1", 3));
    expect(log).toEqual(["add s1 hi", "bind s1 l1 adm-1", "bind s1 l1 turn-1"]);
    // a later turn never moves the row
    run.handleEvent(started("turn-2", 4));
    expect(log).toHaveLength(3);
  });

  it("binds once to the turn's id when turn.started arrived first", async () => {
    const { log, port } = rows();
    let run: ChatActions | null = null;
    run = actions(() => {
      run?.handleEvent(started("turn-1", 3));
      return { turn_id: "adm-1", generation: 3, status: "started" };
    });
    expect(await sendShown(run, port, "hi", "hi")).toBe("started");
    expect(log).toEqual(["add s1 hi", "bind s1 l1 turn-1"]);
    run.handleEvent(started("turn-2", 4));
    expect(log).toHaveLength(2);
  });

  it("stops waiting when the turn ends unnamed", async () => {
    const { log, port } = rows();
    const run = actions(() => ({ turn_id: "adm-1", generation: 3, status: "started" }));
    await sendShown(run, port, "hi", "hi");
    run.handleEvent({ type: "session.info", session_id: "s1", running: false } as SessionEvent);
    run.handleEvent(started("turn-9", 3));
    expect(log).toEqual(["add s1 hi", "bind s1 l1 adm-1"]);
  });

  it("a history reload mid-turn keeps the row and the reply in one group", async () => {
    let t: SessionTranscript = emptyTranscript("s1");
    const port: LocalRows = {
      addLocalUser: (_sessionId, text) => {
        t = addLocalUser(t, "l1", text);
        return "l1";
      },
      bindLocalUser: (_sessionId, localId, turnId) => {
        t = bindLocalUser(t, localId, turnId);
      },
      dropLocalUser: (_sessionId, localId) => {
        t = dropLocalUser(t, localId);
      },
    };
    const run = actions(() => ({ turn_id: "adm-1", generation: 3, status: "started" }));
    const feed = (event: Record<string, unknown>) => {
      const full = { session_id: "s1", ...event };
      run.handleEvent(full as SessionEvent);
      t = applyEvent(t, full as ClientEvent);
    };
    await sendShown(run, port, "Write the notes", "Write the notes");
    feed({ type: "turn.started", seq: 1, turn_id: "turn-1", generation: 3 });
    feed({ type: "message.delta", seq: 2, turn_id: "turn-1", text: "Working on " });
    const stored: HistoryMessage[] = [
      { role: "user", text: "Write the notes", row_id: 1 } as HistoryMessage,
      { role: "assistant", text: "Working on ", row_id: 2 } as HistoryMessage,
    ];
    t = applyHistory(t, stored, 1);
    feed({ type: "message.delta", seq: 3, turn_id: "turn-1", text: "it." });
    const visible = t.items.map((item) =>
      item.kind === "user"
        ? `user ${item.text}`
        : item.kind === "assistant"
          ? `assistant ${assistantText(item)}`
          : item.kind,
    );
    expect(visible).toEqual(["user Write the notes", "assistant Working on it."]);
  });
});
