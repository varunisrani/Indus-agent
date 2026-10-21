// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { RpcLike } from "./canonicalChat.ts";
import { GroupRoom, lineOf } from "./GroupRoom.tsx";

function room(options: { failFirstSend?: boolean } = {}) {
  const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
  let failed = false;
  const events = [
    {
      seq: 1,
      event_id: "e1",
      kind: "room.created",
      actor: { kind: "gateway", id: "g" },
      payload: {},
    },
    {
      seq: 2,
      event_id: "e2",
      kind: "message.member",
      actor: { kind: "member", id: "coder" },
      payload: { text: "Plan ready." },
    },
  ];
  const rpc: RpcLike = {
    request: async (method, params) => {
      calls.push({ method, params });
      if (method === "groups.log") {
        const since = Number(params.since_seq ?? 0);
        return { events: events.filter((e) => e.seq > since), latest_seq: 2, has_more: false };
      }
      if (method === "groups.status") {
        return {
          working: false,
          blocked: true,
          counts: { indeterminate: 1 },
          pending_actions: [{ kind: "retry", task_id: "turn-7f3a9c21e0" }],
        };
      }
      if (method === "groups.send" && options.failFirstSend && !failed) {
        failed = true;
        throw new Error("the connection dropped");
      }
      return {};
    },
  };
  return { rpc, calls };
}

describe("GroupRoom", () => {
  it("shows the room as text and offers only the driver's pending actions", async () => {
    const { rpc, calls } = room();
    render(<GroupRoom rpc={rpc} roomId="r1" pollMs={60_000} newId={() => "id-1"} />);
    expect(await screen.findByText("Plan ready.")).toBeTruthy();
    expect(screen.getByText("room created")).toBeTruthy();
    fireEvent.click(await screen.findByText("Retry turn turn-7f3"));
    await waitFor(() => expect(calls.some((c) => c.method === "groups.retry")).toBe(true));
    expect(calls.find((c) => c.method === "groups.retry")?.params).toEqual({
      room_id: "r1",
      task_id: "turn-7f3a9c21e0",
    });
    expect(
      lineOf({
        seq: 3,
        event_id: "x",
        kind: "member.held",
        actor: {},
        payload: { member: "coder" },
      }).text,
    ).toBe("member held coder");
  });

  it("a retried send keeps its exact client event id; stop gets a fresh cancel id", async () => {
    const { rpc, calls } = room({ failFirstSend: true });
    const ids = ["client-a", "cancel-b", "client-c"];
    render(<GroupRoom rpc={rpc} roomId="r1" pollMs={60_000} newId={() => ids.shift() as string} />);
    await screen.findByText("Plan ready.");
    fireEvent.change(screen.getByLabelText("Message the room"), {
      target: { value: "Stop @coder please" },
    });
    fireEvent.click(screen.getByText("Send"));
    expect(await screen.findByText("the connection dropped")).toBeTruthy();
    fireEvent.click(screen.getByText("Send again"));
    await waitFor(() => expect(calls.filter((c) => c.method === "groups.send")).toHaveLength(2));
    const sends = calls
      .filter((c) => c.method === "groups.send")
      .map((c) => c.params.client_event_id);
    expect(sends).toEqual(["client-a", "client-a"]);
    fireEvent.click(screen.getByText("Stop"));
    await waitFor(() => expect(calls.some((c) => c.method === "groups.stop")).toBe(true));
    expect(calls.find((c) => c.method === "groups.stop")?.params).toEqual({
      room_id: "r1",
      cancel_id: "cancel-b",
    });
  });

  it("group discard needs confirmation", async () => {
    const { rpc, calls } = room();
    let gone = false;
    render(<GroupRoom rpc={rpc} roomId="r1" pollMs={60_000} onDisbanded={() => (gone = true)} />);
    await screen.findByText("Plan ready.");
    fireEvent.click(screen.getByText("Disband"));
    expect(calls.some((c) => c.method === "groups.disband")).toBe(false); // nothing sent yet
    fireEvent.click(screen.getByText("Cancel"));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    fireEvent.click(screen.getByText("Disband"));
    const dialog = screen.getByRole("alertdialog");
    fireEvent.click(dialog.querySelector("button") as HTMLButtonElement);
    await waitFor(() => expect(gone).toBe(true));
    expect(calls.filter((c) => c.method === "groups.disband")).toEqual([
      { method: "groups.disband", params: { room_id: "r1" } },
    ]);
  });
});
