// Auth against a real `indus dashboard` (P18-30): the loopback token, its rotation on restart,
// gated mode without a provider, and username/password login with single-use WS tickets.

import { expect, type Page, test } from "./fixtures.ts";

const SESSION_HEADER = "x-indus-session-token";

/**
 * Open a socket from the page with these subprotocols and report how it went: "open" when it
 * stayed open for 1.5 s (then closed by us), else "closed <code>" (a refusal is accepted and then
 * closed with its code, so a browser can read it).
 */
async function dial(page: Page, path: string, protocols: string[]): Promise<string> {
  return page.evaluate(
    async ({ path, protocols }: { path: string; protocols: string[] }) => {
      const url = `${location.origin.replace(/^http/, "ws")}${path}`;
      return new Promise<string>((done) => {
        let settled = false;
        const finish = (how: string) => {
          if (!settled) {
            settled = true;
            done(how);
          }
        };
        const socket = new WebSocket(url, protocols);
        const timer = setTimeout(() => finish("timeout"), 10_000);
        socket.onopen = () => {
          setTimeout(() => {
            clearTimeout(timer);
            finish("open");
            socket.close(1000);
          }, 1_500);
        };
        socket.onclose = (event) => {
          clearTimeout(timer);
          finish(`closed ${event.code}`);
        };
      });
    },
    { path, protocols },
  );
}

test("loopback: the token rides a header on every API call, never the URL", async ({
  launch,
  page,
}) => {
  const stack = await launch({ turns: [[{ text: "Hello." }]] });
  const calls: { url: string; header: string | null }[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/api/")) {
      calls.push({ url: request.url(), header: request.headers()[SESSION_HEADER] ?? null });
    }
  });
  await page.goto(`${stack.url}/sessions`);
  await expect(page.getByRole("textbox", { name: "Search sessions" })).toBeVisible();
  const token = await page.evaluate(() => window.__INDUS_SESSION_TOKEN__ ?? "");
  expect(token.length).toBeGreaterThan(20);
  expect(calls.length).toBeGreaterThan(0);
  for (const call of calls) expect(call.header, call.url).toBe(token);

  // The same call without the header is refused; with it, answered.
  const bare = await page.request.get(`${stack.url}/api/sessions`);
  expect(bare.status()).toBe(401);
  const good = await page.request.get(`${stack.url}/api/sessions`, {
    headers: { [SESSION_HEADER]: token },
  });
  expect(good.status()).toBe(200);
  // A token in the query is refused even beside a good header (no credential in a URL).
  const query = await page.request.get(`${stack.url}/api/sessions?token=${token}`, {
    headers: { [SESSION_HEADER]: token },
  });
  expect(query.status()).toBe(401);
});

test("a stale token after a dashboard restart reloads the page once and goes on", async ({
  launch,
  page,
}) => {
  const stack = await launch({ turns: [[{ text: "Hello." }]] });
  await page.goto(`${stack.url}/sessions`);
  await expect(page.getByRole("textbox", { name: "Search sessions" })).toBeVisible();
  const before = await page.evaluate(() => window.__INDUS_SESSION_TOKEN__);

  const restarted = await stack.ask("dashboard.restart");
  expect(restarted.url).toBe(stack.url);

  let loads = 0;
  page.on("load", () => {
    loads += 1;
  });
  // Any API call now carries the old token: one 401, one reload, then the new token works.
  await page.getByRole("link", { name: "Logs" }).click();
  await expect
    .poll(() => page.evaluate(() => window.__INDUS_SESSION_TOKEN__), { timeout: 30_000 })
    .not.toBe(before);
  await expect(page.getByRole("heading", { name: "Logs" })).toBeVisible();
  expect(loads).toBe(1);
  // The guard is cleared by the next good answer, so a later rotation may reload again.
  await expect
    .poll(() => page.evaluate(() => sessionStorage.getItem("indus.tokenReload")))
    .toBeNull();
});

test("a gated bind without a login provider refuses to start", async ({ launch }) => {
  const stack = await launch({ turns: [[{ text: "Hello." }]], dashboard: null });
  // 127.0.0.2 is outside the loopback set the gate trusts (still this machine's lo).
  const tried = await stack.ask("dashboard.try", { args: ["--host", "127.0.0.2", "--port", "0"] });
  expect(tried.code).toBe(1);
  expect(String(tried.output)).toMatch(/login|sign-in|provider/i);
  expect(String(tried.output)).not.toMatch(/Dashboard on/);
});

test("gated: login, the loopback token refused, single-use WS tickets, logout", async ({
  launch,
  page,
}) => {
  const password = ["e2e", "pass", String(Date.now())].join("-");
  const stack = await launch({
    turns: [[{ text: "Hello." }]],
    dashboard: { host: "127.0.0.2" },
    basicAuth: { username: "admin", password },
  });

  // An HTML load goes to the login page; no session token is handed to the page.
  await page.goto(`${stack.url}/sessions`);
  await expect(page.getByLabel("Username")).toBeVisible();
  const csp = (await page.request.get(page.url())).headers()["content-security-policy"] ?? "";
  expect(csp).toContain("default-src 'none'");

  // A wrong password is refused with our copy.
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill(`${password}-wrong`);
  await page.getByRole("button", { name: /Sign in/ }).click();
  await expect(page.getByText("That username and password did not match.")).toBeVisible();

  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: /Sign in/ }).click();
  await expect(page.getByRole("textbox", { name: "Search sessions" })).toBeVisible();
  expect(new URL(page.url()).pathname).toBe("/sessions");
  expect(await page.evaluate(() => window.__INDUS_SESSION_TOKEN__ ?? null)).toBeNull();
  expect(await page.evaluate(() => window.__INDUS_AUTH_REQUIRED__)).toBe(true);

  // A header token (what loopback mode accepts) opens nothing here without the cookie.
  const fake = ["x", "y".repeat(40)].join("");
  const refused = await page.request.fetch(`${stack.url}/api/sessions`, {
    headers: { [SESSION_HEADER]: fake, cookie: "" },
  });
  expect(refused.status()).toBe(401);
  // ...and a socket offering a token credential is refused (4401).
  expect(
    await dial(page, "/api/events?channel=e2e", [
      "indus-dashboard-v1",
      `indus-dashboard-token.${fake}`,
    ]),
  ).toBe("closed 4401");

  // A ticket opens one socket, once.
  const ticket = await page.evaluate(async () => {
    const res = await fetch("/api/auth/ws-ticket", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ purpose: "web-events" }),
    });
    return ((await res.json()) as { ticket: string }).ticket;
  });
  const offer = ["indus-dashboard-v1", `indus-dashboard-ticket.${ticket}`];
  expect(await dial(page, "/api/events?channel=e2e", offer)).toBe("open");
  expect(await dial(page, "/api/events?channel=e2e", offer)).toBe("closed 4401");

  // Logout clears the session: the next load is the login page again.
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByLabel("Username")).toBeVisible();
  await page.goto(`${stack.url}/sessions`);
  await expect(page.getByLabel("Username")).toBeVisible();
});
