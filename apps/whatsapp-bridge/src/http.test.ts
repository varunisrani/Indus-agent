import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { request, type Server } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createBridgeServer,
  isLoopbackHost,
  listen,
  MAX_BODY_BYTES,
  tokenMatches,
} from "./http.ts";
import { Session } from "./session.ts";
import { FakeWa, OWN } from "./testing/fakeSocket.ts";

const TOKEN = ["t", "e", "s", "t"].join("").repeat(10);
const ANA = "15550000200@s.whatsapp.net";

interface Answer {
  status: number;
  headers: Record<string, string | string[] | undefined>;
  body: string;
  json: () => Record<string, unknown>;
}

let dir: string;
let server: Server;
let port: number;
let wa: FakeWa;
let session: Session;
let compare: ReturnType<typeof vi.fn<(a: string, b: string) => boolean>>;

function call(
  method: string,
  path: string,
  body?: unknown,
  headers: Record<string, string> = {},
): Promise<Answer> {
  const payload =
    body === undefined ? undefined : typeof body === "string" ? body : JSON.stringify(body);
  return new Promise((resolve, reject) => {
    const req = request(
      {
        host: "127.0.0.1",
        port,
        method,
        path,
        headers: {
          authorization: `Bearer ${TOKEN}`,
          ...(payload === undefined ? {} : { "content-type": "application/json" }),
          ...headers,
        },
      },
      (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (c: Buffer) => chunks.push(c));
        res.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf8");
          resolve({
            status: res.statusCode ?? 0,
            headers: res.headers,
            body: text,
            json: () => JSON.parse(text) as Record<string, unknown>,
          });
        });
      },
    );
    req.on("error", reject);
    if (payload !== undefined) req.write(payload);
    req.end();
  });
}

beforeEach(async () => {
  dir = mkdtempSync(join(tmpdir(), "wa-http-"));
  wa = new FakeWa();
  session = new Session({ provider: wa.provider, authDir: join(dir, "auth"), callTimeoutMs: 200 });
  compare = vi.fn(tokenMatches);
  server = createBridgeServer(session, { token: TOKEN, profile: "work", compare, maxWaitMs: 2000 });
  port = await listen(server, "127.0.0.1", 0);
  await session.start();
  await Promise.resolve();
});

afterEach(async () => {
  await session.stop();
  server.closeAllConnections();
  await new Promise<void>((resolve) => server.close(() => resolve()));
  rmSync(dir, { recursive: true, force: true });
});

describe("the door", () => {
  it("answers 401 without the token or with a wrong one, comparing in constant time", async () => {
    const none = await call("GET", "/health", undefined, { authorization: "" });
    const wrong = await call(
      "POST",
      "/send",
      { chat: ANA, text: "x" },
      { authorization: "Bearer nope" },
    );
    const basic = await call("GET", "/health", undefined, { authorization: `Basic ${TOKEN}` });
    for (const answer of [none, wrong, basic]) {
      expect(answer.status).toBe(401);
      expect(answer.json()).toEqual({ error: "unauthorized" });
    }
    expect(wa.calls).toEqual([]);
    expect(compare).toHaveBeenCalledWith("nope", TOKEN);
    expect(tokenMatches(TOKEN, TOKEN)).toBe(true);
    expect(tokenMatches(TOKEN.slice(1), TOKEN)).toBe(false);
    expect(tokenMatches("", "")).toBe(false);
  });

  it("refuses a Host that is not loopback (DNS rebinding)", async () => {
    const answer = await call("GET", "/health", undefined, { host: "evil.example:3000" });
    expect(answer.status).toBe(400);
    expect(compare).not.toHaveBeenCalled();
    for (const host of ["localhost:3000", "127.0.0.1", "[::1]:3000"]) {
      expect((await call("GET", "/health", undefined, { host })).status).toBe(200);
    }
  });

  it("refuses an oversized body before reading it", async () => {
    const big = JSON.stringify({ chat: ANA, text: "x".repeat(MAX_BODY_BYTES) });
    const answer = await call("POST", "/send", big);
    expect(answer.status).toBe(413);
    expect(wa.calls).toEqual([]);
  });

  it("answers unknown routes 404, wrong methods 405 and bad bodies 400", async () => {
    expect((await call("GET", "/nope")).status).toBe(404);
    expect((await call("GET", "/send")).status).toBe(405);
    expect((await call("POST", "/send", "{not json")).status).toBe(400);
    expect((await call("POST", "/send", { chat: "no-at-sign", text: "x" })).status).toBe(400);
    expect((await call("POST", "/send", { chat: ANA })).status).toBe(400);
    expect((await call("POST", "/send-media", { chat: ANA, kind: "zip", path: "/x" })).status).toBe(
      400,
    );
  });

  it("binds loopback only", async () => {
    const other = createBridgeServer(session, { token: TOKEN, profile: "work" });
    await expect(listen(other, "0.0.0.0", 0)).rejects.toThrow(/loopback/);
    await expect(listen(other, "192.0.2.10", 0)).rejects.toThrow(/loopback/);
    expect(other.listening).toBe(false);
    expect(isLoopbackHost("[::1]")).toBe(true);
    expect(isLoopbackHost("127.0.0.2")).toBe(false);
  });
});

describe("routes", () => {
  it("reports health with the profile and the session's own ids", async () => {
    const answer = (await call("GET", "/health")).json();
    expect(answer).toMatchObject({ status: "open", profile: "work", self: OWN, queued: 0 });
    expect(typeof answer.epoch).toBe("string");
  });

  it("sends text, media, typing, reactions and read receipts", async () => {
    const file = join(dir, "doc.pdf");
    writeFileSync(file, "%PDF-1.4");
    expect((await call("POST", "/send", { chat: ANA, text: "hi", replyTo: "IN1" })).json()).toEqual(
      { id: "OUT1" },
    );
    const media = { chat: ANA, kind: "document", path: file, caption: "c", fileName: "doc.pdf" };
    expect((await call("POST", "/send-media", media)).json()).toEqual({ id: "OUT2" });
    expect((await call("POST", "/typing", { chat: ANA, state: "paused" })).json()).toEqual({
      ok: true,
    });
    expect((await call("POST", "/react", { chat: ANA, id: "IN1", emoji: "👍" })).status).toBe(200);
    expect((await call("POST", "/read", { chat: ANA, id: "IN1", participant: ANA })).status).toBe(
      200,
    );
    expect(wa.calls).toEqual([
      {
        op: "text",
        chat: ANA,
        text: "hi",
        quoted: { id: "IN1", fromMe: false, participant: null },
      },
      {
        op: "media",
        chat: ANA,
        media: { kind: "document", path: file, caption: "c", fileName: "doc.pdf" },
      },
      { op: "presence", chat: ANA, state: "paused" },
      {
        op: "react",
        chat: ANA,
        target: { id: "IN1", fromMe: false, participant: null },
        emoji: "👍",
      },
      { op: "read", chat: ANA, target: { id: "IN1", fromMe: false, participant: ANA } },
    ]);
  });

  it("maps library failures to statuses the adapter reads", async () => {
    const cases: Array<[string, number, string | undefined]> = [
      ["rate_limited", 429, "9"],
      ["chat_not_found", 404, undefined],
      ["forbidden", 403, undefined],
      ["timeout", 504, undefined],
      ["failed", 502, undefined],
    ];
    for (const [code, status, retry] of cases) {
      wa.fail(code as "failed", code === "rate_limited" ? 9 : null);
      const answer = await call("POST", "/send", { chat: ANA, text: "x" });
      expect(answer.status).toBe(status);
      expect(answer.json()).toEqual({ error: code });
      expect(answer.headers["retry-after"]).toBe(retry);
    }
    await session.stop();
    const closed = await call("POST", "/send", { chat: ANA, text: "x" });
    expect(closed.status).toBe(503);
    expect(closed.json()).toEqual({ error: "not_connected" });
  });

  it("long-polls events by cursor and serves media by ref", async () => {
    const empty = (await call("GET", "/events?after=0&wait=0")).json();
    expect(empty.events).toEqual([]);
    const waiting = call("GET", `/events?after=0&epoch=${String(empty.epoch)}&wait=2`);
    setTimeout(() => {
      wa.deliver({
        id: "P1",
        text: "",
        media: {
          kind: "image",
          mime: "image/jpeg",
          name: "",
          size: 3,
          download: async () => new Uint8Array([7, 8, 9]),
        },
      });
    }, 20);
    const page = (await waiting).json() as {
      cursor: number;
      events: Array<{ id: string; media: Array<{ ref: string }> }>;
    };
    expect(page.events.map((e) => e.id)).toEqual(["P1"]);
    expect(page.cursor).toBe(1);
    const ref = page.events[0]?.media[0]?.ref ?? "";
    const media = await call("GET", `/media?ref=${ref}`);
    expect(media.status).toBe(200);
    expect(Buffer.from(media.body, "utf8")).toEqual(Buffer.from([7, 8, 9]));
    expect((await call("GET", "/media?ref=../../etc")).status).toBe(400);
    expect((await call("GET", `/media?ref=${"a".repeat(24)}`)).status).toBe(404);
  });

  it("ends a long-poll when the wait runs out", async () => {
    const started = Date.now();
    const page = (await call("GET", "/events?after=0&wait=0.05")).json();
    expect(page.events).toEqual([]);
    expect(Date.now() - started).toBeLessThan(1500);
  });
});
