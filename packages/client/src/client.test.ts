import type { ApprovalRequest } from "@indus/protocol";
import { describe, expect, it } from "vitest";
import { IndusClient } from "./client.ts";
import { GatewayRpcError, TransportReplaced } from "./errors.ts";
import type { ClientEvent } from "./localEvents.ts";
import { FakeRpcError, FakeServer } from "./testing/fakeServer.ts";

const settle = async (rounds = 6) => {
  for (let i = 0; i < rounds; i += 1) await Promise.resolve();
};

const caught = <E extends Error = Error>(promise: Promise<unknown>): Promise<E> =>
  promise.then(
    () => {
      throw new Error("expected the call to fail");
    },
    (error: unknown) => error as E,
  );

const card = (id: string): ApprovalRequest => ({
  kind: "approval",
  request_id: id,
  session_id: "s-fake",
  command: "git push --force",
  description: "force push",
  choices: ["once", "session", "deny"],
});

describe("IndusClient with the fake server", () => {
  it("makes typed calls and surfaces error frames as GatewayRpcError", async () => {
    const server = new FakeServer()
      .handle("session.create", () => ({ session_id: "s-new", created: true }))
      .handle("prompt.submit", () => {
        throw new FakeRpcError(1001, "type something", { name: "TEXT_EMPTY" });
      });
    const client = new IndusClient({ connect: server.connect });
    client.start();
    await expect(client.request("session.create", { title: "t" })).resolves.toEqual({
      session_id: "s-new",
      created: true,
    });
    const error = await caught<GatewayRpcError>(
      client.request("prompt.submit", { session_id: "s-new", text: " " }),
    );
    expect(error).toBeInstanceOf(GatewayRpcError);
    expect(error.errorName).toBe("TEXT_EMPTY");
    expect(server.calls("session.create")[0]?.params).toEqual({ title: "t" });
    await client.close();
  });

  it("answers an id-bearing server request it has no handler for with -32601", async () => {
    const server = new FakeServer();
    const client = new IndusClient({ connect: server.connect });
    client.start();
    client.handle({});
    const reply = await server.request("desktop.window.read", {});
    expect(reply).toMatchObject({ id: "srq-000000000001", error: { code: -32601 } });
    await client.close();
  });

  it("re-delivers open requests from session.resume as replayed, once", async () => {
    const server = new FakeServer().handle("session.resume", () => ({
      session_id: "s-fake",
      message_count: 4,
      last_seq: 9,
      pending_requests: [card("open-1")],
    }));
    const client = new IndusClient({ connect: server.connect });
    const replayed: boolean[] = [];
    client.handle({
      serverRequests: {
        approval: (_request, meta) => {
          replayed.push(meta.replayed);
          return { kind: "approval", choice: "session" };
        },
      },
    });
    client.start();
    const answered = server.ask(card("open-1"));
    await settle();
    await client.resume({ session_id: "s-fake" });
    await expect(answered).resolves.toEqual({ kind: "approval", choice: "session" });
    expect(replayed).toEqual([false]);
    await client.close();
  });

  it("closes a card when the server withdraws it and passes the event on", async () => {
    const server = new FakeServer();
    const client = new IndusClient({ connect: server.connect });
    const seen: string[] = [];
    let aborted = false;
    client.subscribe((event: ClientEvent) => seen.push(event.type));
    client.handle({
      serverRequests: {
        approval: (_request, meta) =>
          new Promise(() => {
            meta.signal.addEventListener("abort", () => {
              aborted = true;
            });
          }),
      },
    });
    client.start();
    void server.ask(card("gone-1"));
    await settle();
    server.cancel("gone-1");
    await settle();
    expect(aborted).toBe(true);
    expect(seen).toEqual(["server_request.cancelled"]);
    await client.close();
  });

  it("fails calls of the old connection when start() replaces it, and ignores start after close", async () => {
    const server = new FakeServer();
    server.stall();
    const client = new IndusClient({ connect: server.connect });
    client.start();
    const stuck = caught(client.request("session.create", {}));
    client.start();
    expect(await stuck).toBeInstanceOf(TransportReplaced);
    await client.close();
    client.start();
    expect(server.connections).toBe(2);
    expect(client.logs.lines()).toContain("[lifecycle] start ignored after close");
  });
});
