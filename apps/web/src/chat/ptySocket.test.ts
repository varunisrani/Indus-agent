import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FakePtyServer, type FakeSocket } from "../testing/fakePtyServer.ts";
import {
  browserPtyDeps,
  PtyConnection,
  type PtyDeps,
  type PtyRequest,
  type PtyStatus,
  ptyParams,
  resizeFrame,
} from "./ptySocket.ts";
import { COPY } from "./reconnect.ts";

// The loopback session token, built at run time (no credential-shaped literal in the source).
const TOKEN = ["loop", "back", "value"].join("-");

interface Rig {
  server: FakePtyServer;
  conn: PtyConnection;
  statuses: PtyStatus[];
  output: string[];
  notices: string[];
  resumed: string[];
  frames: Array<() => void>;
  fontsLoaded: () => void;
  state: { hidden: boolean; gated: boolean; reloadUsed: boolean; reloads: number };
  dialTimes: number[];
}

function makeRig(overrides: Partial<PtyDeps> = {}, options: { autoAccept?: boolean } = {}): Rig {
  const server = new FakePtyServer({ autoAccept: options.autoAccept ?? true });
  const statuses: PtyStatus[] = [];
  const output: string[] = [];
  const notices: string[] = [];
  const resumed: string[] = [];
  const frames: Array<() => void> = [];
  const state = { hidden: false, gated: false, reloadUsed: false, reloads: 0 };
  const dialTimes: number[] = [];
  let fontsLoaded: () => void = () => {};
  const fonts = new Promise<void>((resolve) => {
    fontsLoaded = resolve;
  });
  const deps: PtyDeps = {
    ...browserPtyDeps,
    socket: (url, protocols) => {
      dialTimes.push(Date.now());
      return server.dial(url, protocols);
    },
    frame: (callback) => frames.push(callback),
    fontsReady: () => fonts,
    mayReload: () => !state.reloadUsed,
    reloadOnce: () => {
      if (state.reloadUsed) return false;
      state.reloadUsed = true;
      state.reloads += 1;
      return true;
    },
    gated: () => state.gated,
    hidden: () => state.hidden,
    now: () => Date.now(),
    ...overrides,
  };
  const conn = new PtyConnection(
    {
      output: (text) => output.push(text),
      status: (status) => statuses.push(status),
      notice: (text) => notices.push(text),
      resumed: (id) => resumed.push(id),
    },
    () => ({ cols: 120, rows: 40 }),
    deps,
  );
  return {
    server,
    conn,
    statuses,
    output,
    notices,
    resumed,
    frames,
    fontsLoaded: () => fontsLoaded(),
    state,
    dialTimes,
  };
}

const settle = () => vi.advanceTimersByTimeAsync(0);
const base: PtyRequest = { channel: "chat-one", attach: "tab-a" };

beforeEach(() => {
  vi.useFakeTimers();
  window.__INDUS_SESSION_TOKEN__ = TOKEN;
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  vi.useRealTimers();
});

function phase(rig: Rig): string | undefined {
  return rig.statuses.at(-1)?.phase;
}

describe("dial", () => {
  it("puts channel, attach, profile and a fresh chat's folder in the query, the credential only in the subprotocols", async () => {
    const rig = makeRig();
    rig.conn.connect({ ...base, profile: "work", cwd: "/srv/project", fresh: true });
    await settle();
    const socket = rig.server.last();
    expect(socket.path).toBe("/api/pty");
    expect(Object.fromEntries(socket.query)).toEqual({
      channel: "chat-one",
      attach: "tab-a",
      profile: "work",
      cwd: "/srv/project",
      fresh: "1",
    });
    expect(socket.url).not.toContain(TOKEN);
    expect(socket.protocols).toEqual(["indus-dashboard-v1", `indus-dashboard-token.${TOKEN}`]);
    expect(phase(rig)).toBe("open");

    // A redial repeats the query without `fresh` (it would wipe the channel's session again).
    socket.drop();
    await vi.advanceTimersByTimeAsync(250);
    const again = rig.server.last();
    expect(again).not.toBe(socket);
    expect(again.query.get("fresh")).toBeNull();
    expect(again.query.get("cwd")).toBe("/srv/project");
  });

  it("sends no folder and no fresh flag with a resume", () => {
    expect(ptyParams({ ...base, resume: "s-7", cwd: "/srv/x", fresh: true }, true)).toEqual({
      channel: "chat-one",
      attach: "tab-a",
      resume: "s-7",
    });
  });

  it("sends the size at once, after two animation frames and after fonts load, before any output", async () => {
    const rig = makeRig();
    let sentBeforeOutput = -1;
    rig.conn.connect(base);
    await settle();
    const socket = rig.server.last();
    const firstOutput = rig.output.length;
    rig.server.pty().output("prompt> ");
    if (rig.output.length > firstOutput) sentBeforeOutput = socket.resizes().length;
    expect(sentBeforeOutput).toBe(1);
    expect(socket.sent[0]).toBe(resizeFrame(120, 40));
    expect(rig.output.join("")).toBe("prompt> ");

    rig.frames.shift()?.();
    expect(socket.resizes()).toHaveLength(1);
    rig.frames.shift()?.();
    expect(socket.resizes()).toHaveLength(2);
    rig.fontsLoaded();
    await settle();
    expect(socket.resizes()).toEqual([
      { cols: 120, rows: 40 },
      { cols: 120, rows: 40 },
      { cols: 120, rows: 40 },
    ]);
    expect(socket.input()).toEqual([]);
  });

  it("sends a keep-alive after 20 s of silence only", async () => {
    const rig = makeRig();
    rig.conn.connect(base);
    await settle();
    const socket = rig.server.last();
    expect(socket.resizes()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(19_999);
    expect(socket.resizes()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(socket.resizes()).toHaveLength(2);

    // Traffic in either direction restarts the quiet period.
    await vi.advanceTimersByTimeAsync(10_000);
    rig.server.pty().output("tick");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(socket.resizes()).toHaveLength(2);
    rig.conn.send("a");
    await vi.advanceTimersByTimeAsync(19_000);
    expect(socket.resizes()).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(socket.resizes()).toHaveLength(3);
  });
});

describe("reconnect", () => {
  it("retries 250, 500, 1000, 2000, 3000 ms and then stops", async () => {
    const rig = makeRig({}, { autoAccept: false });
    rig.conn.connect(base);
    await settle();
    rig.server.accept(rig.server.last());
    const drops: number[] = [];
    rig.server.last().drop();
    drops.push(Date.now());
    for (const delay of [250, 500, 1000, 2000, 3000]) {
      expect(phase(rig)).toBe("reconnecting");
      const before = rig.server.sockets.length;
      await vi.advanceTimersByTimeAsync(delay - 1);
      expect(rig.server.sockets.length).toBe(before);
      await vi.advanceTimersByTimeAsync(1);
      expect(rig.server.sockets.length).toBe(before + 1);
      rig.server.last().drop();
      drops.push(Date.now());
    }
    const waits = rig.dialTimes.slice(1).map((at, i) => at - (drops[i] ?? 0));
    expect(waits).toEqual([250, 500, 1000, 2000, 3000]);
    expect(rig.statuses.at(-1)).toEqual({ phase: "stopped", stop: { kind: "gave-up" } });
    const count = rig.server.sockets.length;
    await vi.advanceTimersByTimeAsync(120_000);
    expect(rig.server.sockets.length).toBe(count);

    // Only a manual reconnect starts again, with a fresh ladder.
    rig.conn.reconnect();
    await settle();
    expect(rig.server.sockets.length).toBe(count + 1);
    rig.server.accept(rig.server.last());
    expect(phase(rig)).toBe("open");
  });

  it("parks while hidden or on another page and dials once when back, throttled to 1 s", async () => {
    const rig = makeRig();
    rig.conn.connect(base);
    await settle();
    rig.state.hidden = true;
    rig.server.last().drop();
    expect(phase(rig)).toBe("paused");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(rig.server.sockets).toHaveLength(1);

    rig.state.hidden = false;
    rig.conn.pageResumed();
    rig.conn.pageResumed();
    await settle();
    expect(rig.server.sockets).toHaveLength(2);
    expect(phase(rig)).toBe("open");

    // Leaving the chat page parks a redial the same way; coming back inside the throttle waits.
    rig.conn.setActive(false);
    rig.server.last().drop();
    expect(phase(rig)).toBe("paused");
    await vi.advanceTimersByTimeAsync(200);
    rig.conn.setActive(true);
    await vi.advanceTimersByTimeAsync(799);
    expect(rig.server.sockets).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(rig.server.sockets).toHaveLength(3);

    // A redial that comes due while the tab is hidden parks instead of dialling.
    rig.server.last().drop();
    expect(phase(rig)).toBe("reconnecting");
    rig.state.hidden = true;
    await vi.advanceTimersByTimeAsync(250);
    expect(phase(rig)).toBe("paused");
    expect(rig.server.sockets).toHaveLength(3);
  });

  it("refuses input while reconnecting, with one notice per outage", async () => {
    const rig = makeRig();
    rig.conn.connect(base);
    await settle();
    rig.server.last().drop();
    expect(rig.conn.send("ls\r")).toBe(false);
    expect(rig.conn.send("ls\r")).toBe(false);
    expect(rig.notices).toEqual([COPY.inputRefused]);
    await vi.advanceTimersByTimeAsync(250);
    expect(phase(rig)).toBe("open");
    expect(rig.conn.send("ls\r")).toBe(true);
    expect(rig.server.pty().typed).toEqual(["ls\r"]);
    rig.server.last().drop();
    rig.conn.send("x");
    expect(rig.notices).toHaveLength(2);
  });

  it("gives a stuck handshake 8 s, then counts it as a failed attempt", async () => {
    const rig = makeRig({}, { autoAccept: false });
    rig.conn.connect(base);
    await settle();
    const stuck = rig.server.last();
    await vi.advanceTimersByTimeAsync(7_999);
    expect(stuck.clientClose).toBeNull();
    expect(phase(rig)).toBe("connecting");
    await vi.advanceTimersByTimeAsync(1);
    expect(stuck.clientClose).not.toBeNull();
    expect(phase(rig)).toBe("reconnecting");
    await vi.advanceTimersByTimeAsync(250);
    expect(rig.server.sockets).toHaveLength(2);
  });

  it("gives a ticket that never arrives 8 s, and a late ticket opens nothing", async () => {
    let release: (value: { url: string; protocols: string[] }) => void = () => {};
    const target = vi
      .fn<PtyDeps["target"]>()
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            release = resolve;
          }),
      )
      .mockImplementation(browserPtyDeps.target);
    const rig = makeRig({ target });
    rig.conn.connect(base);
    await vi.advanceTimersByTimeAsync(8_000);
    expect(phase(rig)).toBe("reconnecting");
    expect(rig.server.sockets).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(250);
    expect(target).toHaveBeenCalledTimes(2);
    expect(rig.server.sockets).toHaveLength(1);
    release({ url: "ws://localhost/api/pty?channel=late", protocols: [] });
    await settle();
    expect(rig.server.sockets).toHaveLength(1);
  });
});

describe("close codes", () => {
  async function closedWith(code: number, setup: (rig: Rig) => void = () => {}, clean = true) {
    const rig = makeRig();
    setup(rig);
    rig.conn.connect(base);
    await settle();
    rig.server.last().serverClose(code, "", clean);
    return rig;
  }

  it("4401 in loopback reloads once; a second 4401 shows the login copy", async () => {
    const rig = await closedWith(4401);
    expect(rig.state.reloads).toBe(1);
    expect(phase(rig)).toBe("stopped");
    const again = await closedWith(4401, (r) => {
      r.state.reloadUsed = true;
    });
    expect(again.state.reloads).toBe(0);
    expect(again.statuses.at(-1)).toEqual({
      phase: "stopped",
      stop: { kind: "rejected", message: COPY.loginExpired, reload: true },
    });
  });

  it("4401 in gated mode never reloads", async () => {
    const rig = await closedWith(4401, (r) => {
      r.state.gated = true;
    });
    expect(rig.state.reloads).toBe(0);
    expect(rig.statuses.at(-1)?.stop).toEqual({
      kind: "rejected",
      message: COPY.loginExpired,
      reload: true,
    });
  });

  it.each([
    [4403, COPY.origin],
    [4404, COPY.disabled],
    [4408, COPY.peer],
  ])("%i shows its explanation and does not retry", async (code, message) => {
    const rig = await closedWith(code);
    const stop = rig.statuses.at(-1)?.stop;
    expect(stop?.kind).toBe("rejected");
    expect(stop && "message" in stop ? stop.message : "").toBe(message);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(rig.server.sockets).toHaveLength(1);
  });

  it("1011 ends with the could-not-start overlay", async () => {
    const rig = await closedWith(1011);
    expect(rig.statuses.at(-1)).toEqual({ phase: "ended", stop: { kind: "start-failed" } });
  });

  it("4410 ends the session and marks the terminal", async () => {
    const rig = await closedWith(4410);
    expect(rig.statuses.at(-1)).toEqual({ phase: "ended", stop: { kind: "exited" } });
    expect(rig.output.join("")).toContain(COPY.endedLine);
  });

  it("4409 is quiet and does not take the terminal back", async () => {
    const rig = await closedWith(4409);
    expect(rig.statuses.at(-1)).toEqual({ phase: "stopped", stop: { kind: "superseded" } });
    expect(rig.output).toEqual([]);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(rig.server.sockets).toHaveLength(1);
  });

  it.each([1012, 1013, 1001])("%i reconnects", async (code) => {
    const rig = await closedWith(code);
    expect(phase(rig)).toBe("reconnecting");
    await vi.advanceTimersByTimeAsync(250);
    expect(rig.server.sockets).toHaveLength(2);
  });

  it("an unclean close reconnects, a clean 1000 ends", async () => {
    expect(phase(await closedWith(1006, () => {}, false))).toBe("reconnecting");
    expect((await closedWith(1000)).statuses.at(-1)?.stop).toEqual({ kind: "exited" });
  });
});

describe("resume replay", () => {
  it("an announced resume reports the id and sanitises for 30 s", async () => {
    const rig = makeRig();
    rig.server.channelFiles.set("chat-one", "s-remembered");
    rig.conn.connect(base);
    await settle();
    expect(rig.resumed).toEqual(["s-remembered"]);
    const flood = `top${"\r\n".repeat(81)}bottom\x1b[K`;
    rig.server.pty().output(flood);
    expect(rig.output.join("")).toBe("top\r\n\r\nbottom\x1b[K");
    await vi.advanceTimersByTimeAsync(30_000);
    rig.output.length = 0;
    rig.server.pty().output(flood);
    expect(rig.output.join("")).toBe(flood);
  });

  it("an explicit resume sanitises from the open", async () => {
    const rig = makeRig();
    rig.conn.connect({ ...base, resume: "s-1" });
    await settle();
    expect(rig.resumed).toEqual([]);
    rig.server.pty().output(`a\x1b[2Kb${"\r\n".repeat(60)}`);
    rig.server.pty().output("c");
    expect(rig.output.join("")).toBe("a\x1b[2Kb\r\n\r\nc");
  });
});

describe("keep-alive registry semantics", () => {
  function secondTab(server: FakePtyServer): { conn: PtyConnection; statuses: PtyStatus[] } {
    const statuses: PtyStatus[] = [];
    const conn = new PtyConnection(
      { output: () => {}, status: (s) => statuses.push(s) },
      () => ({ cols: 80, rows: 24 }),
      {
        ...browserPtyDeps,
        socket: server.dial,
        frame: () => {},
        fontsReady: () => new Promise(() => {}),
      },
    );
    return { conn, statuses };
  }

  it("a redial with the same token reattaches the PTY: ring replayed, then a redraw", async () => {
    const rig = makeRig();
    rig.conn.connect(base);
    await settle();
    rig.server.pty().output("history line\r\n");
    rig.server.last().drop();
    rig.server.ptys[0]?.output("while away\r\n");
    rig.output.length = 0;
    await vi.advanceTimersByTimeAsync(250);
    expect(rig.server.ptys).toHaveLength(1);
    expect(rig.output.join("")).toBe("history line\r\nwhile away\r\n\x0c");
  });

  it("another socket with the same key supersedes this one (4409, quiet, no retry)", async () => {
    const rig = makeRig();
    rig.conn.connect(base);
    await settle();
    const other = secondTab(rig.server);
    other.conn.connect(base);
    await settle();
    expect(rig.server.socketsAt("/api/pty")[0]?.serverCloseCode).toBe(4409);
    expect(rig.statuses.at(-1)).toEqual({ phase: "stopped", stop: { kind: "superseded" } });
    expect(other.statuses.at(-1)?.phase).toBe("open");
    await vi.advanceTimersByTimeAsync(30_000);
    expect(rig.server.socketsAt("/api/pty")).toHaveLength(2);
  });

  it("child exit ends the session; a fresh start spawns a new PTY in the same channel", async () => {
    const rig = makeRig();
    rig.server.channelFiles.set("chat-one", "s-old");
    rig.conn.connect(base);
    await settle();
    rig.conn.send("/exit\r");
    expect(rig.server.pty().typed).toEqual(["/exit\r"]);
    rig.server.pty().exit();
    expect(rig.statuses.at(-1)?.phase).toBe("ended");

    rig.conn.connect({ ...base, attach: "tab-a-rotated", fresh: true });
    await settle();
    const socket: FakeSocket = rig.server.last();
    expect(socket.query.get("fresh")).toBe("1");
    expect(rig.server.ptys).toHaveLength(2);
    expect(rig.server.channelFiles.has("chat-one")).toBe(false);
    expect(phase(rig)).toBe("open");
  });

  it("a fresh connect drops the old socket without reacting to its close", async () => {
    const rig = makeRig();
    rig.conn.connect(base);
    await settle();
    const first = rig.server.last();
    rig.conn.connect({ ...base, resume: "s-2" });
    await settle();
    expect(first.clientClose?.code).toBe(1000);
    expect(rig.statuses.filter((s) => s.phase === "reconnecting")).toEqual([]);
    expect(rig.server.last().query.get("resume")).toBe("s-2");
  });
});
