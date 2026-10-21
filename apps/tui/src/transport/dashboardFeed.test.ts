// P18-09 follow-up: the dashboard feed — the env plan, what is published, the queue before open,
// size caps, redials and the codes that stop it. The real `/api/pub` route is exercised by
// tests/contract/web/test_tui_publish.py.

import { describe, expect, it } from "vitest";
import {
  DASHBOARD_SUBPROTOCOL,
  DashboardFeed,
  FEED_QUEUE_FRAMES,
  FEED_RETRIES,
  type FeedSocket,
  type FeedTimers,
  feedDelay,
  feedUrlProblem,
  INTERNAL_SUBPROTOCOL_PREFIX,
  isPublishable,
  MAX_BUFFERED_BYTES,
  MAX_FEED_FRAME_BYTES,
  SIDECAR_CREDENTIAL_ENV,
  SIDECAR_URL_ENV,
  takeFeedPlan,
} from "./dashboardFeed.ts";

const URL_OK = "ws://127.0.0.1:47123/api/pub?channel=chat-1";
const credential = (): string => `c${"Z".repeat(20)}${Math.random().toString(36).slice(2, 10)}`;

class FakeSocket implements FeedSocket {
  readyState = 0;
  bufferedAmount = 0;
  readonly sent: string[] = [];
  closed: number | null = null;
  readonly listeners = new Map<string, Array<(event: never) => void>>();

  readonly url: string;
  readonly protocols: string[];

  constructor(url: string, protocols: string[]) {
    this.url = url;
    this.protocols = protocols;
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(code?: number): void {
    this.closed = code ?? 1000;
  }

  addEventListener(type: string, listener: (event: never) => void): void {
    const list = this.listeners.get(type) ?? [];
    list.push(listener);
    this.listeners.set(type, list);
  }

  fire(type: string, event: object = {}): void {
    for (const listener of this.listeners.get(type) ?? []) listener(event as never);
  }

  open(): void {
    this.readyState = 1;
    this.fire("open");
  }

  drop(code: number): void {
    this.readyState = 3;
    this.fire("close", { code });
  }
}

class ManualTimers implements FeedTimers {
  readonly pending: Array<{ run: () => void; ms: number; handle: number }> = [];
  #next = 0;

  set(run: () => void, ms: number): unknown {
    this.#next += 1;
    this.pending.push({ run, ms, handle: this.#next });
    return this.#next;
  }

  clear(handle: unknown): void {
    const at = this.pending.findIndex((p) => p.handle === handle);
    if (at >= 0) this.pending.splice(at, 1);
  }

  fireNext(): number {
    const next = this.pending.shift();
    if (next === undefined) throw new Error("no timer");
    next.run();
    return next.ms;
  }
}

function rig() {
  const sockets: FakeSocket[] = [];
  const timers = new ManualTimers();
  const secret = credential();
  const plan = takeFeedPlan({ [SIDECAR_URL_ENV]: URL_OK, [SIDECAR_CREDENTIAL_ENV]: secret });
  if (plan === null) throw new Error("no plan");
  const feed = new DashboardFeed(plan, {
    socket: (url, protocols) => {
      const socket = new FakeSocket(url, protocols);
      sockets.push(socket);
      return socket;
    },
    timers,
  });
  return { feed, sockets, timers, secret };
}

const reply = (n: number) => ({ type: "message.delta", session_id: "s1", seq: n, text: `t${n}` });

describe("the plan", () => {
  it("needs both variables and takes the credential out of the environment", () => {
    const secret = credential();
    const env: Record<string, string | undefined> = {
      [SIDECAR_URL_ENV]: URL_OK,
      [SIDECAR_CREDENTIAL_ENV]: secret,
      OTHER: "kept",
    };
    const plan = takeFeedPlan(env);
    expect(plan).toEqual({
      url: URL_OK,
      protocols: [DASHBOARD_SUBPROTOCOL, `${INTERNAL_SUBPROTOCOL_PREFIX}${secret}`],
    });
    expect(SIDECAR_CREDENTIAL_ENV in env).toBe(false);
    expect(env.OTHER).toBe("kept");
    expect(plan?.url.includes(secret)).toBe(false);

    expect(takeFeedPlan({})).toBeNull();
    const onlyUrl = takeFeedPlan({ [SIDECAR_URL_ENV]: URL_OK });
    expect(onlyUrl).toBeNull();
    const leftover: Record<string, string | undefined> = { [SIDECAR_CREDENTIAL_ENV]: secret };
    expect(takeFeedPlan(leftover)).toBeNull();
    expect(SIDECAR_CREDENTIAL_ENV in leftover).toBe(false); // removed even when unused
  });

  it("refuses URLs and credentials it cannot use", () => {
    expect(feedUrlProblem(URL_OK)).toBeNull();
    expect(feedUrlProblem("wss://example.test/api/pub?channel=a")).toBeNull();
    expect(feedUrlProblem("http://127.0.0.1/api/pub")).toBe("not a ws: or wss: URL");
    expect(feedUrlProblem("ws://user:pw@127.0.0.1/api/pub")).toBe("the URL carries user-info");
    expect(feedUrlProblem("ws://127.0.0.1/api/pub#x")).toBe("the URL carries a fragment");
    expect(feedUrlProblem("nonsense")).toBe("not a URL");
    const bad = takeFeedPlan({ [SIDECAR_URL_ENV]: URL_OK, [SIDECAR_CREDENTIAL_ENV]: "a b,c" });
    expect(bad).toBeNull();
  });
});

describe("what is published", () => {
  it("protocol events only", () => {
    expect(isPublishable({ type: "session.info" })).toBe(true);
    expect(isPublishable({ type: "transport.stderr" })).toBe(false);
    expect(isPublishable({ type: "transport.exit" })).toBe(false);
    expect(isPublishable({ type: "gateway.ready" })).toBe(false);
    expect(isPublishable({ type: "" })).toBe(false);
    expect(isPublishable({})).toBe(false);
  });

  it("sends each event as one JSON frame once open, queued ones first", () => {
    const { feed, sockets, secret } = rig();
    feed.start();
    const [socket] = sockets;
    expect(socket?.url).toBe(URL_OK);
    expect(socket?.protocols).toEqual([
      DASHBOARD_SUBPROTOCOL,
      `${INTERNAL_SUBPROTOCOL_PREFIX}${secret}`,
    ]);
    feed.publish(reply(1));
    const noise = { type: "transport.stderr", line: "noise" };
    feed.publish(noise);
    feed.publish(reply(2));
    expect(feed.queued).toBe(2);
    socket?.open();
    feed.publish(reply(3));
    expect(socket?.sent.map((f) => JSON.parse(f).seq)).toEqual([1, 2, 3]);
    expect(JSON.parse(socket?.sent[0] ?? "{}")).toEqual(reply(1));
    expect(feed.state).toBe("open");
  });

  it("keeps the newest frames while it waits and skips oversize ones", () => {
    const { feed, sockets } = rig();
    feed.start();
    for (let n = 1; n <= FEED_QUEUE_FRAMES + 5; n += 1) feed.publish(reply(n));
    expect(feed.queued).toBe(FEED_QUEUE_FRAMES);
    expect(feed.dropped).toBe(5);
    const huge = { type: "message.delta", text: "x".repeat(MAX_FEED_FRAME_BYTES + 1) };
    feed.publish(huge);
    expect(feed.dropped).toBe(6);
    sockets[0]?.open();
    expect(JSON.parse(sockets[0]?.sent[0] ?? "{}").seq).toBe(6);
  });

  it("drops frames rather than letting a slow dashboard grow the buffer", () => {
    const { feed, sockets } = rig();
    feed.start();
    sockets[0]?.open();
    const socket = sockets[0];
    if (socket) socket.bufferedAmount = MAX_BUFFERED_BYTES + 1;
    feed.publish(reply(1));
    expect(socket?.sent).toEqual([]);
    expect(feed.dropped).toBe(1);
  });

  it("never throws, whatever it is given", () => {
    const { feed } = rig();
    const cyclic: { type: string; self?: unknown } = { type: "session.info" };
    cyclic.self = cyclic;
    expect(() => feed.publish(cyclic)).not.toThrow();
    const broken = new DashboardFeed(
      { url: URL_OK, protocols: [DASHBOARD_SUBPROTOCOL] },
      {
        socket: () => {
          throw new Error("refused");
        },
        timers: new ManualTimers(),
      },
    );
    expect(() => broken.start()).not.toThrow();
    expect(broken.state).toBe("retrying");
  });
});

describe("the link", () => {
  it("dials again with a growing wait, then gives up", () => {
    const { feed, sockets, timers } = rig();
    feed.start();
    const waits: number[] = [];
    for (let attempt = 1; attempt <= FEED_RETRIES; attempt += 1) {
      sockets.at(-1)?.drop(1006);
      expect(feed.state).toBe("retrying");
      waits.push(timers.fireNext());
    }
    expect(waits).toEqual(Array.from({ length: FEED_RETRIES }, (_, i) => feedDelay(i + 1)));
    expect(waits.slice(0, 3)).toEqual([1_000, 2_000, 4_000]);
    expect(Math.max(...waits)).toBeLessThanOrEqual(30_000);
    sockets.at(-1)?.drop(1006);
    expect(feed.state).toBe("stopped");
    expect(sockets).toHaveLength(FEED_RETRIES + 1);
  });

  it("an open link resets the count", () => {
    const { feed, sockets, timers } = rig();
    feed.start();
    sockets[0]?.drop(1006);
    timers.fireNext();
    sockets[1]?.open();
    sockets[1]?.drop(1001);
    expect(timers.fireNext()).toBe(1_000);
  });

  it.each([4400, 4401, 4403])("close code %i stops it for good", (code) => {
    const { feed, sockets, timers } = rig();
    feed.start();
    sockets[0]?.drop(code);
    expect(feed.state).toBe("stopped");
    expect(timers.pending).toEqual([]);
    feed.publish(reply(1));
    expect(feed.queued).toBe(0);
  });

  it("close() stops the redials and closes the socket", () => {
    const { feed, sockets, timers } = rig();
    feed.start();
    sockets[0]?.open();
    feed.close();
    expect(sockets[0]?.closed).toBe(1000);
    expect(feed.state).toBe("stopped");
    sockets[0]?.drop(1006); // the close event of our own close
    expect(timers.pending).toEqual([]);
  });
});
