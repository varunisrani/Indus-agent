import { describe, expect, it, vi } from "vitest";
import {
  type ApiRequest,
  mergePage,
  PAGE_SIZE,
  plainSnippet,
  rowMatches,
  type SessionInfo,
  SessionListStore,
  type SessionRow,
  searchResults,
  toRows,
  UNREAD_LIMIT,
  UnreadTracker,
} from "./sessionList.ts";

class MemoryStorage {
  readonly data = new Map<string, string>();
  get length(): number {
    return this.data.size;
  }
  key(index: number): string | null {
    return [...this.data.keys()][index] ?? null;
  }
  getItem(key: string): string | null {
    return this.data.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.data.set(key, value);
  }
  removeItem(key: string): void {
    this.data.delete(key);
  }
}

function info(id: string, extra: Partial<SessionInfo> = {}): SessionInfo {
  return {
    id,
    title: `title ${id}`,
    started_at: 100,
    last_active: 100,
    message_count: 2,
    ...extra,
  };
}

describe("rows", () => {
  it("a compression lineage is one row: the tip, keyed by the root", () => {
    const rows = toRows([
      info("root", { end_reason: "compression", started_at: 10, last_active: 20, pinned: true }),
      info("mid", {
        parent_session_id: "root",
        end_reason: "compression",
        started_at: 30,
        last_active: 40,
        title: null,
      }),
      info("tip", { parent_session_id: "mid", started_at: 50, last_active: 60, title: null }),
    ]);
    expect(rows).toHaveLength(1);
    const [row] = rows;
    expect(row).toMatchObject({ id: "tip", durableId: "root", title: "title root", pinned: true });
    expect(row?.ids).toEqual(["tip", "mid", "root"]);
    expect(row?.lastActive).toBe(60_000);
    expect(row?.messageCount).toBe(6);
  });

  it("a branch child is its own titled row, even from a compressed parent", () => {
    const rows = toRows([
      info("p", { end_reason: "compression" }),
      info("b", { parent_session_id: "p", kind: "branch", title: "a branch", last_active: 200 }),
    ]);
    expect(rows.map((r) => [r.id, r.branch, r.title])).toEqual([
      ["b", true, "a branch"],
      ["p", false, "title p"],
    ]);
  });

  it("a REST row that already shows the tip names its root", () => {
    const [row] = toRows([info("tip", { lineage_root: "root" })]);
    expect(row?.durableId).toBe("root");
    expect(row?.ids).toEqual(["tip", "root"]);
  });

  it("is newest first and survives junk", () => {
    const rows = toRows([
      info("a", { last_active: 5 }),
      info("b", { last_active: 9 }),
      { id: "" } as SessionInfo,
    ]);
    expect(rows.map((r) => r.id)).toEqual(["b", "a"]);
  });

  it("pages merge by durable id; older rows stay for load-more; vanished ones go", () => {
    const shown = toRows([
      info("old", { last_active: 1 }),
      info("gone", { last_active: 50 }),
      info("x", { last_active: 60 }),
    ]);
    const page = toRows([
      info("x2", { lineage_root: "x", last_active: 70 }),
      info("y", { last_active: 40 }),
    ]);
    const merged = mergePage(shown, page, { complete: false });
    expect(merged.map((r) => r.id)).toEqual(["x2", "y", "old"]);
    expect(mergePage(shown, page, { complete: true }).map((r) => r.id)).toEqual(["x2", "y"]);
  });
});

describe("search", () => {
  const rows = toRows([
    info("s-1234abcd", { title: "Refactor parser", cwd: "/work/indus", git_branch: "feat/x" }),
    info("s-9999", { title: "Lunch plans", lineage_root: "root-9" }),
  ]);

  it("finds a row by id: exact, prefix, padded, and by its root", () => {
    expect(searchResults(rows, "s-1234abcd", null).map((r) => r.id)).toEqual(["s-1234abcd"]);
    expect(searchResults(rows, "  s-1234 ", null).map((r) => r.id)).toEqual(["s-1234abcd"]);
    expect(searchResults(rows, "root-9", null).map((r) => r.id)).toEqual(["s-9999"]);
  });

  it("matches title, folder and branch, ignoring case", () => {
    expect(rowMatches(rows[0] as SessionRow, "PARSER")).toBe(true);
    expect(rowMatches(rows[0] as SessionRow, "work/indus")).toBe(true);
    expect(rowMatches(rows[0] as SessionRow, "feat/")).toBe(true);
    expect(rowMatches(rows[0] as SessionRow, "lunch")).toBe(false);
  });

  it("server hits come first; unknown hits get a row; local extras follow", () => {
    const results = searchResults(rows, "p", [
      { session_id: "s-9999" },
      { session_id: "s-remote", title: null, snippet: "the <mark>p</mark>lan", last_active: 5 },
    ]);
    expect(results.map((r) => r.id)).toEqual(["s-9999", "s-remote", "s-1234abcd"]);
    expect(results[1]?.preview).toBe("the plan");
    expect(plainSnippet("  ")).toBeNull();
    expect(searchResults(rows, "   ", null)).toEqual([]);
  });
});

describe("unread", () => {
  const row = (count: number) => toRows([info("s1", { message_count: count })])[0] as SessionRow;

  it("survives a reload: a new tracker over the same storage still shows the dot", () => {
    const storage = new MemoryStorage();
    const first = new UnreadTracker("work", storage);
    first.observe([row(2)]);
    expect(first.isUnread(row(2))).toBe(false);
    expect(first.isUnread(row(4))).toBe(true);
    const reloaded = new UnreadTracker("work", storage);
    expect(reloaded.isUnread(row(4))).toBe(true);
    // per profile
    expect(new UnreadTracker("home", storage).isUnread(row(4))).toBe(false);
  });

  it("first sight seeds, opening marks seen, the open row never shows a dot", () => {
    const tracker = new UnreadTracker(null, new MemoryStorage());
    expect(tracker.isUnread(row(9))).toBe(false);
    tracker.observe([row(9)]);
    expect(tracker.isUnread(row(12))).toBe(true);
    expect(tracker.isUnread(row(12), "s1")).toBe(false);
    tracker.markSeen(row(12));
    expect(tracker.isUnread(row(12))).toBe(false);
    tracker.markUnread(row(12));
    expect(tracker.isUnread(row(12))).toBe(true);
  });

  it("is bounded", () => {
    const storage = new MemoryStorage();
    const tracker = new UnreadTracker("p", storage);
    tracker.observe(toRows(Array.from({ length: UNREAD_LIMIT + 50 }, (_, i) => info(`s${i}`))));
    const saved = JSON.parse(storage.getItem("indus.desktop.sidebar.seen.p") ?? "{}");
    expect(Object.keys(saved)).toHaveLength(UNREAD_LIMIT);
  });

  it("broken storage is not fatal", () => {
    const broken = {
      length: 0,
      key: () => null,
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("full");
      },
      removeItem: () => undefined,
    };
    const tracker = new UnreadTracker("p", broken);
    expect(() => tracker.observe([row(1)])).not.toThrow();
  });
});

describe("the profile's list store", () => {
  function api(sessions: SessionInfo[], status = 200) {
    return vi.fn<ApiRequest>(async (request) => {
      if (request.method === "PATCH") return { kind: "response", status, body: {} };
      if (request.path.startsWith("/api/sessions/search")) {
        return { kind: "response", status: 200, body: { results: [{ session_id: "zz" }] } };
      }
      return { kind: "response", status: 200, body: { sessions, total: sessions.length } };
    });
  }

  it("loads the profile's rows, naming the profile on every call", async () => {
    const request = api([info("a")]);
    const store = new SessionListStore("work", request, new MemoryStorage());
    await store.refresh();
    expect(store.getState().rows.map((r) => r.id)).toEqual(["a"]);
    expect(request.mock.calls[0]?.[0]).toMatchObject({
      method: "GET",
      path: `/api/sessions?order=recent&archived=exclude&limit=${PAGE_SIZE}`,
      profile: "work",
    });
    expect(await store.search("x")).toEqual([{ session_id: "zz" }]);
  });

  it("pins a compressed conversation on its durable id, optimistically", async () => {
    const request = api([info("tip", { lineage_root: "root" })]);
    const store = new SessionListStore("default", request, new MemoryStorage());
    await store.refresh();
    const row = store.getState().rows[0] as SessionRow;
    const done = store.setPinned(row, true);
    expect(store.getState().rows[0]?.pinned).toBe(true);
    expect(await done).toBe(true);
    expect(request.mock.calls.at(-1)?.[0]).toMatchObject({
      method: "PATCH",
      path: "/api/sessions/root",
      body: { pinned: true },
    });
  });

  it("a write carries the session's mutation identity and rereads a stale one once", async () => {
    let revision = 3;
    const calls: Array<Parameters<ApiRequest>[0]> = [];
    const request = vi.fn<ApiRequest>(async (payload) => {
      calls.push(payload);
      if (payload.path.endsWith("/mutation-snapshot")) {
        return { kind: "response", status: 200, body: { revision, generation: 1 } };
      }
      if (payload.method === "PATCH") {
        const body = payload.body as { expected_revision: number };
        if (body.expected_revision !== 4) {
          revision = 4; // someone else wrote in between
          return { kind: "response", status: 409, body: { detail: { code: "stale_revision" } } };
        }
        return { kind: "response", status: 200, body: {} };
      }
      return { kind: "response", status: 200, body: { sessions: [info("a")] } };
    });
    const store = new SessionListStore("default", request, new MemoryStorage());
    await store.refresh();
    expect(await store.setArchived(store.getState().rows[0] as SessionRow, true)).toBe(true);
    const patches = calls.filter((c) => c.method === "PATCH");
    expect(patches).toHaveLength(2);
    expect(patches[1]?.body).toMatchObject({
      archived: true,
      expected_revision: 4,
      expected_generation: 1,
      request_id: expect.stringMatching(/^sidebar-/),
    });
  });

  it("a refused PATCH rolls back and reports", async () => {
    const request = api([info("a")], 409);
    const store = new SessionListStore("default", request, new MemoryStorage());
    await store.refresh();
    const row = store.getState().rows[0] as SessionRow;
    expect(await store.setPinned(row, true)).toBe(false);
    expect(store.getState().rows[0]?.pinned).toBe(false);
    expect(store.getState().error).toBe("status 409");
  });

  it("archive takes the row out of the live view", async () => {
    const store = new SessionListStore("default", api([info("a"), info("b")]), new MemoryStorage());
    await store.refresh();
    await store.setArchived(store.getState().rows[0] as SessionRow, true);
    expect(store.getState().rows).toHaveLength(1);
  });

  it("an older answer arriving late changes nothing", async () => {
    let release: (value: Awaited<ReturnType<ApiRequest>>) => void = () => undefined;
    const slow = vi
      .fn<ApiRequest>()
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            release = resolve;
          }),
      )
      .mockImplementation(async () => ({
        kind: "response",
        status: 200,
        body: { sessions: [info("new")] },
      }));
    const store = new SessionListStore("default", slow, new MemoryStorage());
    const first = store.refresh();
    await store.refresh();
    release({ kind: "response", status: 200, body: { sessions: [info("old")] } });
    await first;
    expect(store.getState().rows.map((r) => r.id)).toEqual(["new"]);
  });

  it("running dots follow turn events; a finished turn asks for a refresh; wipe empties", async () => {
    const store = new SessionListStore("default", api([info("a")]), new MemoryStorage());
    await store.refresh();
    expect(store.noteEvent({ type: "turn.started", session_id: "a" })).toBe(false);
    expect(store.getState().running.has("a")).toBe(true);
    expect(store.noteEvent({ type: "turn.done", session_id: "a" })).toBe(true);
    expect(store.getState().running.has("a")).toBe(false);
    expect(store.noteEvent({ type: "message.delta" })).toBe(false);
    store.wipe();
    expect(store.getState().rows).toEqual([]);
  });
});
