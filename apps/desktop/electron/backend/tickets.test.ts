import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { cleanupFakes, posix, tempDir, writeFakeIndus } from "../../testing/fakeIndus.ts";
import { FakeTimers } from "../../testing/fakeTimers.ts";
import {
  controlQuery,
  homeIsSafe,
  mintTicket,
  POINTER_FILE,
  socketPathFor,
  wsUrlIsSafe,
} from "./control.ts";
import { LocalAttach } from "./localAttach.ts";
import {
  DASHBOARD_TOKEN_PREFIX,
  DialReservations,
  MAX_PENDING_DIALS,
  RESERVATION_TTL_MS,
  TooManyDials,
} from "./tickets.ts";

afterAll(cleanupFakes);

function dial(url: string, protocols: string[]): Promise<{ opened: boolean; code?: number }> {
  return new Promise((resolve) => {
    const ws = new WebSocket(url, protocols);
    ws.addEventListener("message", () => {
      resolve({ opened: true });
      ws.close();
    });
    ws.addEventListener("close", (event) => resolve({ opened: false, code: event.code }));
  });
}

const upgrade = (url: string, protocols: string[], webContentsId = 7) => ({
  url,
  webContentsId,
  resourceType: "webSocket",
  requestHeaders: {
    Origin: "file://",
    "Sec-WebSocket-Protocol": protocols.join(", "),
    "User-Agent": "test",
  },
});

describe.skipIf(!posix)("tickets against the fake daemon", () => {
  it("two dials get two distinct tickets; the fake refuses a reused one", async () => {
    const base = tempDir();
    const home = path.join(base, "h");
    const attach = new LocalAttach({
      backend: {
        command: writeFakeIndus(path.join(base, "indus")),
        args: [],
        label: "fake",
        rung: "command",
        root: null,
      },
      version: "0.20.0",
      profile: null,
      home,
      env: { ...process.env },
      log: () => {},
    });
    const descriptor = await attach.attachGateway();
    const reservations = new DialReservations();
    const one = await attach.mint();
    const two = await attach.mint();
    if (!one.ok || !two.ok) throw new Error("mint failed");
    expect(one.ticket).not.toBe(two.ticket);
    const answer = reservations.reserveLocal(descriptor.wsUrl, one.ticket, 7);
    expect(answer).toEqual({
      ticket: one.ticket,
      wsUrl: descriptor.wsUrl,
      expiresInMs: RESERVATION_TTL_MS,
      protocols: ["indus-gateway-v1", `indus-gateway-ticket.${one.ticket}`],
    });
    expect(answer.wsUrl).not.toContain(one.ticket);
    expect(await dial(answer.wsUrl, answer.protocols)).toEqual({ opened: true });
    expect(await dial(answer.wsUrl, answer.protocols)).toEqual({ opened: false, code: 4401 });
    const second = reservations.reserveLocal(descriptor.wsUrl, two.ticket, 7);
    expect(await dial(second.wsUrl, second.protocols)).toEqual({ opened: true });
  });

  it("control socket path checks: unsafe home, pointer ownership and mode", async () => {
    const base = tempDir();
    const home = path.join(base, "h");
    fs.mkdirSync(home, { mode: 0o700 });
    expect(homeIsSafe(home)).toBe(true);
    expect(await controlQuery(home, "identify")).toEqual({ ok: false, error: "no_socket" });
    fs.chmodSync(home, 0o777);
    expect(homeIsSafe(home)).toBe(false);
    expect(await controlQuery(home, "identify")).toEqual({ ok: false, error: "unsafe_home" });
    fs.chmodSync(home, 0o700);

    // a pointer to a socket in a private directory is followed
    const sockDir = tempDir("s-");
    try {
      fs.chmodSync(sockDir, 0o700);
      const sock = path.join(sockDir, "g.sock");
      const server = net.createServer((conn) => {
        conn.on("data", () =>
          conn.end(
            `${JSON.stringify({ ok: true, protocol: 1, id: "x", result: { pid: 4321 } })}\n`,
          ),
        );
      });
      // the temp dir may be too deep for a socket path: bind by a relative name from inside it
      const before = process.cwd();
      process.chdir(sockDir);
      try {
        await new Promise<void>((resolve) => server.listen("g.sock", resolve));
      } finally {
        process.chdir(before);
      }
      fs.writeFileSync(path.join(home, POINTER_FILE), sock, { mode: 0o600 });
      expect(socketPathFor(home)).toBe(sock);
      expect(await controlQuery(home, "identify")).toEqual({ ok: true, result: { pid: 4321 } });
      // group/other bits on the pointer → not trusted
      fs.chmodSync(path.join(home, POINTER_FILE), 0o644);
      expect(socketPathFor(home)).toBeNull();
      fs.chmodSync(path.join(home, POINTER_FILE), 0o600);
      // the socket's directory open to others → not trusted
      fs.chmodSync(sockDir, 0o755);
      expect(socketPathFor(home)).toBeNull();
      server.close();
    } finally {
      fs.chmodSync(sockDir, 0o700);
    }
  });
});

describe("dial reservations", () => {
  const url = "ws://127.0.0.1:4100/ws";
  const ticket = "t".repeat(43);

  it("strips Origin only for the reserved dial, once", () => {
    const reservations = new DialReservations();
    const { protocols } = reservations.reserveLocal(url, ticket, 7);
    // another window, another port, another path, a query, a non-socket request: untouched
    expect(reservations.rewrite(upgrade(url, protocols, 8))).toBeNull();
    expect(reservations.rewrite(upgrade("ws://127.0.0.1:4101/ws", protocols))).toBeNull();
    expect(reservations.rewrite(upgrade("ws://127.0.0.1:4100/other", protocols))).toBeNull();
    expect(reservations.rewrite(upgrade(`${url}?x=1`, protocols))).toBeNull();
    expect(reservations.rewrite({ ...upgrade(url, protocols), resourceType: "xhr" })).toBeNull();
    expect(
      reservations.rewrite(upgrade(url, ["indus-gateway-v1", "indus-gateway-ticket.other"])),
    ).toBeNull();
    // the reserved one: Origin removed, everything else kept
    expect(reservations.rewrite(upgrade(url, protocols))).toEqual({
      "Sec-WebSocket-Protocol": protocols.join(", "),
      "User-Agent": "test",
    });
    // single use
    expect(reservations.rewrite(upgrade(url, protocols))).toBeNull();
  });

  it("inside Electron (no subprotocol header) a local dial is matched by window and exact URL", () => {
    const timers = new FakeTimers();
    const reservations = new DialReservations(timers.clock);
    const hidden = (target: string, webContentsId = 7) => ({
      url: target,
      webContentsId,
      resourceType: "webSocket",
      requestHeaders: { Origin: "file://", "User-Agent": "test" },
    });
    const stale = reservations.reserveLocal(url, ticket, 7);
    timers.advance(RESERVATION_TTL_MS);
    reservations.reserveLocal(url, "u".repeat(43), 7);
    // another window, another path, a query: untouched; an expired reservation is never used
    expect(reservations.rewrite(hidden(url, 8))).toBeNull();
    expect(reservations.rewrite(hidden("ws://127.0.0.1:4100/other"))).toBeNull();
    expect(reservations.rewrite(hidden(`${url}?x=1`))).toBeNull();
    expect(reservations.rewrite(hidden(url))).toEqual({ "User-Agent": "test" });
    // single use; the expired one is gone too
    expect(reservations.rewrite(hidden(url))).toBeNull();
    expect(reservations.rewrite(upgrade(url, stale.protocols))).toBeNull();
    // a remote reservation is never matched this way (its swap needs the subprotocol header)
    reservations.reserveRemote("wss://agent.example.test/api/ws", "x", 7);
    expect(reservations.rewrite(hidden("wss://agent.example.test/api/ws"))).toBeNull();
  });

  it("a reservation expires at 25 s (fake clock)", () => {
    const timers = new FakeTimers();
    const reservations = new DialReservations(timers.clock);
    const early = reservations.reserveLocal(url, ticket, 7);
    timers.advance(RESERVATION_TTL_MS - 1);
    expect(reservations.rewrite(upgrade(url, early.protocols))).not.toBeNull();
    const late = reservations.reserveLocal(url, "u".repeat(43), 7);
    timers.advance(RESERVATION_TTL_MS);
    expect(reservations.rewrite(upgrade(url, late.protocols))).toBeNull();
    expect(reservations.size()).toBe(0);
  });

  it("at most 100 dials wait at once", () => {
    const reservations = new DialReservations();
    for (let i = 0; i < MAX_PENDING_DIALS; i += 1)
      reservations.reserveLocal(url, `${String(i).padStart(43, "x")}`, 7);
    expect(() => reservations.reserveLocal(url, "y".repeat(43), 7)).toThrow(TooManyDials);
  });

  it("a remote dial swaps the reservation id for the token on the wire only", () => {
    const reservations = new DialReservations();
    const token = ["remote", "token", "value"].join("-");
    const remote = "wss://agent.example.test/api/ws";
    const answer = reservations.reserveRemote(remote, token, 7);
    expect(JSON.stringify(answer)).not.toContain(token);
    expect(answer.protocols[0]).toBe("indus-dashboard-v1");
    const headers = reservations.rewrite(upgrade(remote, answer.protocols));
    expect(headers?.["Sec-WebSocket-Protocol"]).toBe(
      `indus-dashboard-v1, ${DASHBOARD_TOKEN_PREFIX}${token}`,
    );
    expect(headers?.Origin).toBeUndefined();
  });

  it("only loopback ws URLs without credentials are accepted from the daemon", async () => {
    expect(wsUrlIsSafe("ws://127.0.0.1:4100/ws")).toBe(true);
    expect(wsUrlIsSafe("ws://[::1]:4100/ws")).toBe(true);
    expect(wsUrlIsSafe("ws://example.test:4100/ws")).toBe(false);
    expect(wsUrlIsSafe("ws://127.0.0.1/ws")).toBe(false);
    expect(wsUrlIsSafe("ws://u:p@127.0.0.1:4100/ws")).toBe(false);
    expect(wsUrlIsSafe("ws://127.0.0.1:4100/ws?ticket=x")).toBe(false);
    const answer = await mintTicket("/nowhere", {}, async () => ({
      ok: true,
      result: {
        ticket: "a".repeat(43),
        ws_url: "ws://127.0.0.1:1/ws",
        profile_id: "p",
        instance_id: "other",
        expires_in: 30,
      },
    }));
    expect(answer.ok).toBe(true);
    const stale = await mintTicket("/nowhere", { instanceId: "mine" }, async () => ({
      ok: true,
      result: {
        ticket: "a".repeat(43),
        ws_url: "ws://127.0.0.1:1/ws",
        profile_id: "p",
        instance_id: "other",
      },
    }));
    expect(stale).toMatchObject({ ok: false, error: "stale_instance" });
  });
});
