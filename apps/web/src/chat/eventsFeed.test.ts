import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FakePtyServer } from "../testing/fakePtyServer.ts";
import {
  browserFeedDeps,
  EventsFeed,
  FEED_ATTEMPTS,
  FEED_COPY,
  type FeedDeps,
  type FeedEvent,
  type FeedStatus,
  feedDelay,
  parseFeedFrame,
} from "./eventsFeed.ts";

const TOKEN = ["feed", "token", "value"].join("-");
const settle = () => vi.advanceTimersByTimeAsync(0);

function makeFeed(overrides: Partial<FeedDeps> = {}, autoAccept = true) {
  const server = new FakePtyServer({ autoAccept });
  const events: FeedEvent[] = [];
  const statuses: FeedStatus[] = [];
  const dialTimes: number[] = [];
  const feed = new EventsFeed(
    "chat-feed",
    { event: (e) => events.push(e), status: (s) => statuses.push(s) },
    {
      ...browserFeedDeps,
      socket: (url, protocols) => {
        dialTimes.push(Date.now());
        return server.dial(url, protocols);
      },
      ...overrides,
    },
  );
  return { server, feed, events, statuses, dialTimes };
}

beforeEach(() => {
  vi.useFakeTimers();
  window.__INDUS_SESSION_TOKEN__ = TOKEN;
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  vi.useRealTimers();
});

describe("EventsFeed", () => {
  it("subscribes to its channel with the credential only in the subprotocols", async () => {
    const rig = makeFeed();
    rig.feed.start();
    await settle();
    const socket = rig.server.last("/api/events");
    expect(socket.query.get("channel")).toBe("chat-feed");
    expect(socket.url).not.toContain(TOKEN);
    expect(socket.protocols).toContain(`indus-dashboard-token.${TOKEN}`);
    expect(rig.statuses.at(-1)).toEqual({ phase: "live" });

    rig.server.publish("chat-feed", { type: "session.info", session_id: "s1", model: "m-live" });
    rig.server.publish("chat-feed", "not json");
    rig.server.publish("other-channel", { type: "status", kind: "working" });
    expect(rig.events).toEqual([{ type: "session.info", session_id: "s1", model: "m-live" }]);
  });

  it("waits 1 s, 2 s, 4 s … capped at 30 s and stops after 15 attempts", async () => {
    const rig = makeFeed({}, false);
    rig.feed.start();
    await settle();
    rig.server.accept(rig.server.last("/api/events"));
    const drops: number[] = [];
    rig.server.last("/api/events").drop();
    drops.push(Date.now());
    const expected: number[] = [];
    for (let attempt = 1; attempt <= FEED_ATTEMPTS; attempt += 1) {
      const delay = feedDelay(attempt);
      expected.push(delay);
      await vi.advanceTimersByTimeAsync(delay);
      rig.server.last("/api/events").drop();
      drops.push(Date.now());
    }
    const waits = rig.dialTimes.slice(1).map((at, i) => at - (drops[i] ?? 0));
    expect(waits).toEqual(expected);
    expect(expected.slice(0, 7)).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000]);
    expect(rig.statuses.at(-1)).toEqual({
      phase: "stopped",
      notice: FEED_COPY.gaveUp,
      reload: false,
    });
    const count = rig.server.sockets.length;
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(rig.server.sockets.length).toBe(count);

    rig.feed.reconnect();
    await settle();
    expect(rig.server.sockets.length).toBe(count + 1);
  });

  it("an open resets the attempt count", async () => {
    const rig = makeFeed();
    rig.feed.start();
    await settle();
    rig.server.last("/api/events").drop();
    expect(rig.feed.attempts).toBe(1);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(rig.statuses.at(-1)).toEqual({ phase: "live" });
    expect(rig.feed.attempts).toBe(0);
    // The next outage starts from the first step again (1 s, not 2 s).
    rig.server.last("/api/events").drop();
    expect(rig.feed.attempts).toBe(1);
    await vi.advanceTimersByTimeAsync(999);
    expect(rig.server.sockets).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(rig.server.sockets).toHaveLength(3);
  });

  it.each([4401, 4403])("stops at %i with the reload notice", async (code) => {
    const rig = makeFeed();
    rig.feed.start();
    await settle();
    rig.server.last("/api/events").serverClose(code);
    expect(rig.statuses.at(-1)).toEqual({
      phase: "stopped",
      notice: FEED_COPY.loginExpired,
      reload: true,
    });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(rig.server.sockets).toHaveLength(1);
  });

  it("a bad channel (4400) stops without asking for a reload", async () => {
    const server = new FakePtyServer();
    const statuses: FeedStatus[] = [];
    const feed = new EventsFeed(
      "not a channel!",
      { event: () => {}, status: (s) => statuses.push(s) },
      { ...browserFeedDeps, socket: server.dial },
    );
    feed.start();
    await settle();
    expect(statuses.at(-1)).toEqual({
      phase: "stopped",
      notice: FEED_COPY.unavailable,
      reload: false,
    });
  });

  it("gives each attempt 15 s, a hung ticket included", async () => {
    const target = vi
      .fn<FeedDeps["target"]>()
      .mockImplementationOnce(() => new Promise(() => {}))
      .mockImplementation(browserFeedDeps.target);
    const rig = makeFeed({ target }, false);
    rig.feed.start();
    await vi.advanceTimersByTimeAsync(14_999);
    expect(rig.statuses.at(-1)?.phase).toBe("connecting");
    await vi.advanceTimersByTimeAsync(1);
    expect(rig.statuses.at(-1)?.phase).toBe("retrying");
    await vi.advanceTimersByTimeAsync(1_000);
    expect(target).toHaveBeenCalledTimes(2);
    const stuck = rig.server.last("/api/events");
    await vi.advanceTimersByTimeAsync(15_000);
    expect(stuck.clientClose).not.toBeNull();
    expect(rig.statuses.at(-1)?.phase).toBe("retrying");
  });

  it("stop closes the socket and nothing redials", async () => {
    const rig = makeFeed();
    rig.feed.start();
    await settle();
    const socket = rig.server.last("/api/events");
    rig.feed.stop();
    expect(socket.clientClose?.code).toBe(1000);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(rig.server.sockets).toHaveLength(1);
  });
});

describe("parseFeedFrame", () => {
  it("reads protocol events and JSON-RPC notifications, ignores the rest", () => {
    expect(parseFeedFrame('{"type":"turn.done","session_id":"s"}')).toEqual({
      type: "turn.done",
      session_id: "s",
    });
    expect(
      parseFeedFrame('{"jsonrpc":"2.0","method":"message.complete","params":{"text":"hi"}}'),
    ).toEqual({ type: "message.complete", text: "hi" });
    expect(parseFeedFrame("[1,2]")).toBeNull();
    expect(parseFeedFrame('{"id":1,"result":{}}')).toBeNull();
    expect(parseFeedFrame("plain text")).toBeNull();
  });
});
