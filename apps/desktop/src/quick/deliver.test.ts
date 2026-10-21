import { describe, expect, it } from "vitest";
import type { ChatActions, SendOutcome } from "../chat/busyActions.ts";
import type { LocalRows } from "../chat/localEcho.ts";
import type { ChatConnection } from "../gateway/connectionStore.ts";
import { type DeliverDeps, deliverQuickText } from "./deliver.ts";

function actions(sessionId: string, outcome: SendOutcome, sent: string[]): ChatActions {
  return {
    sessionId,
    send: async (text: string) => {
      sent.push(`${sessionId}:${text}`);
      return outcome;
    },
    snapshot: () => ({ turnId: outcome === "started" ? "t1" : null }),
  } as unknown as ChatActions;
}

function deps(outcome: SendOutcome, created: string | Error = "s-new") {
  const sent: string[] = [];
  const calls: Array<[string, unknown]> = [];
  const rows: string[] = [];
  const opened: string[] = [];
  const connection = {
    request: async (method: string, params: unknown) => {
      calls.push([method, params]);
      if (created instanceof Error) throw created;
      return { session_id: created };
    },
  } as unknown as ChatConnection;
  const localRows: LocalRows = {
    addLocalUser: (sessionId, text) => {
      rows.push(`add ${sessionId} ${text}`);
      return "local-1";
    },
    bindLocalUser: (sessionId) => rows.push(`bind ${sessionId}`),
    dropLocalUser: (sessionId) => rows.push(`drop ${sessionId}`),
  };
  const d: DeliverDeps = {
    connection,
    rows: localRows,
    actionsFor: (id) => actions(id, outcome, sent),
    newKey: () => "key-1",
    onSessionCreated: (id) => opened.push(id),
  };
  return { d, sent, calls, rows, opened };
}

describe("quick-entry delivery", () => {
  it("submits into the reported session through the composer's send path (optimistic row)", async () => {
    const { d, sent, calls, rows } = deps("started");
    const result = await deliverQuickText(d, {
      requestId: "r1",
      text: " hi ",
      sessionId: "s-last",
    });
    expect(result).toEqual({ ok: true, sessionId: "s-last" });
    expect(sent).toEqual(["s-last:hi"]);
    expect(calls).toEqual([]);
    expect(rows).toEqual(["add s-last hi", "bind s-last"]);
  });

  it("no session → a new chat first, which the window then shows", async () => {
    const { d, sent, calls, opened } = deps("queued");
    const result = await deliverQuickText(d, { requestId: "r1", text: "go", sessionId: null });
    expect(result).toEqual({ ok: true, sessionId: "s-new" });
    expect(calls).toEqual([["session.create", { idempotency_key: "key-1" }]]);
    expect(sent).toEqual(["s-new:go"]);
    expect(opened).toEqual(["s-new"]);
  });

  it("refused, blocked or unconfirmed sends answer not ok (the quick window keeps the text)", async () => {
    for (const outcome of ["rejected", "blocked", "uncertain"] as const) {
      const { d } = deps(outcome);
      const result = await deliverQuickText(d, { requestId: "r", text: "x", sessionId: "s" });
      expect(result.ok).toBe(false);
    }
    const failing = deps("started", new Error("daemon unreachable"));
    expect(
      await deliverQuickText(failing.d, { requestId: "r", text: "x", sessionId: null }),
    ).toEqual({ ok: false, sessionId: null, error: "daemon unreachable" });
    const empty = deps("started");
    expect(
      (await deliverQuickText(empty.d, { requestId: "r", text: "  ", sessionId: "s" })).ok,
    ).toBe(false);
    expect(empty.sent).toEqual([]);
  });
});
