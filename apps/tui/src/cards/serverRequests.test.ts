import { RpcChannel } from "@indus/client";
import { FakeServer } from "@indus/client/testing";
import { type ClarifyRequest, ERROR_CODES } from "@indus/protocol";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { overlayStore } from "../state/overlayStore.ts";
import { uiStore } from "../state/uiStore.ts";
import type { ApprovalPayload, ClarifyPayload, MaskedPayload } from "./payloads.ts";
import { createServerRequestCards, WITHDRAWN_LINE } from "./serverRequests.ts";

const tick = (ms = 0): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

function setup(options: { bell?: boolean } = {}) {
  const server = new FakeServer();
  const channel = new RpcChannel();
  channel.attach(server.connect());
  const ringBell = vi.fn();
  const lines: string[] = [];
  const cards = createServerRequestCards({
    rpc: { call: (method, params) => channel.call(method, params as object) },
    ringBell,
    bellOnPrompt: () => options.bell ?? true,
    systemLine: (text) => lines.push(text),
  });
  channel.setHandlers(cards.handlers);
  return { server, channel, ringBell, lines, cards };
}

const approval = (id: string) =>
  ({
    request_id: id,
    session_id: "s-1",
    kind: "approval",
    command: "rm -rf ./build",
    description: "deletes a folder",
  }) as const;

const card = () => overlayStore.getState().card;

function payload<T>(): T {
  const open = card();
  if (open === null) throw new Error("no card is open");
  return open.payload as T;
}

beforeEach(() => {
  overlayStore.setState({ card: null });
  uiStore.getState().patch({ statusText: "" });
});

describe("server requests become cards", () => {
  it("opens an approval card, sets the status, rings once and answers with the choice", async () => {
    const { server, ringBell } = setup();
    const answer = server.ask(approval("r-1"));
    await tick();
    expect(card()?.kind).toBe("approval");
    expect(card()?.requestId).toBe("r-1");
    expect(card()?.ownsInput).toBe(true);
    expect(uiStore.getState().statusText).toBe("approval needed");
    expect(ringBell).toHaveBeenCalledTimes(1);
    payload<ApprovalPayload>().choose("session");
    expect(await answer).toEqual({ kind: "approval", choice: "session" });
    expect(card()).toBeNull();
  });

  it("a replayed request rings no bell", async () => {
    const { channel, ringBell } = setup();
    channel.redeliver([approval("r-2")]);
    await tick();
    expect(card()?.requestId).toBe("r-2");
    expect(ringBell).not.toHaveBeenCalled();
  });

  it("rings no bell when bell_on_prompt is off", async () => {
    const { server, ringBell } = setup({ bell: false });
    void server.ask(approval("r-3"));
    await tick();
    expect(card()?.requestId).toBe("r-3");
    expect(ringBell).not.toHaveBeenCalled();
  });

  it("request.cancel closes the card and nothing is answered", async () => {
    const { server } = setup();
    void server.ask(approval("r-4"));
    await tick();
    expect(card()?.requestId).toBe("r-4");
    server.cancel("r-4", "timed_out");
    await tick();
    expect(card()).toBeNull();
    await tick();
    expect(server.calls("server_request.answer")).toHaveLength(0);
  });

  it("answers a desktop-only request kind with -32601", async () => {
    const { server } = setup();
    const frame = await server.request("preview.open", { url: "file:///tmp/x.html" });
    expect(frame.error).toMatchObject({ code: ERROR_CODES.METHOD_NOT_FOUND.code });
    expect(ERROR_CODES.METHOD_NOT_FOUND.code).toBe(-32601);
    expect(card()).toBeNull();
  });

  it("shows a second request only after the first is answered", async () => {
    const { server } = setup();
    const first = server.ask(approval("r-5"));
    const second = server.ask(approval("r-6"));
    await tick();
    expect(card()?.requestId).toBe("r-5");
    payload<ApprovalPayload>().choose("deny");
    await first;
    expect(card()?.requestId).toBe("r-6");
    payload<ApprovalPayload>().choose("once");
    expect(await second).toEqual({ kind: "approval", choice: "once" });
  });

  it("settleAll answers every open card with its refusal", async () => {
    const { server, cards } = setup();
    const first = server.ask(approval("r-7"));
    const sudo = server.ask({ request_id: "r-8", session_id: "s-1", kind: "sudo", reason: "apt" });
    await tick();
    expect(cards.openCount).toBe(2);
    cards.settleAll();
    expect(await first).toEqual({ kind: "approval", choice: "deny" });
    expect(await sudo).toEqual({ kind: "sudo", password: null });
    expect(card()).toBeNull();
  });
});

describe("clarify, sudo and secret requests", () => {
  const clarify: ClarifyRequest = {
    request_id: "c-1",
    session_id: "s-1",
    kind: "clarify",
    questions: [
      { id: "q1", text: "Which colour?", choices: ["red", "green"] },
      { id: "q2", text: "Any notes?" },
    ],
  };

  it("locks each answer through clarify.lock and the last one resolves", async () => {
    const { server, lines } = setup();
    const locks: unknown[] = [];
    server.handle("clarify.lock", (params) => {
      locks.push(params);
      return { remaining: params.question_id === "q1" ? ["q2"] : [] };
    });
    void server.ask(clarify);
    await tick();
    const clarifyCard = payload<ClarifyPayload>();
    expect(clarifyCard.questions.map((q) => q.id)).toEqual(["q1", "q2"]);
    expect(await clarifyCard.lock("q1", ["green"])).toEqual({ done: false });
    expect(card()?.kind).toBe("clarify");
    expect(await clarifyCard.lock("q2", [])).toEqual({ done: true });
    expect(card()).toBeNull();
    expect(locks).toEqual([
      { session_id: "s-1", request_id: "c-1", question_id: "q1", answer: ["green"] },
      { session_id: "s-1", request_id: "c-1", question_id: "q2", answer: [] },
    ]);
    expect(lines).toEqual([
      "Questions from the agent (answered):\n  Which colour? → green\n  Any notes? → skipped",
    ]);
  });

  it("a withdrawn clarify writes its questions with the locked answers as timed out", async () => {
    const { server, lines } = setup();
    server.handle("clarify.lock", () => ({ remaining: ["q2"] }));
    void server.ask(clarify);
    await tick();
    await payload<ClarifyPayload>().lock("q1", ["red"]);
    server.cancel("c-1", "timed_out");
    await tick();
    expect(card()).toBeNull();
    expect(lines).toHaveLength(1);
    expect(lines[0]).toContain("timed out");
    expect(lines[0]).toContain("Which colour? → red");
    expect(lines[0]).toContain("Any notes? → no answer");
  });

  it("flushOpenClarify records the open card when the clarify tool completed", async () => {
    const { server, lines, cards } = setup();
    void server.ask(clarify);
    await tick();
    cards.flushOpenClarify();
    expect(card()).toBeNull();
    expect(lines[0]).toContain("timed out");
  });

  it("Esc on clarify keeps locked answers and skips the rest", async () => {
    const { server, lines } = setup();
    const answer = server.ask(clarify);
    await tick();
    payload<ClarifyPayload>().cancel({ q1: ["red"] });
    expect(await answer).toEqual({ kind: "clarify", answers: { q1: ["red"], q2: [] } });
    expect(lines[0]).toContain("(cancelled)");
  });

  it("a withdrawn password card says the agent stopped waiting", async () => {
    const { server, lines } = setup();
    void server.ask({ request_id: "p-9", session_id: "s-1", kind: "sudo", reason: "install" });
    await tick();
    server.cancel("p-9", "timed_out");
    await tick();
    expect(card()).toBeNull();
    expect(lines).toEqual([WITHDRAWN_LINE.sudo]);
  });

  it("a clarify without usable questions is answered at once", async () => {
    const { server } = setup();
    const answer = await server.ask({
      ...clarify,
      request_id: "c-2",
      questions: [{ id: "q", text: "  " }],
    });
    expect(answer).toEqual({ kind: "clarify", answers: { q: [] } });
    expect(card()).toBeNull();
  });

  it("sudo and secret answer with the typed value or null", async () => {
    const { server } = setup();
    const sudo = server.ask({
      request_id: "p-1",
      session_id: "s-1",
      kind: "sudo",
      reason: "install",
    });
    await tick();
    expect(card()?.kind).toBe("sudo");
    expect(uiStore.getState().statusText).toBe("password needed");
    const sudoPayload = payload<MaskedPayload>();
    sudoPayload.submit(null);
    expect(await sudo).toEqual({ kind: "sudo", password: null });

    const secret = server.ask({
      request_id: "k-1",
      session_id: "s-1",
      kind: "secret",
      name: "WEATHER_KEY",
      prompt: "Key for the weather skill",
    });
    await tick();
    expect(card()?.kind).toBe("secret");
    const secretCard = payload<MaskedPayload>();
    expect(secretCard.title).toBe("Key for the weather skill");
    expect(secretCard.detail).toContain("WEATHER_KEY");
    secretCard.submit(["wk", "123"].join("-"));
    expect(await secret).toEqual({ kind: "secret", value: "wk-123" });
  });
});
