import { mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mask, stripDevice } from "./jid.ts";
import { WaError } from "./provider.ts";
import { type LifecycleEvent, OutboundIds, Session, type SessionOptions } from "./session.ts";
import { FakeWa, OWN } from "./testing/fakeSocket.ts";

const ANA = "15550000200@s.whatsapp.net";
const GROUP = "120363000000000300@g.us";

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "wa-session-"));
});

afterEach(() => {
  vi.useRealTimers();
  rmSync(dir, { recursive: true, force: true });
});

async function started(options: Partial<SessionOptions> = {}): Promise<{
  wa: FakeWa;
  session: Session;
  notes: LifecycleEvent[];
}> {
  const wa = new FakeWa();
  const notes: LifecycleEvent[] = [];
  const session = new Session({
    provider: wa.provider,
    authDir: join(dir, "auth"),
    notify: (e) => notes.push(e),
    ...options,
  });
  await session.start();
  await Promise.resolve();
  return { wa, session, notes };
}

async function drain(
  session: Session,
): Promise<ReturnType<Session["events"]> extends Promise<infer P> ? P : never> {
  return session.events(0, session.epoch, 0);
}

describe("OutboundIds", () => {
  it("keeps the newest ids and drops the oldest first", () => {
    const ids = new OutboundIds(2);
    ids.remember("a");
    ids.remember("b");
    ids.remember("a");
    ids.remember("c");
    expect(ids.has("a")).toBe(false);
    expect(ids.has("b")).toBe(true);
    expect(ids.has("c")).toBe(true);
    expect(ids.size).toBe(2);
    expect(ids.has("")).toBe(false);
    expect(() => new OutboundIds(0)).toThrow(RangeError);
  });
});

describe("jid helpers", () => {
  it("strips device suffixes and masks numbers", () => {
    expect(stripDevice("15550000200:12@s.whatsapp.net")).toBe(ANA);
    expect(stripDevice(GROUP)).toBe(GROUP);
    expect(mask(ANA)).toBe("*******0200@s.whatsapp.net");
  });
});

describe("inbound filtering", () => {
  it("drops our own outbound messages when they come back", async () => {
    const { wa, session } = await started({ selfChat: true });
    const id = await session.sendText(OWN.jid, "reply");
    wa.deliver({ id, chat: OWN.jid, fromMe: true, text: "reply" });
    wa.deliver({ id: "TYPED", chat: OWN.jid, fromMe: true, text: "note to self" });
    const page = await drain(session);
    expect(page.events.map((e) => e.text)).toEqual(["note to self"]);
    expect(page.events[0]?.fromMe).toBe(true);
    expect(page.events[0]?.sender).toBe(OWN.jid);
  });

  it("decides on an own message only after a send in flight returned its id", async () => {
    const { wa, session } = await started({ selfChat: true });
    const release = wa.holdSends();
    const sending = session.sendText(OWN.jid, "reply");
    await Promise.resolve();
    wa.deliver({ id: "OUT1", chat: OWN.jid, fromMe: true, text: "reply" });
    expect((await drain(session)).events).toEqual([]);
    release();
    expect(await sending).toBe("OUT1");
    expect((await drain(session)).events).toEqual([]);
  });

  it("drops what the owner types unless self-chat is on and it is their own chat", async () => {
    const off = await started();
    off.wa.deliver({ id: "A", chat: OWN.jid, fromMe: true }, { id: "B", chat: ANA, fromMe: true });
    expect((await drain(off.session)).events).toEqual([]);
    const on = await started({ selfChat: true });
    on.wa.deliver(
      { id: "C", chat: ANA, fromMe: true, text: "owner to Ana" },
      { id: "D", chat: OWN.lid ?? "", fromMe: true, text: "own lid chat" },
    );
    expect((await drain(on.session)).events.map((e) => e.id)).toEqual(["D"]);
  });

  it("never forwards status updates, broadcast lists or channels", async () => {
    const { wa, session } = await started();
    wa.deliver(
      { id: "S", chat: "status@broadcast", participant: ANA },
      { id: "N", chat: "1203630000@newsletter" },
      { id: "L", chat: "1700000000@broadcast" },
      { id: "OK", chat: ANA },
    );
    expect((await drain(session)).events.map((e) => e.id)).toEqual(["OK"]);
  });

  it("normalises groups, devices, mentions, quotes and edits", async () => {
    const { wa, session } = await started();
    wa.deliver(
      {
        id: "G1",
        chat: GROUP,
        participant: "15550000200:7@s.whatsapp.net",
        senderAlt: "77700000200:7@lid",
        text: "@88800000999 look",
        mentions: ["88800000999:3@lid"],
        quoted: { id: "OUT9", participant: `${OWN.jid}`, text: "earlier" },
      },
      { id: "E1", chat: ANA, text: "fixed", editOf: "IN0" },
      { id: "EMPTY", chat: ANA, text: "" },
    );
    const [group, edit, ...rest] = (await drain(session)).events;
    expect(rest).toEqual([]);
    expect(group).toMatchObject({
      chat: GROUP,
      group: true,
      sender: ANA,
      senderAlt: "77700000200@lid",
      mentions: ["88800000999@lid"],
      mentionsMe: true,
      quoted: { id: "OUT9", sender: OWN.jid, text: "earlier", fromMe: true },
      edited: false,
    });
    expect(edit).toMatchObject({
      id: "IN0",
      edited: true,
      text: "fixed",
      group: false,
      sender: ANA,
    });
  });

  it("hands out media refs and downloads only when asked", async () => {
    const { wa, session } = await started();
    let downloads = 0;
    const media = {
      kind: "voice" as const,
      mime: "audio/ogg",
      name: "",
      size: 4,
      download: async () => {
        downloads += 1;
        return new Uint8Array([1, 2, 3, 4]);
      },
    };
    wa.deliver({ id: "V", chat: ANA, text: "", media });
    wa.deliver({ id: "BIG", chat: ANA, text: "", media: { ...media, size: 200 * 1024 * 1024 } });
    const [voice, big] = (await drain(session)).events;
    expect(downloads).toBe(0);
    const ref = voice?.media[0]?.ref ?? "";
    expect(voice?.media[0]).toMatchObject({ kind: "voice", mime: "audio/ogg", size: 4 });
    expect((await session.media(ref)).data).toEqual(new Uint8Array([1, 2, 3, 4]));
    await expect(session.media(big?.media[0]?.ref ?? "")).rejects.toMatchObject({
      code: "too_large",
    });
    await expect(session.media("0".repeat(24))).rejects.toMatchObject({
      code: "message_not_found",
    });
  });
});

describe("the event ring", () => {
  it("pages by cursor and restarts a cursor from another run", async () => {
    const { wa, session } = await started({ ringSize: 3 });
    wa.deliver({ id: "1" }, { id: "2" }, { id: "3" }, { id: "4" });
    const first = await session.events(0, session.epoch, 0);
    expect(first.events.map((e) => e.id)).toEqual(["2", "3", "4"]);
    const next = await session.events(first.cursor, session.epoch, 0);
    expect(next.events).toEqual([]);
    expect(next.cursor).toBe(first.cursor);
    const other = await session.events(first.cursor, "another-run", 0);
    expect(other.events.map((e) => e.id)).toEqual(["2", "3", "4"]);
  });

  it("wakes a waiting poll when an event arrives", async () => {
    const { wa, session } = await started();
    const waiting = session.events(0, session.epoch, 10_000);
    setTimeout(() => wa.deliver({ id: "LATE" }), 5);
    expect((await waiting).events.map((e) => e.id)).toEqual(["LATE"]);
  });

  it("answers a waiting poll at once when the session stops", async () => {
    const { session } = await started();
    const waiting = session.events(0, session.epoch, 10_000);
    await session.stop();
    expect((await waiting).events).toEqual([]);
  });
});

describe("connection", () => {
  it("reconnects with a doubling wait and stops for good when logged out", async () => {
    vi.useFakeTimers();
    const { wa, session, notes } = await started({ retryFirstMs: 1000, retryMaxMs: 4000 });
    await vi.advanceTimersByTimeAsync(0);
    expect(session.status).toBe("open");
    wa.refuseConnects = 3;
    wa.close();
    expect(session.status).toBe("connecting");
    await vi.advanceTimersByTimeAsync(999);
    expect(wa.sockets).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    await vi.advanceTimersByTimeAsync(2000);
    await vi.advanceTimersByTimeAsync(4000);
    expect(wa.refuseConnects).toBe(0);
    await vi.advanceTimersByTimeAsync(4000);
    expect(wa.sockets).toHaveLength(2);
    expect(session.status).toBe("open");
    wa.close(true);
    expect(session.status).toBe("logged_out");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(wa.sockets).toHaveLength(2);
    expect(notes.map((n) => n.event)).toContain("logged_out");
    await session.stop();
  });

  it("asks for pairing only in pair mode, by code when a phone is given", async () => {
    const serve = await started();
    serve.wa.qr();
    expect(serve.session.status).toBe("pairing");
    expect(serve.notes.at(-1)).toEqual({ event: "pairing_required" });

    const wa = new FakeWa();
    wa.autoOpen = false;
    const notes: LifecycleEvent[] = [];
    const session = new Session({
      provider: wa.provider,
      authDir: join(dir, "a2"),
      mode: "pair",
      pairPhone: "15550000999",
      notify: (e) => notes.push(e),
    });
    await session.start();
    const paired = session.whenPaired();
    wa.qr();
    wa.qr();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(wa.calls.filter((c) => c.op === "pair")).toHaveLength(1);
    expect(notes).toContainEqual({ event: "pairing_code", code: "ABCD1234" });
    wa.open();
    await paired;
    expect(notes.at(-1)).toEqual({ event: "connected" });
  });
});

describe("outbound", () => {
  it("refuses to send while not connected", async () => {
    const wa = new FakeWa();
    wa.autoOpen = false;
    const session = new Session({ provider: wa.provider, authDir: join(dir, "auth") });
    await session.start();
    await expect(session.sendText(ANA, "x")).rejects.toMatchObject({ code: "not_connected" });
  });

  it("types failures and times out a call that never answers", async () => {
    const { wa, session } = await started({ callTimeoutMs: 20 });
    wa.fail("rate_limited", 12);
    await expect(session.sendText(ANA, "x")).rejects.toMatchObject({
      code: "rate_limited",
      retryAfter: 12,
    });
    wa.holdSends();
    await expect(session.sendText(ANA, "x")).rejects.toMatchObject({ code: "timeout" });
    await expect(session.sendText(ANA, "x".repeat(70_000))).rejects.toBeInstanceOf(WaError);
  });

  it("sends only regular files outside the auth folder", async () => {
    const { session } = await started();
    const auth = join(dir, "auth");
    mkdirSync(auth, { recursive: true });
    writeFileSync(join(auth, "creds.json"), "{}");
    const file = join(dir, "photo.jpg");
    writeFileSync(file, "jpeg");
    symlinkSync(join(auth, "creds.json"), join(dir, "link.jpg"));
    const media = (path: string) => ({ kind: "image" as const, path });
    await expect(session.sendMedia(ANA, media(file))).resolves.toBe("OUT1");
    for (const [path, code] of [
      ["photo.jpg", "bad_request"],
      [join(dir, "missing.jpg"), "bad_request"],
      [join(dir, "link.jpg"), "bad_request"],
      [dir, "bad_request"],
      [join(auth, "creds.json"), "forbidden"],
    ] as const) {
      await expect(session.sendMedia(ANA, media(path))).rejects.toMatchObject({ code });
    }
  });

  it("passes typing, reactions and read receipts to the socket", async () => {
    const { wa, session } = await started();
    await session.typing(ANA, "composing");
    await session.react(ANA, { id: "IN1", fromMe: false, participant: null }, "👍");
    await session.read(GROUP, { id: "G1", fromMe: false, participant: ANA });
    expect(wa.calls.map((c) => c.op)).toEqual(["presence", "react", "read"]);
  });
});
