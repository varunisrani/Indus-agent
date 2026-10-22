// P25-03 web security audit against a real `indus dashboard` (R09.2–R09.4, D-16.25):
//
//   host guard   a rebinding name gets neither the page (which holds the loopback token) nor the API,
//                over HTTP and over a WebSocket upgrade;
//   sockets      every WebSocket path refuses an upgrade with no credential and with a credential in
//                the URL; a good token from a foreign Origin is refused too;
//   framing      no other site can frame the dashboard (SEC2-1);
//   gated        session cookies are HttpOnly + SameSite=Lax on Path=/; unsafe methods from a foreign
//                or `null` Origin are refused (login included); every protected path answers 401
//                (unknown plugin routes too, never a 404 that names plugins); HTML goes to the login.
//
// Raw requests go out from Node (not the page), so a URL credential used as an attack input never
// reaches the browser's URL log (the `urls` fixture still checks that the product never builds one).

import { randomBytes } from "node:crypto";
import http, { type IncomingHttpHeaders } from "node:http";
import type { AddressInfo } from "node:net";
import net from "node:net";
import { expect, type Page, test } from "./fixtures.ts";

const SESSION_HEADER = "x-indus-session-token";
const PROTOCOL = "indus-dashboard-v1";
/** Every WebSocket route the dashboard mounts (`indus.web.ws.websocket_routes`), plus a plugin one. */
const SOCKET_PATHS = [
  "/api/events?channel=audit",
  "/api/ws",
  "/api/pty",
  "/api/pub",
  "/api/console",
  "/api/ext/no-such-plugin/socket",
];
/**
 * A refused upgrade: accepted and closed with a 44xx code (what a browser can read), or, for an
 * unknown plugin socket, no upgrade at all (the plugin gate answers before any accept).
 */
const REFUSED = /^(?:closed 44\d\d|http 403)$/;
/** Protected API paths: refused without a credential whether or not a route answers them. */
const PROTECTED = [
  "/api/sessions",
  "/api/config",
  "/api/logs",
  "/api/env",
  "/api/status/extra",
  "/api/ext/no-such-plugin/data",
];

interface Answer {
  status: number;
  headers: IncomingHttpHeaders;
  body: string;
}

/** One HTTP request from Node with exactly these headers (`host` included when given). */
function raw(
  base: string,
  path: string,
  init: { method?: string; headers?: Record<string, string>; body?: string } = {},
): Promise<Answer> {
  const url = new URL(base);
  return new Promise((done, fail) => {
    const request = http.request(
      {
        host: url.hostname,
        port: Number(url.port),
        path,
        method: init.method ?? "GET",
        headers: init.headers ?? {},
        agent: false,
      },
      (response) => {
        const chunks: Buffer[] = [];
        response.on("data", (chunk: Buffer) => chunks.push(chunk));
        response.on("end", () =>
          done({
            status: response.statusCode ?? 0,
            headers: response.headers,
            body: Buffer.concat(chunks).toString("utf8"),
          }),
        );
      },
    );
    request.on("error", fail);
    if (init.body !== undefined) request.write(init.body);
    request.end();
  });
}

/**
 * A WebSocket upgrade written by hand, so Host, Origin and the URL are exactly what an attacker
 * would send: "open" (accepted and left open for 1.5 s), "closed <code>" (accepted, then the
 * server's close frame), or "http <status>" (no upgrade at all).
 */
function upgrade(
  base: string,
  path: string,
  options: { host?: string; origin?: string; protocols?: string[] } = {},
): Promise<string> {
  const url = new URL(base);
  return new Promise((done) => {
    const socket = net.connect(Number(url.port), url.hostname);
    let buffer = Buffer.alloc(0);
    let settled = false;
    let quiet: ReturnType<typeof setTimeout> | null = null;
    const finish = (how: string) => {
      if (settled) return;
      settled = true;
      clearTimeout(limit);
      if (quiet !== null) clearTimeout(quiet);
      socket.destroy();
      done(how);
    };
    const limit = setTimeout(() => finish("timeout"), 15_000);
    socket.on("connect", () => {
      const lines = [
        `GET ${path} HTTP/1.1`,
        `Host: ${options.host ?? url.host}`,
        "Upgrade: websocket",
        "Connection: Upgrade",
        `Sec-WebSocket-Key: ${randomBytes(16).toString("base64")}`,
        "Sec-WebSocket-Version: 13",
      ];
      if (options.origin !== undefined) lines.push(`Origin: ${options.origin}`);
      if (options.protocols?.length) {
        lines.push(`Sec-WebSocket-Protocol: ${options.protocols.join(", ")}`);
      }
      socket.write(`${lines.join("\r\n")}\r\n\r\n`);
    });
    socket.on("data", (chunk: Buffer) => {
      buffer = Buffer.concat([buffer, chunk]);
      const end = buffer.indexOf("\r\n\r\n");
      if (end < 0) return;
      const status = Number(buffer.subarray(0, end).toString("latin1").split(" ")[1]);
      if (status !== 101) {
        finish(`http ${status}`);
        return;
      }
      quiet ??= setTimeout(() => finish("open"), 1_500);
      const frame = buffer.subarray(end + 4);
      if (frame.length < 2) return;
      if (((frame[0] ?? 0) & 0x0f) !== 0x8) {
        finish("open");
        return;
      }
      if (frame.length >= 4) finish(`closed ${frame.readUInt16BE(2)}`);
    });
    socket.on("error", () => finish("error"));
    socket.on("close", () => finish("ended"));
  });
}

function tokenOffer(token: string): string[] {
  return [PROTOCOL, `indus-dashboard-token.${token}`];
}

async function pageToken(page: Page, url: string): Promise<string> {
  await page.goto(`${url}/sessions`);
  await expect(page.getByRole("textbox", { name: "Search sessions" })).toBeVisible();
  return page.evaluate(() => window.__INDUS_SESSION_TOKEN__ ?? "");
}

function setCookies(headers: IncomingHttpHeaders): string[] {
  const value = headers["set-cookie"];
  return value === undefined ? [] : Array.isArray(value) ? value : [value];
}

test("loopback: a rebinding Host gets neither the page nor the API", async ({ launch, page }) => {
  const stack = await launch({ turns: [[{ text: "Hello." }]] });
  const token = await pageToken(page, stack.url);
  expect(token.length).toBeGreaterThan(20);
  const port = new URL(stack.url).port;

  // The served page carries the token: only for a Host naming this listener.
  const ours = await raw(stack.url, "/sessions", { headers: { host: `127.0.0.1:${port}` } });
  expect(ours.status).toBe(200);
  expect(ours.body).toContain(token);
  expect((await raw(stack.url, "/", { headers: { host: `localhost:${port}` } })).status).toBe(200);
  for (const host of [
    `evil.example:${port}`,
    "evil.example",
    `127.0.0.1.evil.test:${port}`,
    `user@127.0.0.1:${port}`,
    `127.0.0.1:${port}/x`,
  ]) {
    const page = await raw(stack.url, "/sessions", { headers: { host } });
    expect(page.status, host).toBe(400);
    expect(page.body, host).not.toContain(token);
    const api = await raw(stack.url, "/api/sessions", {
      headers: { host, [SESSION_HEADER]: token },
    });
    expect(api.status, host).toBe(400);
  }
  // A socket with the right token but a rebinding Host is refused as well.
  expect(
    await upgrade(stack.url, "/api/events?channel=audit", {
      host: `evil.example:${port}`,
      protocols: tokenOffer(token),
    }),
  ).toBe("closed 4403");
  expect(
    await upgrade(stack.url, "/api/events?channel=audit", { protocols: tokenOffer(token) }),
  ).toBe("open");
});

test("loopback: every socket path refuses no credential, a URL credential, a foreign Origin", async ({
  launch,
  page,
}) => {
  const stack = await launch({ turns: [[{ text: "Hello." }]] });
  const token = await pageToken(page, stack.url);
  for (const path of SOCKET_PATHS) {
    const joiner = path.includes("?") ? "&" : "?";
    const bare = await upgrade(stack.url, path, { protocols: [PROTOCOL] });
    expect(bare, `${path} without a credential`).toMatch(REFUSED);
    const none = await upgrade(stack.url, path);
    expect(none, `${path} offering nothing`).toMatch(REFUSED);
    for (const key of ["token", "ticket"]) {
      const inUrl = await upgrade(stack.url, `${path}${joiner}${key}=${token}`, {
        protocols: [PROTOCOL],
      });
      expect(inUrl, `${path} with ${key} in the URL`).toMatch(REFUSED);
    }
    // the URL credential is refused even beside a good subprotocol credential
    const both = await upgrade(stack.url, `${path}${joiner}token=${token}`, {
      protocols: tokenOffer(token),
    });
    expect(both, `${path} with a URL credential beside the header one`).toMatch(REFUSED);
  }
  for (const origin of [
    "http://evil.example",
    "https://evil.example",
    "http://127.0.0.1.evil.test",
  ]) {
    expect(
      await upgrade(stack.url, "/api/events?channel=audit", {
        origin,
        protocols: tokenOffer(token),
      }),
      origin,
    ).toBe("closed 4403");
  }
  // From the dashboard's own page the same token opens the socket (the refusals above are real).
  expect(
    await upgrade(stack.url, "/api/events?channel=audit", {
      origin: stack.url,
      protocols: tokenOffer(token),
    }),
  ).toBe("open");
});

test("loopback: protected API paths need the token; public ones do not", async ({ launch }) => {
  const stack = await launch({ turns: [[{ text: "Hello." }]] });
  for (const path of PROTECTED) {
    const answer = await raw(stack.url, path);
    expect(answer.status, path).toBe(401);
    expect(JSON.parse(answer.body).error, path).toBe("unauthenticated");
  }
  expect((await raw(stack.url, "/api/health")).status).toBe(200);
});

test("no other site can frame the dashboard (SEC2-1)", async ({ launch, page }) => {
  const stack = await launch({ turns: [[{ text: "Hello." }]] });
  for (const path of ["/", "/sessions"]) {
    const answer = await raw(stack.url, path);
    expect(answer.headers["x-frame-options"], path).toBe("DENY");
    expect(answer.headers["content-security-policy"], path).toContain("frame-ancestors 'none'");
    expect(answer.headers["x-content-type-options"], path).toBe("nosniff");
  }
  // A page on another origin frames it: the frame never becomes the dashboard.
  const framer = http.createServer((_request, response) => {
    response
      .writeHead(200, { "content-type": "text/html" })
      .end(
        `<!doctype html><title>framer</title><iframe id="target" src="${stack.url}/sessions" width="800" height="600"></iframe>`,
      );
  });
  await new Promise<void>((resolve) => framer.listen(0, "127.0.0.1", resolve));
  try {
    const port = (framer.address() as AddressInfo).port;
    await page.goto(`http://127.0.0.1:${port}/`);
    await page.waitForTimeout(3_000);
    const child = page.frames().find((frame) => frame !== page.mainFrame());
    expect(child).toBeDefined();
    const inside = await child
      ?.evaluate(() => typeof window.__INDUS_SESSION_TOKEN__)
      .catch(() => "unreachable");
    expect(inside).not.toBe("string");
    expect(child?.url() ?? "").not.toBe(`${stack.url}/sessions`);
  } finally {
    await new Promise<void>((resolve) => framer.close(() => resolve()));
  }
});

test("gated: cookie flags, CSRF refusals, 401 on protected paths, Host bound to the listener", async ({
  launch,
  page,
}) => {
  const password = ["e2e", "audit", String(Date.now())].join("-");
  const stack = await launch({
    turns: [[{ text: "Hello." }]],
    dashboard: { host: "127.0.0.2" },
    basicAuth: { username: "admin", password },
  });
  const url = stack.url;
  const port = new URL(url).port;
  const login = JSON.stringify({ username: "admin", password });
  const json = { "content-type": "application/json" };

  // Login CSRF: a foreign or null Origin cannot sign the browser in.
  for (const origin of ["http://evil.example", "null", `http://127.0.0.1:${port}`]) {
    const refused = await raw(url, "/auth/password-login", {
      method: "POST",
      headers: { ...json, origin },
      body: login,
    });
    expect(refused.status, origin).toBe(403);
    expect(setCookies(refused.headers), origin).toEqual([]);
  }

  // The session cookies: HttpOnly, SameSite=Lax, Path=/ (plain http here, so no Secure flag).
  const signed = await raw(url, "/auth/password-login", {
    method: "POST",
    headers: { ...json, origin: url },
    body: login,
  });
  expect(signed.status).toBe(200);
  const cookies = setCookies(signed.headers).filter((line) => !/max-age=0/i.test(line));
  expect(cookies.length).toBeGreaterThan(0);
  for (const line of cookies) {
    expect(line, line).toMatch(/;\s*HttpOnly/i);
    expect(line, line).toMatch(/;\s*SameSite=lax/i);
    expect(line, line).toMatch(/;\s*Path=\//i);
  }
  const jar = cookies.map((line) => line.split(";")[0]).join("; ");

  // With the session: unsafe methods from a foreign or null Origin are refused; ours pass.
  const mint = (origin: string | null) =>
    raw(url, "/api/auth/ws-ticket", {
      method: "POST",
      headers: { ...json, cookie: jar, ...(origin === null ? {} : { origin }) },
      body: JSON.stringify({ purpose: "web-events" }),
    });
  for (const origin of ["http://evil.example", "null", `${url}.evil.test`, "file://"]) {
    const answer = await mint(origin);
    expect(answer.status, origin).toBe(403);
    expect(JSON.parse(answer.body).error, origin).toBe("forbidden_origin");
  }
  const minted = await mint(url);
  expect(minted.status).toBe(200);
  expect(minted.headers["cache-control"]).toBe("no-store");

  // The Host must name this listener: 127.0.0.1 is loopback, but not what it is bound to.
  for (const host of [`127.0.0.1:${port}`, `localhost:${port}`, `evil.example:${port}`]) {
    const answer = await raw(url, "/api/sessions", { headers: { host, cookie: jar } });
    expect(answer.status, host).toBe(400);
  }
  expect((await raw(url, "/api/sessions", { headers: { cookie: jar } })).status).toBe(200);

  // Without a session: every protected API path is 401 with the login URL; HTML goes to login.
  for (const path of PROTECTED) {
    const answer = await raw(url, path);
    expect(answer.status, path).toBe(401);
    const body = JSON.parse(answer.body) as { error: string; login_url?: string };
    expect(body.error, path).toBe("unauthenticated");
    expect(body.login_url, path).toMatch(/\/login/);
  }
  const html = await raw(url, "/sessions");
  expect(html.status).toBe(302);
  expect(String(html.headers.location)).toMatch(/\/login/);
  expect((await raw(url, "/api/health")).status).toBe(200);

  // Sockets: a ticket only in the subprotocol; in the URL it is spent and refused.
  const ticket = (JSON.parse(minted.body) as { ticket: string }).ticket;
  expect(
    await upgrade(url, `/api/events?channel=audit&ticket=${ticket}`, {
      origin: url,
      protocols: [PROTOCOL],
    }),
  ).toBe("closed 4401");
  expect(
    await upgrade(url, "/api/events?channel=audit", {
      origin: url,
      protocols: [PROTOCOL, `indus-dashboard-ticket.${ticket}`],
    }),
  ).toBe("closed 4401");
  const fresh = JSON.parse((await mint(url)).body) as { ticket: string };
  const offer = [PROTOCOL, `indus-dashboard-ticket.${fresh.ticket}`];
  expect(
    await upgrade(url, "/api/events?channel=audit", {
      origin: "http://evil.example",
      protocols: offer,
    }),
  ).toBe("closed 4403");
  const last = JSON.parse((await mint(url)).body) as { ticket: string };
  expect(
    await upgrade(url, "/api/events?channel=audit", {
      origin: url,
      protocols: [PROTOCOL, `indus-dashboard-ticket.${last.ticket}`],
    }),
  ).toBe("open");

  // The browser itself never saw a credential in a URL (checked by the `urls` fixture).
  await page.goto(`${url}/sessions`);
  await expect(page.getByLabel("Username")).toBeVisible();
});
