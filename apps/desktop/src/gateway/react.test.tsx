// @vitest-environment jsdom
import type { IncomingRequest } from "@indus/client";
import type { ApprovalRequest, ServerRequestAnswer } from "@indus/protocol";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { FakeConnection } from "../chat/testFakes.ts";
import {
  GatewayConnectionProvider,
  ServerRequestStoreProvider,
  useGatewayConnection,
  useOpenServerRequests,
} from "./react.tsx";
import { type OpenServerRequest, ServerRequestStore } from "./serverRequests.ts";

afterEach(cleanup);

const approval: ApprovalRequest = {
  request_id: "r1",
  session_id: "s1",
  kind: "approval",
  command: "rm -rf build",
  description: "deletes a folder",
};

function offer(store: ServerRequestStore): Promise<ServerRequestAnswer> {
  const meta: IncomingRequest = {
    id: "r1",
    method: "approval",
    params: approval,
    replayed: false,
    via: "server_request",
    signal: new AbortController().signal,
  };
  const handler = store.handlers().approval as (
    request: ApprovalRequest,
    meta: IncomingRequest,
  ) => Promise<ServerRequestAnswer>;
  return handler(approval, meta);
}

describe("the chat surfaces' hooks over the real stores", () => {
  it("open requests come bound to the store: the first answer wins, a second is refused", async () => {
    const store = new ServerRequestStore();
    let seen: readonly OpenServerRequest[] = [];
    let other: readonly OpenServerRequest[] = [];
    function Probe() {
      seen = useOpenServerRequests("s1");
      other = useOpenServerRequests(null);
      return null;
    }
    render(
      <ServerRequestStoreProvider value={store}>
        <Probe />
      </ServerRequestStoreProvider>,
    );
    expect(seen).toHaveLength(0);
    let answered: ServerRequestAnswer | null = null;
    act(() => {
      void offer(store).then((answer) => {
        answered = answer;
      });
    });
    expect(seen.map((open) => open.id)).toEqual(["r1"]);
    expect(other).toHaveLength(0);
    const card = seen[0] as OpenServerRequest;
    let first = false;
    let second = true;
    act(() => {
      first = card.answer({ kind: "approval", choice: "once" });
      second = card.answer({ kind: "approval", choice: "deny" });
    });
    await act(async () => undefined);
    expect([first, second]).toEqual([true, false]);
    expect(answered).toEqual({ kind: "approval", choice: "once" });
    expect(store.isAnswered("r1")).toBe(true);
    expect(seen).toHaveLength(0);
  });

  it("the connection is the one provided, else null without a store", () => {
    const fake = new FakeConnection();
    let inside: unknown = "unset";
    let outside: unknown = "unset";
    function Inside() {
      inside = useGatewayConnection();
      return null;
    }
    function Outside() {
      outside = useGatewayConnection();
      return null;
    }
    render(
      <>
        <GatewayConnectionProvider value={fake}>
          <Inside />
        </GatewayConnectionProvider>
        <Outside />
      </>,
    );
    expect(inside).toBe(fake);
    expect(outside).toBeNull();
  });
});
