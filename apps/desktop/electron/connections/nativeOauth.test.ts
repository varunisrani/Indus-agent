import { createHash } from "node:crypto";
import http from "node:http";
import type net from "node:net";
import { afterAll, describe, expect, it } from "vitest";
import {
  authorizeUrl,
  CALLBACK_PATH,
  decodeTokens,
  listenForCallback,
  needsRefresh,
  OAuthError,
  OAuthSession,
  offersNativeSignIn,
  parseCallback,
  parseTokens,
  pkcePair,
  signIn,
  type TokenVault,
} from "./nativeOauth.ts";

const servers: http.Server[] = [];
afterAll(() => {
  for (const server of servers) server.close();
});

const ACCESS = ["access", "value", "one"].join("-");
const REFRESH = ["refresh", "value", "one"].join("-");

function vault(initial: string | null = null): TokenVault & { value: string | null } {
  const box = {
    value: initial,
    get: () => box.value,
    put: (v: string) => {
      box.value = v;
    },
    delete: () => {
      box.value = null;
    },
  };
  return box;
}

async function tokenServer(
  answer: (path: string, body: Record<string, string>) => { status: number; body: unknown },
): Promise<{ base: string; calls: Array<{ path: string; body: Record<string, string> }> }> {
  const calls: Array<{ path: string; body: Record<string, string> }> = [];
  const srv = http.createServer((request, response) => {
    let text = "";
    request.on("data", (chunk: Buffer) => {
      text += chunk.toString();
    });
    request.on("end", () => {
      const body = JSON.parse(text || "{}") as Record<string, string>;
      calls.push({ path: request.url ?? "", body });
      const out = answer(request.url ?? "", body);
      response.writeHead(out.status, { "Content-Type": "application/json" });
      response.end(JSON.stringify(out.body));
    });
  });
  await new Promise<void>((resolve) => srv.listen(0, "127.0.0.1", resolve));
  servers.push(srv);
  return { base: `http://127.0.0.1:${(srv.address() as net.AddressInfo).port}/gw`, calls };
}

describe("PKCE and the callback", () => {
  it("the PKCE pair verifies (RFC 7636 S256)", () => {
    const pair = pkcePair();
    expect(pair.verifier).toMatch(/^[A-Za-z0-9_-]{43}$/);
    const expected = createHash("sha256").update(pair.verifier).digest("base64url");
    expect(pair.challenge).toBe(expected);
    expect(pkcePair().verifier).not.toBe(pair.verifier);
  });

  it("builds the authorize URL on the base's path prefix", () => {
    const url = new URL(
      authorizeUrl("https://gw.example/team/", {
        challenge: "c",
        redirectUri: "http://127.0.0.1:5000/callback",
        state: "s",
      }),
    );
    expect(url.pathname).toBe("/team/auth/native/authorize");
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(url.searchParams.get("redirect_uri")).toBe("http://127.0.0.1:5000/callback");
    expect(offersNativeSignIn({ auth_flows: ["native_pkce"] })).toBe(true);
    expect(offersNativeSignIn({ auth_required: true })).toBe(false);
  });

  it("an OAuth state mismatch is rejected and the code never redeemed", async () => {
    expect(() => parseCallback("/callback?code=abc&state=other", "mine")).toThrow(OAuthError);
    expect(() => parseCallback("/callback?code=abc", "mine")).toThrow(/did not match/);
    expect(() => parseCallback("/callback?error=access_denied", "mine")).toThrow(/refused/);
    expect(parseCallback("/callback?code=abc&state=mine", "mine")).toBe("abc");

    const server = await tokenServer(() => ({ status: 200, body: { access_token: ACCESS } }));
    const flow = signIn(server.base, {
      openBrowser: async (url) => {
        const redirect = new URL(new URL(url).searchParams.get("redirect_uri") as string);
        await fetch(`${redirect.origin}${CALLBACK_PATH}?code=forged&state=not-ours`);
      },
    });
    await expect(flow).rejects.toMatchObject({ kind: "state-mismatch" });
    expect(server.calls).toEqual([]); // nothing was exchanged
  });

  it("accepts exactly one callback on a random loopback port", async () => {
    const loopback = await listenForCallback("st");
    expect(loopback.redirectUri).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/callback$/);
    const origin = new URL(loopback.redirectUri).origin;
    expect((await fetch(`${origin}/elsewhere`)).status).toBe(404);
    expect((await fetch(`${origin}${CALLBACK_PATH}?code=c1&state=st`)).status).toBe(200);
    await expect(loopback.code).resolves.toBe("c1");
  });

  it("times out", async () => {
    const loopback = await listenForCallback("st", { timeoutMs: 20 });
    await expect(loopback.code).rejects.toMatchObject({ kind: "timeout" });
  });
});

describe("the sign-in and the session", () => {
  it("exchanges the code with the verifier and the same redirect", async () => {
    const server = await tokenServer(() => ({
      status: 200,
      body: { access_token: ACCESS, refresh_token: REFRESH, expires_in: 3600 },
    }));
    const tokens = await signIn(server.base, {
      nowS: () => 1000,
      openBrowser: async (url) => {
        const params = new URL(url).searchParams;
        const redirect = params.get("redirect_uri") as string;
        await fetch(`${redirect}?code=good&state=${params.get("state")}`);
      },
    });
    expect(tokens).toEqual({ accessToken: ACCESS, refreshToken: REFRESH, expiresAt: 4600 });
    const exchange = server.calls[0];
    expect(exchange?.path).toBe("/gw/auth/native/token");
    expect(exchange?.body.grant_type).toBe("authorization_code");
    expect(exchange?.body.code).toBe("good");
    expect(exchange?.body.code_verifier).toMatch(/^[A-Za-z0-9_-]{43}$/);
  });

  it("refreshes near expiry, single flight, keeps the rotated refresh token", async () => {
    let n = 0;
    const server = await tokenServer((_path, body) => {
      n += 1;
      return {
        status: 200,
        body: {
          access_token: `a-${n}-${body.refresh_token}`,
          refresh_token: `r-${n}`,
          expires_in: 600,
        },
      };
    });
    let now = 1000;
    const box = vault(
      JSON.stringify({ accessToken: ACCESS, refreshToken: REFRESH, expiresAt: 1100 }),
    );
    const session = new OAuthSession(server.base, box, { nowS: () => now });
    expect(await session.accessToken()).toBe(ACCESS); // 100 s left: fine
    now = 1050; // within 60 s of expiry
    const [one, two] = await Promise.all([session.accessToken(), session.accessToken()]);
    expect(one).toBe(`a-1-${REFRESH}`);
    expect(two).toBe(one);
    expect(server.calls).toHaveLength(1);
    expect(decodeTokens(box.value)?.refreshToken).toBe("r-1");
    expect(needsRefresh({ accessToken: "x", refreshToken: "", expiresAt: 0 }, 0)).toBe(true);
  });

  it("a refused refresh clears the set; a server error keeps it", async () => {
    let status = 503;
    const server = await tokenServer(() => ({ status, body: { error: "x" } }));
    const box = vault(JSON.stringify({ accessToken: ACCESS, refreshToken: REFRESH, expiresAt: 1 }));
    const session = new OAuthSession(server.base, box, { nowS: () => 1000 });
    await expect(session.accessToken()).rejects.toMatchObject({ kind: "unreachable" });
    expect(box.value).not.toBeNull();
    status = 400;
    await expect(session.accessToken()).rejects.toMatchObject({ kind: "signed-out" });
    expect(box.value).toBeNull();
    expect(session.signedIn()).toBe(false);
  });

  it("parses expiry forms and refuses an answer without an access token", () => {
    expect(parseTokens({ access_token: "a", expires_at: 50 }, 10).expiresAt).toBe(50);
    expect(
      parseTokens({ access_token: "a" }, 10, {
        accessToken: "o",
        refreshToken: "keep",
        expiresAt: 0,
      }).refreshToken,
    ).toBe("keep");
    expect(() => parseTokens({}, 10)).toThrow(OAuthError);
  });
});
