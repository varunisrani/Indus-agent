import http from "node:http";
import type net from "node:net";
import { afterAll, describe, expect, it } from "vitest";
import { FakeTimers } from "../../testing/fakeTimers.ts";
import { isSensitivePath } from "../security/sensitiveFiles.ts";
import {
  ApiProxy,
  buildApiUrl,
  DEFAULT_TIMEOUT_MS,
  type ProxyTarget,
  STARTUP_TIMEOUT_MS,
} from "./proxy.ts";

const TOKEN = ["serve", "launch", "token", "for", "tests"].join("-");
const servers: http.Server[] = [];
afterAll(() => {
  for (const server of servers) server.close();
});

interface Seen {
  method: string;
  url: string;
  token: string | undefined;
  body: string;
}

async function backend(): Promise<{ origin: string; seen: Seen[] }> {
  const seen: Seen[] = [];
  const server = http.createServer((request, response) => {
    let body = "";
    request.on("data", (chunk: Buffer) => {
      body += chunk.toString();
    });
    request.on("end", () => {
      seen.push({
        method: String(request.method),
        url: String(request.url),
        token: request.headers["x-indus-session-token"] as string | undefined,
        body,
      });
      const url = new URL(String(request.url), "http://x");
      if (url.pathname === "/api/hang" || url.pathname === "/api/health") return; // never answers
      response.setHeader("x-indus-session-token", "echoed-secret");
      if (url.pathname === "/api/missing") {
        response
          .writeHead(404, { "content-type": "application/json" })
          .end('{"detail":"no such session"}');
        return;
      }
      if (url.pathname === "/api/text") {
        response.writeHead(200).end("plain words");
        return;
      }
      response
        .writeHead(200, { "content-type": "application/json" })
        .end(JSON.stringify({ ok: true, echo: body }));
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  servers.push(server);
  return { origin: `http://127.0.0.1:${(server.address() as net.AddressInfo).port}`, seen };
}

function proxy(target: ProxyTarget | null, timers?: FakeTimers) {
  return new ApiProxy({
    target: () => target,
    log: () => {},
    ...(timers === undefined ? {} : { timers }),
  });
}

describe("REST proxy", () => {
  it("refuses another origin and paths that walk out of /api/", () => {
    const base = "http://127.0.0.1:4000";
    for (const bad of [
      "http://other/api/x",
      "//other/api/x",
      "/api/../etc/passwd",
      "/api/%2e%2e/x",
      "/api/a/./b",
      "/api//x",
      "/api\\x",
      "/api/x#frag",
      "/other",
      "api/x",
    ]) {
      expect(() => buildApiUrl(base, bad, null), bad).toThrow(
        /bad-request|only paths|walk out|encoded/,
      );
    }
    expect(buildApiUrl(base, "/api/sessions?limit=5", null).toString()).toBe(
      "http://127.0.0.1:4000/api/sessions?limit=5",
    );
    // a remote with a path prefix keeps it
    expect(buildApiUrl("https://agent.example.test/indus", "/api/status", null).toString()).toBe(
      "https://agent.example.test/indus/api/status",
    );
  });

  it("every attempt carries the session token; the answer holds no header and no token", async () => {
    const b = await backend();
    const p = proxy({ origin: b.origin, token: TOKEN, profile: null });
    const first = await p.request({ method: "GET", path: "/api/sessions" });
    const second = await p.request({ method: "POST", path: "/api/sessions", body: { title: "x" } });
    expect(b.seen.map((s) => s.token)).toEqual([TOKEN, TOKEN]);
    expect(first).toEqual({ kind: "response", status: 200, body: { ok: true, echo: "" } });
    expect(second).toEqual({
      kind: "response",
      status: 200,
      body: { ok: true, echo: '{"title":"x"}' },
    });
    expect(JSON.stringify([first, second])).not.toContain(TOKEN);
    expect(JSON.stringify([first, second])).not.toContain("echoed-secret");
  });

  it("404 is a typed not-found, never an exception; a text body stays text", async () => {
    const b = await backend();
    const p = proxy({ origin: b.origin, token: TOKEN, profile: null });
    expect(await p.request({ method: "GET", path: "/api/missing" })).toEqual({
      kind: "not-found",
      status: 404,
      body: { detail: "no such session" },
    });
    expect(await p.request({ method: "GET", path: "/api/text" })).toEqual({
      kind: "response",
      status: 200,
      body: "plain words",
    });
  });

  it("profile appended from the request, else the connection; never doubled", async () => {
    const b = await backend();
    const p = proxy({ origin: b.origin, token: TOKEN, profile: "work" });
    await p.request({ method: "GET", path: "/api/status" });
    await p.request({ method: "GET", path: "/api/config", profile: "home" });
    await p.request({ method: "GET", path: "/api/sessions?profile=other&limit=2" });
    expect(b.seen.map((s) => s.url)).toEqual([
      "/api/status?profile=work",
      "/api/config?profile=home",
      "/api/sessions?profile=other&limit=2",
    ]);
    const plain = proxy({ origin: b.origin, token: TOKEN, profile: null });
    await plain.request({ method: "GET", path: "/api/status" });
    expect(b.seen.at(-1)?.url).toBe("/api/status");
  });

  it("timeout fires at 30 s (fake clock); startup calls get 60 s", async () => {
    const b = await backend();
    const timers = new FakeTimers();
    const p = proxy({ origin: b.origin, token: TOKEN, profile: null }, timers);
    const pending = p.request({ method: "GET", path: "/api/hang" }).catch((e: unknown) => e);
    await new Promise((resolve) => setTimeout(resolve, 50));
    timers.advance(DEFAULT_TIMEOUT_MS - 1);
    let settled = false;
    void pending.then(() => {
      settled = true;
    });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(settled).toBe(false);
    timers.advance(1);
    const error = (await pending) as Error;
    expect(error.message).toContain("longer than 30 s");
    expect((error as { code?: string }).code).toBe("timeout");

    // a startup path: still waiting at 30 s, gives up at 60 s
    const slow = p.request({ method: "GET", path: "/api/health" }).catch((e: unknown) => e);
    let slowSettled = false;
    void slow.then(() => {
      slowSettled = true;
    });
    await new Promise((resolve) => setTimeout(resolve, 50));
    timers.advance(DEFAULT_TIMEOUT_MS);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(slowSettled).toBe(false);
    timers.advance(STARTUP_TIMEOUT_MS - DEFAULT_TIMEOUT_MS);
    expect(((await slow) as Error).message).toContain("longer than 60 s");
  });

  it("no connection yet → not-connected; credential routes and secret files refused", async () => {
    await expect(proxy(null).request({ method: "GET", path: "/api/status" })).rejects.toMatchObject(
      {
        code: "not-connected",
      },
    );
    const b = await backend();
    const p = proxy({ origin: b.origin, token: TOKEN, profile: null });
    await expect(
      p.request({ method: "POST", path: "/api/files/download-ticket", body: { path: "/tmp/x" } }),
    ).rejects.toMatchObject({ code: "forbidden" });
    await expect(
      p.request({ method: "GET", path: "/api/files/read?path=%2Fhome%2Fu%2F.env" }),
    ).rejects.toMatchObject({
      code: "forbidden",
    });
    await expect(
      p.request({
        method: "POST",
        path: "/api/files/upload",
        body: { path: "/home/u/.ssh/config" },
      }),
    ).rejects.toMatchObject({ code: "forbidden" });
    expect(b.seen).toEqual([]);
    await p.request({ method: "GET", path: "/api/files/read?path=%2Fhome%2Fu%2Fnotes.md" });
    expect(b.seen).toHaveLength(1);
  });

  it("sensitive file names", () => {
    for (const name of [
      "/home/u/.env",
      "C:\\proj\\.env.local",
      "/p/prod.env",
      "/home/u/.ssh/id_ed25519",
      "/home/u/id_rsa",
      "/k/server.pem",
      "/k/tls.key",
      "/home/u/.indus/auth.json",
      "/home/u/.indus/secrets.json",
      "/home/u/.aws/config",
      "/home/u/.netrc",
    ]) {
      expect(isSensitivePath(name), name).toBe(true);
    }
    for (const name of ["/p/README.md", "/p/src/env.ts", "/p/.environment-notes", "/p/keys.txt"]) {
      expect(isSensitivePath(name), name).toBe(false);
    }
  });
});
