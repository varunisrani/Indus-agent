import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { ApprovalRequest, ServerRequestAnswer } from "@indus/protocol";
import { describe, expect, it } from "vitest";
import { harness, settle } from "./testSupport.ts";
import { RECONNECT_CAP_MS } from "./timing.ts";

function approval(id: string, sessionId = "s1"): ApprovalRequest {
  return {
    request_id: id,
    session_id: sessionId,
    kind: "approval",
    command: "rm -rf build",
    description: "deletes a folder",
  };
}

function offer(request: ApprovalRequest, replayed: boolean) {
  return {
    jsonrpc: "2.0",
    method: "server_request",
    params: { request, replayed, generation: 1 },
  };
}

async function open(capabilities: string[] = []) {
  const h = harness();
  h.server.handle("server_request.answer", () => ({ accepted: true }));
  h.server.handle("server_request.decline", () => ({ accepted: true }));
  h.connection.start();
  await settle();
  h.ready(capabilities);
  await settle();
  await h.connection.watchSession("s1");
  return h;
}

interface RequestFixture {
  frames: Array<
    | { op: "offer"; request: ApprovalRequest; replayed: boolean; generation: number }
    | { op: "reconnect" }
    | { op: "answer"; request_id: string; answer: ServerRequestAnswer }
  >;
  expect: { cards_max: number; answers_on_wire: number; answer: ServerRequestAnswer };
}

const FIXTURE = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../testing/fixtures/streams/replayed-requests.json",
);

describe("ServerRequestStore", () => {
  it("replays the replayed-requests fixture: one card, one answer on the wire", async () => {
    const fixture = JSON.parse(fs.readFileSync(FIXTURE, "utf8")) as RequestFixture;
    const h = await open();
    const requests = h.connection.serverRequests;
    let most = 0;
    for (const frame of fixture.frames) {
      if (frame.op === "offer") {
        h.server.sendRaw({
          jsonrpc: "2.0",
          method: "server_request",
          params: {
            request: frame.request,
            replayed: frame.replayed,
            generation: frame.generation,
          },
        });
      } else if (frame.op === "reconnect") {
        h.server.die("cut");
        await settle();
        await h.advance(RECONNECT_CAP_MS);
        h.ready();
      } else {
        requests.answer(frame.request_id, frame.answer);
      }
      await settle();
      most = Math.max(most, requests.list().length);
    }
    expect(most).toBe(fixture.expect.cards_max);
    const answers = h.server.calls("server_request.answer");
    expect(answers).toHaveLength(fixture.expect.answers_on_wire);
    expect(answers[0]?.params).toMatchObject({ answer: fixture.expect.answer, generation: 1 });
  });

  it("a re-offered approval stays one card and is answered once on the wire", async () => {
    const h = await open();
    const requests = h.connection.serverRequests;
    h.server.sendRaw(offer(approval("srq-1"), false));
    await settle();
    expect(requests.list("s1").map((card) => card.id)).toEqual(["srq-1"]);
    expect(requests.list("s1")[0]?.replayed).toBe(false);

    h.server.die("cut");
    await settle();
    await h.advance(RECONNECT_CAP_MS);
    h.ready();
    await settle();
    // The daemon offers it again after the reconnect, marked replayed.
    h.server.sendRaw(offer(approval("srq-1"), true));
    await settle();
    expect(requests.list()).toHaveLength(1);

    expect(requests.answer("srq-1", { kind: "approval", choice: "once" })).toBe(true);
    expect(requests.answer("srq-1", { kind: "approval", choice: "deny" })).toBe(false);
    await settle();
    // A late third delivery after the answer shows nothing.
    h.server.sendRaw(offer(approval("srq-1"), true));
    await settle();
    expect(requests.list()).toHaveLength(0);
    const answers = h.server.calls("server_request.answer");
    expect(answers).toHaveLength(1);
    expect(answers[0]?.params).toMatchObject({
      request_id: "srq-1",
      answer: { kind: "approval", choice: "once" },
    });
    expect(requests.isAnswered("srq-1")).toBe(true);
  });

  it("a card first seen through a replay says so", async () => {
    const h = await open();
    h.server.sendRaw(offer(approval("srq-2"), true));
    await settle();
    expect(h.connection.serverRequests.get("srq-2")?.replayed).toBe(true);
  });

  it("withdrawn requests close their card", async () => {
    const h = await open();
    const requests = h.connection.serverRequests;
    h.server.sendRaw(offer(approval("srq-3"), false));
    h.server.sendRaw(offer(approval("srq-4"), false));
    await settle();
    expect(requests.list()).toHaveLength(2);
    h.emit("s1", {
      type: "request.settled",
      request_id: "srq-3",
      kind: "approval",
      outcome: "answered",
    });
    await settle();
    expect(requests.list().map((card) => card.id)).toEqual(["srq-4"]);
    expect(requests.answer("srq-3", { kind: "approval", choice: "once" })).toBe(false);
  });

  it("sends a decline frame only to a daemon that counts declines", async () => {
    const counting = await open(["request.decline"]);
    counting.server.sendRaw(offer(approval("srq-5"), false));
    await settle();
    expect(counting.connection.serverRequests.decline("srq-5")).toBe(true);
    expect(counting.connection.serverRequests.decline("srq-5")).toBe(false);
    await settle();
    expect(counting.server.calls("server_request.decline")).toHaveLength(1);
    expect(counting.server.calls("server_request.answer")).toHaveLength(0);

    const plain = await open([]);
    plain.server.sendRaw(offer(approval("srq-6"), false));
    await settle();
    expect(plain.connection.serverRequests.decline("srq-6")).toBe(true);
    await settle();
    expect(plain.server.calls("server_request.decline")).toHaveLength(0);
    // The refusal every daemon understands: an error frame under the request's id.
    const refusals = plain.server.received.filter((frame) => frame.id === "srq-6");
    expect(refusals).toHaveLength(1);
    expect(refusals[0]).toHaveProperty("error");
    expect(plain.server.calls("server_request.answer")).toHaveLength(0);
  });

  it("refuses an answer of the wrong kind and keeps the card", async () => {
    const h = await open();
    h.server.sendRaw(offer(approval("srq-7"), false));
    await settle();
    expect(h.connection.serverRequests.answer("srq-7", { kind: "sudo", password: null })).toBe(
      false,
    );
    expect(h.connection.serverRequests.list()).toHaveLength(1);
  });

  it("keeps the list stable between changes (safe for useSyncExternalStore)", async () => {
    const h = await open();
    const requests = h.connection.serverRequests;
    h.server.sendRaw(offer(approval("srq-8"), false));
    await settle();
    expect(requests.list("s1")).toBe(requests.list("s1"));
    expect(requests.list()).toBe(requests.list());
  });
});
