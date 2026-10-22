import { describe, expect, it } from "vitest";
import type { GatewayConnection } from "../../gateway/connectionStore.ts";
import { rpcOf } from "./managementSlots.tsx";

describe("rpcOf", () => {
  it("is one object per connection and passes the connection's notifications on", async () => {
    const listeners = new Set<(event: unknown) => void>();
    const sent: Array<[string, unknown]> = [];
    const connection = {
      request: async (method: string, params: unknown) => {
        sent.push([method, params]);
        return { ok: true };
      },
      onEvent: (listener: (event: unknown) => void) => {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
    } as unknown as GatewayConnection;
    const rpc = rpcOf(connection);
    expect(rpcOf(connection)).toBe(rpc); // a re-render keeps the room's watch
    expect(await rpc.request("groups.watch", { room_id: "r1" })).toEqual({ ok: true });
    expect(sent).toEqual([["groups.watch", { room_id: "r1" }]]);
    const seen: string[] = [];
    const off = rpc.onEvent?.((event) => seen.push(event.type));
    for (const listener of listeners) listener({ type: "groups.event", room_id: "r1" });
    off?.();
    expect(listeners.size).toBe(0);
    expect(seen).toEqual(["groups.event"]);
  });
});
