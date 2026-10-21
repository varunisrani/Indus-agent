import { afterAll, describe, expect, it } from "vitest";
import { closeFakeServes, fakeServe, SERVE_TOKEN } from "../../testing/fakeServe.ts";
import { OAuthSession } from "./nativeOauth.ts";
import {
  DialError,
  dialProtocols,
  mintRemoteTicket,
  RemoteAuth,
  testRemoteConnection,
} from "./remoteDial.ts";

afterAll(closeFakeServes);

const TOKEN = SERVE_TOKEN;

describe("remote dial", () => {
  it("each dial mints its own single-use ticket", async () => {
    const serve = await fakeServe();
    const auth = new RemoteAuth(serve.url, { mode: "token", token: TOKEN });
    const one = await mintRemoteTicket(auth, { profile: "work" });
    const two = await mintRemoteTicket(auth);
    expect(one.ticket).not.toBe(two.ticket);
    expect(serve.minted).toEqual([one.ticket, two.ticket]);
    expect(one.expiresInMs).toBe(30_000);
    expect(dialProtocols(one.ticket)).toEqual([
      "indus-dashboard-v1",
      `indus-dashboard-ticket.${one.ticket}`,
    ]);
    // the token itself is never offered on a socket
    expect(dialProtocols(one.ticket).join(" ")).not.toContain(TOKEN);
  });

  it("both legs pass with the credential; the socket used a ticket, not the token", async () => {
    const serve = await fakeServe();
    const auth = new RemoteAuth(serve.url, { mode: "token", token: TOKEN });
    const result = await testRemoteConnection(auth, { settleMs: 50 });
    expect(result).toEqual({ ok: true, http: "ok", ws: "ok", nativeSignIn: true });
    const ws = serve.seen.find((line) => line.startsWith("WS "));
    expect(ws).toContain("indus-dashboard-ticket.");
    expect(ws).not.toContain(TOKEN);
  });

  it("HTTP-only success with a WS failure reports the connection as failing", async () => {
    const serve = await fakeServe({ wsClose: 1011 });
    const auth = new RemoteAuth(serve.url, { mode: "token", token: TOKEN });
    const result = await testRemoteConnection(auth, { settleMs: 50 });
    expect(result.ok).toBe(false);
    expect(result.http).toBe("ok");
    expect(result.ws).not.toBe("ok");
    expect(result.message).toMatch(/WebSocket did not work/);
  });

  it("a wrong token is unauthorized on both legs; an old serve is named", async () => {
    const serve = await fakeServe();
    const wrong = new RemoteAuth(serve.url, { mode: "token", token: "nope" });
    const result = await testRemoteConnection(wrong, { settleMs: 50 });
    expect(result).toMatchObject({ ok: false, http: "unauthorized", ws: "unauthorized" });
    const old = await fakeServe({ ticketStatus: 409 });
    await expect(
      mintRemoteTicket(new RemoteAuth(old.url, { mode: "token", token: TOKEN })),
    ).rejects.toMatchObject({ kind: "old-server" });
  });

  it("503 is connectivity, not reauth; an unreachable server is unreachable", async () => {
    const serve = await fakeServe({ configStatus: 503 });
    const auth = new RemoteAuth(serve.url, { mode: "token", token: TOKEN });
    expect((await testRemoteConnection(auth, { settleMs: 50 })).http).toBe("error");
    const gone = new RemoteAuth("http://127.0.0.1:9", { mode: "token", token: TOKEN });
    const result = await testRemoteConnection(gone, { timeoutMs: 2000 });
    expect(result).toMatchObject({ ok: false, http: "unreachable" });
    await expect(mintRemoteTicket(gone)).rejects.toBeInstanceOf(DialError);
  });

  it("access-proxy headers reach the gateway; dropped names never do", async () => {
    const serve = await fakeServe();
    const auth = new RemoteAuth(serve.url, {
      mode: "token",
      token: TOKEN,
      headers: { "CF-Access-Client-Id": "cf-1", Cookie: "stolen=1" },
    });
    await mintRemoteTicket(auth);
    expect(serve.seen.some((line) => line.includes("cf=cf-1"))).toBe(true);
    expect(auth.proxyHeaders(`${serve.url}/api/x`)).toEqual({ "CF-Access-Client-Id": "cf-1" });
  });

  it("an OAuth connection sends its access token and refreshes once on an explicit 401", async () => {
    const access = ["fresh", "access"].join("-");
    const serve = await fakeServe({ bearer: access });
    const stored = {
      value: JSON.stringify({ accessToken: "stale", refreshToken: "r", expiresAt: 9e9 }),
    };
    const refreshes: string[] = [];
    const fakeFetch: typeof fetch = async (input, init) => {
      const url = String(input);
      if (url.endsWith("/auth/native/refresh")) {
        refreshes.push(String(init?.body));
        return new Response(JSON.stringify({ access_token: access, expires_in: 600 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return fetch(input, init);
    };
    const session = new OAuthSession(
      serve.url,
      {
        get: () => stored.value,
        put: (v) => {
          stored.value = v;
        },
        delete: () => {
          stored.value = "";
        },
      },
      { fetch: fakeFetch, nowS: () => 1000 },
    );
    const auth = new RemoteAuth(serve.url, { mode: "oauth", oauth: session }, { fetch: fakeFetch });
    const minted = await mintRemoteTicket(auth);
    expect(minted.ticket).toBe(serve.minted[0]);
    expect(refreshes).toHaveLength(1);
  });
});
