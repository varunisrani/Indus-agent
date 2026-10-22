import type { ClientEvent } from "@indus/client";
import { describe, expect, it } from "vitest";
import { FakeTimers } from "../../testing/fakeTimers.ts";
import { CLIENT_FEATURES, ConnectionStore } from "./connectionStore.ts";
import { descriptor, EPOCH, harness, settle } from "./testSupport.ts";
import {
  RECONNECT_CAP_MS,
  RECONNECT_RESET_AFTER_OPEN_MS,
  RECONNECT_WARNING_AFTER_MS,
  RPC_STARTUP_TIMEOUT_MS,
  TURN_WATCHDOG_SILENCE_MS,
} from "./timing.ts";

function seqs(events: ClientEvent[]): number[] {
  return events
    .map((event) => (event as { seq?: unknown }).seq)
    .filter((seq): seq is number => typeof seq === "number");
}

describe("GatewayConnection", () => {
  it("dials with a fresh single-use ticket offered only as a subprotocol", async () => {
    const h = harness();
    h.connection.start();
    await settle();
    expect(h.dials).toHaveLength(1);
    const dial = h.dials[0];
    expect(dial?.url).toBe("ws://127.0.0.1:4100/ws");
    expect(dial?.url).not.toContain("tk1");
    expect(dial?.protocols).toEqual(["indus-gateway-v1", "indus-gateway-ticket.tk1"]);
    h.ready(["request.decline"]);
    await settle();
    const state = h.connection.getState();
    expect(state.phase).toBe("open");
    expect(state.ready).toBe(true);
    expect(state.countsDeclines).toBe(true);
    expect(JSON.stringify(state)).not.toContain("tk1");
  });

  it("after a drop, resubscribes since the cursor and passes each event on once", async () => {
    const h = harness();
    const seen: ClientEvent[] = [];
    h.connection.onSessionEvent("s1", (event) => seen.push(event));
    h.connection.start();
    await settle();
    h.ready();
    await settle();
    await h.connection.watchSession("s1");
    for (const text of ["a", "b", "c"])
      h.emit("s1", { type: "message.delta", turn_id: "t1", text });
    await settle();
    expect(seqs(seen)).toEqual([1, 2, 3]);

    h.server.die("cut");
    await settle();
    expect(h.connection.getState().phase).toBe("reconnecting");
    // Missed while the link was down.
    h.emit("s1", { type: "message.delta", turn_id: "t1", text: "d" });
    h.emit("s1", { type: "message.delta", turn_id: "t1", text: "e" });
    await h.advance(RECONNECT_CAP_MS);
    expect(h.dials).toHaveLength(2);
    expect(h.dials[1]?.protocols[1]).toBe("indus-gateway-ticket.tk2");
    h.ready();
    await settle();
    const subscribes = h.server.calls("session.subscribe");
    expect(subscribes.at(-1)?.params).toEqual({ session_id: "s1", epoch: EPOCH, cursor: 3 });
    expect(seqs(seen)).toEqual([1, 2, 3, 4, 5]);
    // A second replay of the same window adds nothing.
    for (const event of h.log.get("s1") ?? []) {
      h.server.sendRaw({ jsonrpc: "2.0", method: String(event.type), params: event });
    }
    await settle();
    expect(seqs(seen)).toEqual([1, 2, 3, 4, 5]);
  });

  it("resets the ladder only after a socket stayed open 5 s", async () => {
    const h = harness({ random: () => 0.999 });
    h.connection.start();
    await settle();
    // Three flaps: open, ready, drop at once.
    for (let i = 0; i < 3; i += 1) {
      h.ready();
      await settle();
      h.server.die("flap");
      await settle();
      await h.advance(RECONNECT_CAP_MS);
    }
    expect(h.connection.getState().attempt).toBe(3);
    h.ready();
    await settle();
    await h.advance(RECONNECT_RESET_AFTER_OPEN_MS - 1);
    expect(h.connection.getState().attempt).toBe(3);
    await h.advance(1);
    expect(h.connection.getState().attempt).toBe(0);
    expect(h.connection.getState().failingSince).toBeNull();
  });

  it("backs off with full jitter inside [0, min(cap, 300·2^(n−1)))", async () => {
    const h = harness({ random: () => 0.999 });
    h.connection.start();
    await settle();
    const delays: number[] = [];
    for (let attempt = 1; attempt <= 8; attempt += 1) {
      h.server.die("down");
      await settle();
      const before = h.dials.length;
      const step = Math.min(RECONNECT_CAP_MS, 300 * 2 ** (attempt - 1));
      await h.advance(Math.floor(0.999 * step) - 1);
      expect(h.dials.length).toBe(before);
      await h.advance(1);
      expect(h.dials.length).toBe(before + 1);
      delays.push(step);
    }
    expect(delays.at(-1)).toBe(RECONNECT_CAP_MS);
  });

  it("raises the warning only after 300 s of continuous failure", async () => {
    const h = harness();
    h.failTickets = 10_000;
    h.connection.start();
    await settle();
    expect(h.connection.getState().attempt).toBe(1);
    await h.advance(RECONNECT_WARNING_AFTER_MS - 1_000);
    expect(h.connection.getState().warning).toBe(false);
    await h.advance(1_000);
    expect(h.connection.getState().warning).toBe(true);
    expect(h.connection.getState().phase).toBe("connecting");
    h.failTickets = 0;
    await h.advance(RECONNECT_CAP_MS);
    h.ready();
    await settle();
    expect(h.connection.getState().warning).toBe(true);
    await h.advance(RECONNECT_RESET_AFTER_OPEN_MS);
    expect(h.connection.getState().warning).toBe(false);
  });

  it("closes a link that never gets ready within the startup deadline", async () => {
    const h = harness();
    h.connection.start();
    await settle();
    await h.advance(RPC_STARTUP_TIMEOUT_MS);
    expect(h.connection.getState().attempt).toBe(1);
    expect(h.connection.getState().lastError).toBeTruthy();
  });

  it("drops a replay result that belongs to an older connection", async () => {
    const h = harness();
    const seen: ClientEvent[] = [];
    h.connection.onSessionEvent("s1", (event) => seen.push(event));
    h.connection.start();
    await settle();
    h.ready();
    await settle();
    await h.connection.watchSession("s1");
    h.emit("s1", { type: "message.delta", turn_id: "t1", text: "a" });
    await settle();
    // The next subscribe hangs until we release it.
    let release: (() => void) | null = null;
    h.server.handle("session.subscribe", (params) => {
      return new Promise((resolve) => {
        release = () =>
          resolve({ session_id: params.session_id, replay_epoch: EPOCH, last_seq: 1, replayed: 0 });
      });
    });
    h.server.die("cut");
    await settle();
    await h.advance(RECONNECT_CAP_MS);
    h.ready();
    await settle();
    const generation = h.connection.getState().generation;
    h.server.die("cut again");
    await settle();
    expect(release).not.toBeNull();
    (release as unknown as () => void)();
    await settle();
    expect(h.connection.getState().generation).toBe(generation);
    expect(h.connection.getState().ready).toBe(false);
  });

  it("during a turn, one missed probe re-probes; a streak rebuilds the socket", async () => {
    const h = harness();
    h.connection.start();
    await settle();
    h.ready();
    await settle();
    await h.connection.watchSession("s1");
    h.emit("s1", { type: "turn.started", turn_id: "t1", generation: 1 });
    await settle();
    h.server.stall();
    const first = h.connection.probeLiveness(1_000);
    await h.advance(1_000);
    expect(await first).toBe(false);
    expect(h.dials).toHaveLength(1);
    expect(h.connection.getState().ready).toBe(true);
    // The bounded re-probe also goes unanswered: the socket is rebuilt.
    await h.advance(3_000);
    await h.advance(1_000);
    expect(h.connection.getState().ready).toBe(false);
    await h.advance(RECONNECT_CAP_MS);
    expect(h.dials).toHaveLength(2);
  });

  it("closes at once on a missed probe when no turn runs", async () => {
    const h = harness();
    h.connection.start();
    await settle();
    h.ready();
    await settle();
    h.server.stall();
    const probe = h.connection.probeLiveness(1_000);
    await h.advance(1_000);
    expect(await probe).toBe(false);
    expect(h.connection.getState().ready).toBe(false);
  });

  it("marks a silent turn stalled after the watchdog finds nothing new", async () => {
    const h = harness();
    h.server.handle("events.since", (params) => ({
      session_id: params.session_id,
      replay_epoch: EPOCH,
      last_seq: 1,
      events: [],
    }));
    h.connection.start();
    await settle();
    h.ready();
    await settle();
    await h.connection.watchSession("s1");
    h.emit("s1", { type: "turn.started", turn_id: "t1", generation: 1 });
    await settle();
    await h.advance(TURN_WATCHDOG_SILENCE_MS - 1);
    expect(h.server.calls("events.since")).toHaveLength(0);
    await h.advance(1);
    expect(h.server.calls("events.since")).toHaveLength(1);
    expect(h.connection.getState().stalledSessions).toEqual(["s1"]);
    // Any later event clears it.
    h.emit("s1", { type: "message.delta", turn_id: "t1", text: "late" });
    await settle();
    expect(h.connection.getState().stalledSessions).toEqual([]);
  });

  it("rebuilds the socket when main reports another backend", async () => {
    const h = harness();
    h.connection.start();
    await settle();
    h.ready();
    await settle();
    for (const listener of h.changed) listener(descriptor("inst-2"));
    await settle();
    expect(h.dials).toHaveLength(2);
    expect(h.tickets).toBe(2);
  });

  it("announces the desktop viewer on every socket before replaying its sessions", async () => {
    const h = harness();
    h.server.handle("client.capabilities", () => ({ features: ["request.decline"] }));
    void h.connection.watchSession("s1");
    h.connection.start();
    await settle();
    h.ready();
    await settle();
    const [announce] = h.server.calls("client.capabilities");
    expect(announce?.params).toEqual({ features: [...CLIENT_FEATURES], surface: "gui" });
    expect(CLIENT_FEATURES).toEqual([
      "server_requests",
      "server_requests.preview.act",
      "server_requests.preview.read",
      "request.decline",
    ]);
    const [resubscribe] = h.server.calls("session.subscribe");
    expect(Number(resubscribe?.id)).toBeGreaterThan(Number(announce?.id));
  });

  it("a backend announced while the first dial waits for its descriptor is not a change", async () => {
    const h = harness();
    h.connection.start();
    for (const listener of h.changed) listener(descriptor());
    await settle();
    h.ready();
    await settle();
    expect(h.dials).toHaveLength(1);
    expect(h.tickets).toBe(1);
    expect(h.connection.getState()).toMatchObject({ phase: "open", lastError: null });
  });

  it("stops dialling after close()", async () => {
    const h = harness();
    h.connection.start();
    await settle();
    h.ready();
    await settle();
    await h.connection.close();
    expect(h.connection.getState().phase).toBe("closed");
    await h.advance(RECONNECT_CAP_MS * 4);
    expect(h.dials).toHaveLength(1);
  });

  it("stays failed after a terminal close (forbidden peer) until started again", async () => {
    const timers = new FakeTimers();
    let dials = 0;
    const store = new ConnectionStore({
      bridge: {
        get: async () => descriptor(),
        ticket: async () => ({
          ticket: "x",
          wsUrl: "ws://127.0.0.1:1/ws",
          protocols: ["indus-gateway-v1", "indus-gateway-ticket.x"],
          expiresInMs: 1,
        }),
        onChanged: () => () => undefined,
      },
      clock: {
        now: () => timers.now,
        setTimeout: (run, ms) => timers.setTimeout(run, ms),
        clearTimeout: (handle) => timers.clearTimeout(handle),
      },
      transport: () => {
        dials += 1;
        const closes = new Set<(info: { reason: string; terminal?: boolean }) => void>();
        queueMicrotask(() => {
          for (const listener of closes) listener({ reason: "forbidden (4403)", terminal: true });
        });
        return {
          send: () => undefined,
          onFrame: () => () => undefined,
          onClose: (listener) => {
            closes.add(listener);
            return () => closes.delete(listener);
          },
          close: () => undefined,
        };
      },
    });
    const connection = store.primary();
    connection.start();
    await settle();
    expect(connection.getState().phase).toBe("failed");
    timers.advance(RECONNECT_CAP_MS * 4);
    await settle();
    expect(dials).toBe(1);
    connection.start();
    await settle();
    expect(dials).toBe(2);
  });

  it("offers exactly the subprotocols main returned (remote family too)", async () => {
    const h = harness();
    h.connection.start();
    await settle();
    expect(h.connection.mode).toBe("local");
    expect(h.connection.codeVersion).toBe("0.20.0");
    h.ready();
    await settle();
    h.connection.reconnect();
    await settle();
    expect(h.dials).toHaveLength(2);
    expect(h.dials[1]?.protocols).toEqual(["indus-gateway-v1", "indus-gateway-ticket.tk2"]);
  });

  it("leaves a failed boot to the boot overlay instead of redialling it", async () => {
    const h = harness();
    const bridge = (h.store as unknown as { bridge: { get: () => Promise<unknown> } }).bridge;
    const original = bridge.get;
    bridge.get = async () => {
      throw new Error("boot-local: the runtime did not start");
    };
    h.connection.start();
    await settle();
    expect(h.connection.getState().phase).toBe("failed");
    await h.advance(RECONNECT_CAP_MS * 4);
    expect(h.dials).toHaveLength(0);
    bridge.get = original;
    h.connection.start();
    await settle();
    expect(h.dials).toHaveLength(1);
  });

  it("uses each method's own deadline", async () => {
    const h = harness();
    h.connection.start();
    await settle();
    h.ready();
    await settle();
    h.server.stall();
    const submit = h.connection.request("prompt.submit", { session_id: "s1", text: "hi" });
    let failed = false;
    submit.catch(() => {
      failed = true;
    });
    await h.advance(120_000);
    expect(failed).toBe(false);
    await h.advance(1_800_000 - 120_000);
    expect(failed).toBe(true);
  });
});
