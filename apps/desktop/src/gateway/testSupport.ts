// Test harness for the connection store: a fake bridge (descriptor + ticket mint), the in-memory
// FakeServer as the daemon, and a deterministic clock. No sockets, no Electron.

import type { Clock, Transport } from "@indus/client";
import { FakeServer } from "@indus/client/testing";
import { FakeTimers } from "../../testing/fakeTimers.ts";
import type { IndusConnectionDescriptor } from "../global.d.ts";
import {
  type ConnectionBridge,
  ConnectionStore,
  type DialRequest,
  type GatewayConnection,
} from "./connectionStore.ts";

export const EPOCH = "epoch-1";

export function descriptor(instanceId = "inst-1"): IndusConnectionDescriptor {
  return {
    mode: "local",
    profile: null,
    profileId: "p1",
    instanceId,
    codeVersion: "0.20.0",
    wsUrl: "ws://127.0.0.1:4100/ws",
  };
}

/** Let queued microtasks and the FakeServer's hops run. */
export async function settle(rounds = 8): Promise<void> {
  for (let i = 0; i < rounds; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

export interface Harness {
  timers: FakeTimers;
  clock: Clock;
  server: FakeServer;
  store: ConnectionStore;
  connection: GatewayConnection;
  dials: DialRequest[];
  tickets: number;
  changed: Array<(payload: IndusConnectionDescriptor | null) => void>;
  /** Number of `connection.ticket` calls that should fail before minting again. */
  failTickets: number;
  /** Events the daemon has sent per session, for replays. */
  log: Map<string, Array<Record<string, unknown>>>;
  /** Send ready on the current link. */
  ready(capabilities?: string[]): void;
  /** Emit a session event (seq per session) and remember it for replays. */
  emit(sessionId: string, event: { type: string } & Record<string, unknown>): void;
  /** Advance fake time and let async work settle. */
  advance(ms: number): Promise<void>;
}

export function harness(options: { random?: () => number } = {}): Harness {
  const timers = new FakeTimers();
  const clock: Clock = {
    now: () => timers.now,
    setTimeout: (run, ms) => timers.setTimeout(run, ms),
    clearTimeout: (handle) => timers.clearTimeout(handle),
  };
  const server = new FakeServer();
  const log = new Map<string, Array<Record<string, unknown>>>();
  const seqs = new Map<string, number>();
  const h = {
    timers,
    clock,
    server,
    dials: [] as DialRequest[],
    tickets: 0,
    changed: [] as Array<(payload: IndusConnectionDescriptor | null) => void>,
    failTickets: 0,
    log,
  } as Harness;
  const bridge: ConnectionBridge = {
    get: async () => descriptor(),
    ticket: async () => {
      if (h.failTickets > 0) {
        h.failTickets -= 1;
        throw new Error("internal: stale socket");
      }
      h.tickets += 1;
      return {
        ticket: `tk${h.tickets}`,
        wsUrl: "ws://127.0.0.1:4100/ws",
        protocols: ["indus-gateway-v1", `indus-gateway-ticket.tk${h.tickets}`],
        expiresInMs: 30_000,
      };
    },
    onChanged: (listener) => {
      h.changed.push(listener);
      return () => undefined;
    },
  };
  h.store = new ConnectionStore({
    bridge,
    clock,
    random: options.random ?? (() => 0.5),
    transport: (dial): Transport => {
      h.dials.push(dial);
      return server.connect();
    },
  });
  h.connection = h.store.primary();
  h.ready = (capabilities = []) => {
    server.sendRaw({
      jsonrpc: "2.0",
      method: "gateway.ready",
      params: {
        type: "gateway.ready",
        protocol_version: 1,
        replay_epoch: EPOCH,
        capabilities,
      },
    });
  };
  h.emit = (sessionId, event) => {
    const seq = (seqs.get(sessionId) ?? 0) + 1;
    seqs.set(sessionId, seq);
    const params = { session_id: sessionId, seq, replay_epoch: EPOCH, ...event };
    const list = log.get(sessionId) ?? [];
    list.push(params);
    log.set(sessionId, list);
    server.sendRaw({ jsonrpc: "2.0", method: event.type, params });
  };
  h.advance = async (ms) => {
    timers.advance(ms);
    await settle();
  };
  // The daemon's subscribe: replay what came after the cursor, then answer.
  server.handle("session.subscribe", (params) => {
    const sessionId = String(params.session_id);
    const cursor = typeof params.cursor === "number" ? params.cursor : null;
    const events = log.get(sessionId) ?? [];
    let replayed = 0;
    if (cursor !== null) {
      for (const event of events) {
        if ((event.seq as number) > cursor) {
          server.sendRaw({ jsonrpc: "2.0", method: String(event.type), params: event });
          replayed += 1;
        }
      }
    }
    return {
      session_id: sessionId,
      replay_epoch: EPOCH,
      last_seq: seqs.get(sessionId) ?? 0,
      replayed,
    };
  });
  server.handle("session.unsubscribe", () => ({ ok: true }));
  return h;
}
