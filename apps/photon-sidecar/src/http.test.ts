import { mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { type IncomingMessage, request, type Server } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createSidecarServer,
  isLoopbackHost,
  listen,
  MAX_BODY_BYTES,
  tokenMatches,
} from "./http.ts";
import { LinkError, type SpectrumLink } from "./link.ts";
import { Relay } from "./relay.ts";
import { ANA, ANA_SPACE, FakePhoton, rawMessage, textFile } from "./testing/fakeLink.ts";

const TOKEN = ["s", "i", "d", "e"].join("").repeat(10);

interface Answer {
  status: number;
  headers: Record<string, string | string[] | undefined>;
  body: string;
  json: () => Record<string, unknown>;
}

let dir: string;
let server: Server;
let port: number;
let photon: FakePhoton;
let relay: Relay;
let link: SpectrumLink | null;
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

/** Open the event stream and collect its lines until `count` events (heartbeats counted apart). */
function openStream(path: string): Promise<{
  res: IncomingMessage;
  events: Record<string, unknown>[];
  beats: () => number;
  until: (count: number) => Promise<void>;
  close: () => void;
}> {
  return new Promise((resolve, reject) => {
    const req = request(
      {
        host: "127.0.0.1",
        port,
        method: "GET",
        path,
        headers: { authorization: `Bearer ${TOKEN}` },
      },
      (res) => {
        const events: Record<string, unknown>[] = [];
        let beats = 0;
        let pending = "";
        const waiters: (() => void)[] = [];
        res.setEncoding("utf8");
        res.on("data", (chunk: string) => {
          const lines = (pending + chunk).split("\n");
          pending = lines.pop() ?? "";
          for (const line of lines) {
            if (line === "") beats += 1;
            else events.push(JSON.parse(line) as Record<string, unknown>);
          }
          for (const wake of waiters.splice(0)) wake();
        });
        const until = async (count: number): Promise<void> => {
          while (events.length < count) {
            await new Promise<void>((wake) => waiters.push(wake));
          }
        };
        resolve({ res, events, beats: () => beats, until, close: () => req.destroy() });
      },
    );
    req.on("error", reject);
    req.end();
  });
}

beforeEach(async () => {
  dir = mkdtempSync(join(tmpdir(), "photon-http-"));
  photon = new FakePhoton();
  relay = new Relay();
  link = null;
  compare = vi.fn(tokenMatches);
  server = createSidecarServer({
    token: TOKEN,
    profile: "work",
    relay,
    link: () => link,
    version: "1",
    compare,
    heartbeatMs: 50,
  });
  port = await listen(server, "127.0.0.1", 0);
  link = await photon.provider({
    projectId: "project",
    projectSecret: "secret",
    handlers: {
      onState: (state, detail) => relay.setStatus(state, detail ?? ""),
      onMessage: (message) => relay.accept(message),
    },
  });
});

afterEach(async () => {
  server.closeAllConnections();
  await new Promise<void>((resolve) => server.close(() => resolve()));
  rmSync(dir, { recursive: true, force: true });
});

describe("the door", () => {
  it("refuses a request without the token, or with another one, in constant time", async () => {
    const none = await call("GET", "/health", undefined, { authorization: "" });
    expect(none.status).toBe(401);
    expect(none.json()).toEqual({ error: "unauthorized" });
    const wrong = await call("GET", "/health", undefined, { authorization: "Bearer nope" });
    expect(wrong.status).toBe(401);
    expect(compare).toHaveBeenCalledWith("nope", TOKEN);
    const send = await call(
      "POST",
      "/send",
      { space: ANA_SPACE, text: "hi" },
      { authorization: "" },
    );
    expect(send.status).toBe(401);
    expect(photon.calls).toEqual([]);
    expect(tokenMatches(TOKEN, TOKEN)).toBe(true);
    expect(tokenMatches(`${TOKEN}x`, TOKEN)).toBe(false);
    expect(tokenMatches("", "")).toBe(false);
  });

  it("refuses a Host that is not loopback (DNS rebinding) before the token", async () => {
    const answer = await call("GET", "/health", undefined, { host: "evil.example:8789" });
    expect(answer.status).toBe(400);
    expect(answer.json()).toEqual({ error: "bad_host" });
    expect(compare).not.toHaveBeenCalled();
    expect(isLoopbackHost("[::1]")).toBe(true);
    expect(isLoopbackHost("0.0.0.0")).toBe(false);
  });

  it("binds loopback only", async () => {
    await expect(
      listen(
        createSidecarServer({ token: TOKEN, profile: "", relay, link: () => null }),
        "0.0.0.0",
        0,
      ),
    ).rejects.toThrow(/loopback/);
  });

  it("refuses an oversized body before reading it, unknown routes and wrong methods", async () => {
    const big = await call("POST", "/send", "x".repeat(MAX_BODY_BYTES + 1));
    expect(big.status).toBe(413);
    expect((await call("GET", "/nope")).status).toBe(404);
    expect((await call("GET", "/send")).status).toBe(405);
    expect((await call("POST", "/send", "{not json")).status).toBe(400);
    expect(photon.calls).toEqual([]);
  });
});

describe("health", () => {
  it("reports the link state, the profile and the run", async () => {
    const answer = await call("GET", "/health");
    expect(answer.status).toBe(200);
    expect(answer.json()).toMatchObject({
      status: "open",
      profile: "work",
      epoch: relay.epoch,
      version: "1",
      kept: 0,
    });
    photon.state("degraded", "stream silent");
    expect((await call("GET", "/health")).json()).toMatchObject({
      status: "degraded",
      link: { state: "degraded", detail: "stream silent" },
    });
  });
});

describe("the event stream", () => {
  it("replays kept events after the cursor, then streams live ones with heartbeats", async () => {
    photon.deliver({ id: "A" });
    photon.deliver({ id: "B" });
    const stream = await openStream(`/events?after=1&epoch=${relay.epoch}`);
    await stream.until(1);
    expect(stream.events.map((e) => e.id)).toEqual(["B"]);
    expect(stream.res.headers["content-type"]).toContain("application/x-ndjson");
    photon.deliver({ id: "C" });
    await stream.until(2);
    expect(stream.events.map((e) => [e.id, e.seq])).toEqual([
      ["B", 2],
      ["C", 3],
    ]);
    await new Promise((resolve) => setTimeout(resolve, 160));
    expect(stream.beats()).toBeGreaterThanOrEqual(2);
    stream.close();
  });

  it("starts from the oldest kept event when the cursor is from another run", async () => {
    photon.deliver({ id: "A" });
    photon.deliver({ id: "B" });
    const stream = await openStream("/events?after=2&epoch=other");
    await stream.until(2);
    expect(stream.events.map((e) => e.id)).toEqual(["A", "B"]);
    stream.close();
  });

  it("gives a redelivered message again: the adapter dedupes (at-least-once)", async () => {
    photon.deliver({ id: "A" });
    photon.deliver({ id: "A" });
    const stream = await openStream("/events?after=0");
    await stream.until(2);
    expect(stream.events.map((e) => e.id)).toEqual(["A", "A"]);
    stream.close();
  });

  it("drops our own echoes and read receipts; reduces content to our shape", async () => {
    photon.deliver({ id: "ECHO", fromUs: true });
    photon.deliver({ id: "RCPT", content: { type: "receipt", target: "OUT1" } });
    photon.deliver({
      id: "MIX",
      space: { id: "any;+;chat300", type: "group", name: "Family" },
      replyTo: { id: "OUT1", text: "earlier", fromUs: true },
      content: {
        type: "group",
        items: [
          { type: "text", text: "look at this" },
          textFile("photo.jpg", "jpeg", { mime: "image/jpeg" }),
          textFile("Audio Message.caf", "caf", { mime: "audio/x-caf" }),
          { type: "link", url: "https://example.org/a", title: "A", summary: "first" },
          { type: "link", url: "https://example.org/b" },
          { type: "other", name: "poll" },
        ],
      },
    });
    photon.deliver({
      id: "TAP",
      content: { type: "reaction", target: "OUT1", emoji: "👍", targetFromUs: true },
    });
    const stream = await openStream("/events?after=0");
    await stream.until(2);
    const [mixed, tap] = stream.events;
    expect(mixed).toMatchObject({
      id: "MIX",
      kind: "message",
      space: { id: "any;+;chat300", type: "group", name: "Family" },
      sender: { id: ANA, name: "Ana" },
      text: "look at this\nhttps://example.org/b\n[unsupported message: poll]",
      link: { url: "https://example.org/a", title: "A", summary: "first" },
      reply: { id: "OUT1", text: "earlier", fromUs: true },
      reaction: null,
    });
    const media = (mixed?.media ?? []) as Record<string, unknown>[];
    expect(media.map((m) => [m.kind, m.name, m.preview])).toEqual([
      ["image", "photo.jpg", false],
      ["voice", "Audio Message.caf", false],
    ]);
    expect(tap).toMatchObject({
      kind: "reaction",
      reaction: { target: "OUT1", emoji: "👍", targetFromUs: true },
    });
    stream.close();
  });

  it("serves the bytes of a media ref and refuses unknown refs", async () => {
    photon.deliver({ id: "F", content: textFile("notes.txt", "fake notes\n") });
    const event = relay.after(0, relay.epoch)[0];
    const ref = event?.media[0]?.ref ?? "";
    const answer = await call("GET", `/media?ref=${ref}`);
    expect(answer.status).toBe(200);
    expect(answer.body).toBe("fake notes\n");
    expect((await call("GET", `/media?ref=${"0".repeat(24)}`)).status).toBe(404);
    expect((await call("GET", "/media?ref=../etc")).status).toBe(400);
  });

  it("marks rich-link preview art and refuses an oversized attachment", async () => {
    photon.deliver({
      id: "P",
      content: textFile("ABC.pluginPayloadAttachment", "png", { mime: "image/png" }),
    });
    photon.deliver({ id: "BIG", content: textFile("big.bin", "x", { size: 200 * 1024 * 1024 }) });
    const [preview, big] = relay.after(0, relay.epoch);
    expect(preview?.media[0]?.preview).toBe(true);
    expect((await call("GET", `/media?ref=${big?.media[0]?.ref}`)).status).toBe(413);
  });
});

describe("outbound", () => {
  it("sends text, links, files, typing, reactions and read receipts", async () => {
    const file = join(dir, "photo.jpg");
    writeFileSync(file, "jpeg");
    expect((await call("POST", "/send", { space: ANA_SPACE, text: "**hi**" })).json()).toEqual({
      id: "OUT1",
    });
    await call("POST", "/send", { space: ANA, text: "plain", format: "plain" });
    expect(
      (await call("POST", "/send-link", { space: ANA, url: "https://example.org" })).json(),
    ).toEqual({ id: "OUT3" });
    await call("POST", "/send-file", { space: ANA, path: file, voice: false, name: "p.jpg" });
    await call("POST", "/typing", { space: ANA, on: true });
    await call("POST", "/typing", { space: ANA, on: false });
    await call("POST", "/react", { space: ANA, id: "IN1", emoji: "👀" });
    await call("POST", "/unreact", { space: ANA, id: "IN1" });
    await call("POST", "/read", { space: ANA, id: "IN1" });
    expect(photon.calls).toEqual([
      { op: "text", space: ANA_SPACE, text: "**hi**", format: "markdown" },
      { op: "text", space: ANA, text: "plain", format: "plain" },
      { op: "link", space: ANA, url: "https://example.org" },
      { op: "file", space: ANA, file: { path: file, voice: false, name: "p.jpg" } },
      { op: "typing", space: ANA, on: true },
      { op: "typing", space: ANA, on: false },
      { op: "react", space: ANA, id: "IN1", emoji: "👀" },
      { op: "unreact", space: ANA, id: "IN1" },
      { op: "read", space: ANA, id: "IN1" },
    ]);
  });

  it("checks what it sends: URLs, paths (never a symlink or a relative path), spaces", async () => {
    const real = join(dir, "real.txt");
    writeFileSync(real, "x");
    const link_ = join(dir, "link.txt");
    symlinkSync(real, link_);
    expect(
      (await call("POST", "/send-link", { space: ANA, url: "file:///etc/passwd" })).status,
    ).toBe(400);
    expect((await call("POST", "/send-file", { space: ANA, path: "real.txt" })).status).toBe(400);
    expect((await call("POST", "/send-file", { space: ANA, path: link_ })).status).toBe(403);
    expect((await call("POST", "/send-file", { space: ANA, path: dir })).status).toBe(403);
    expect((await call("POST", "/send", { space: "a\nb", text: "x" })).status).toBe(400);
    expect((await call("POST", "/send", { space: ANA, text: 7 })).status).toBe(400);
    expect((await call("POST", "/send", { space: ANA, text: "x", format: "html" })).status).toBe(
      400,
    );
    expect(photon.calls).toEqual([]);
  });

  it("maps provider failures to statuses and codes", async () => {
    const cases: [LinkError, number, string][] = [
      [new LinkError("space_not_found"), 404, "space_not_found"],
      [new LinkError("message_not_found"), 404, "message_not_found"],
      [new LinkError("target_not_allowed"), 403, "target_not_allowed"],
      [new LinkError("auth_failed"), 403, "auth_failed"],
      [new LinkError("forbidden"), 403, "forbidden"],
      [new LinkError("too_large"), 413, "too_large"],
      [new LinkError("bad_request"), 400, "bad_request"],
      [new LinkError("timeout"), 504, "timeout"],
      [new LinkError("failed"), 502, "failed"],
    ];
    for (const [failure, status, code] of cases) {
      photon.failures.push(failure);
      const answer = await call("POST", "/send", { space: ANA, text: "x" });
      expect([answer.status, answer.json().error]).toEqual([status, code]);
    }
    photon.failures.push(new LinkError("rate_limited", "", 7));
    const limited = await call("POST", "/send", { space: ANA, text: "x" });
    expect([limited.status, limited.headers["retry-after"]]).toEqual([429, "7"]);
    photon.failures.push(new Error("boom with details") as LinkError);
    const odd = await call("POST", "/send", { space: ANA, text: "x" });
    expect(odd.json()).toEqual({ error: "failed" });
  });

  it("answers not_connected while the link is down and auth_failed after a refusal", async () => {
    relay.setStatus("connecting");
    const down = await call("POST", "/send", { space: ANA, text: "x" });
    expect([down.status, down.json().error, down.headers["retry-after"]]).toEqual([
      503,
      "not_connected",
      "5",
    ]);
    relay.setStatus("auth_failed");
    const refused = await call("POST", "/send", { space: ANA, text: "x" });
    expect([refused.status, refused.json().error]).toEqual([403, "auth_failed"]);
    link = null;
    relay.setStatus("open");
    expect((await call("POST", "/send", { space: ANA, text: "x" })).status).toBe(503);
    expect(photon.calls).toEqual([]);
    expect(rawMessage().id).toBe("IN1");
  });
});
