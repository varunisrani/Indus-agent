// @vitest-environment node
// P18-29: the dev bootstrap plugin, the `/api` proxy (a real Vite dev server in front of a tiny
// stand-in backend that speaks just enough HTTP and WebSocket), chunk naming and the size budget.

import { createHash } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { createServer as createHttpServer, type IncomingMessage, type Server } from "node:http";
import type { AddressInfo, Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer as createViteServer, type ViteDevServer } from "vite";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { CHUNK_LIMIT_KB, chunkBudget, chunkFor, oversized, strictBuild } from "./vite.chunks.ts";
import {
  apiProxy,
  BACKEND_ENV,
  backendUrl,
  bootstrapScript,
  DEFAULT_BACKEND,
  devBootstrap,
  loadBootstrapTag,
  parseBootstrap,
} from "./vite.dev-bootstrap.ts";

// Built at runtime so no credential-shaped literal sits in the source.
const fakeToken = () => ["dev", "token", "for", "tests", "0123456789"].join("-");

function backendIndex(token: string, profile = "work"): string {
  const js = (value: unknown) => JSON.stringify(value).replace(/</g, "\\u003c");
  return (
    "<!doctype html><html><head><title>x</title>" +
    "<script>" +
    `window.__INDUS_SESSION_TOKEN__=${js(token)};` +
    `window.__INDUS_BASE_PATH__=${js("")};` +
    "window.__INDUS_AUTH_REQUIRED__=false;" +
    "window.__INDUS_DASHBOARD_EMBEDDED_CHAT__=true;" +
    `window.__INDUS_INITIAL_PROFILE__=${js(profile)};` +
    `window.__INDUS_DASHBOARD_PROFILE__=${js("default")};` +
    "</script></head><body></body></html>"
  );
}

describe("bootstrap parsing", () => {
  it("reads the known values and drops everything else", () => {
    const html =
      backendIndex(fakeToken()) +
      '<script>window.__INDUS_EVIL__="x";window.__INDUS_BASE_PATH__=1;</script>';
    const values = parseBootstrap(html);
    expect(values).toEqual({
      __INDUS_SESSION_TOKEN__: fakeToken(),
      __INDUS_BASE_PATH__: "",
      __INDUS_AUTH_REQUIRED__: false,
      __INDUS_DASHBOARD_EMBEDDED_CHAT__: true,
      __INDUS_INITIAL_PROFILE__: "work",
      __INDUS_DASHBOARD_PROFILE__: "default",
    });
  });

  it("re-encodes values so none can close the script", () => {
    const values = parseBootstrap(backendIndex(fakeToken(), "</script><b>"));
    expect(values.__INDUS_INITIAL_PROFILE__).toBe("</script><b>");
    const script = bootstrapScript(values);
    expect(script).not.toContain("</script>");
    expect(script).toContain(
      'window.__INDUS_INITIAL_PROFILE__="\\u003c/script\\u003e\\u003cb\\u003e";',
    );
    expect(script.indexOf("__INDUS_SESSION_TOKEN__")).toBeLessThan(
      script.indexOf("__INDUS_DASHBOARD_PROFILE__"),
    );
  });

  it("finds nothing in a page without our script", () => {
    expect(parseBootstrap("<html><body>login</body></html>")).toEqual({});
    expect(parseBootstrap('window.__INDUS_SESSION_TOKEN__="unterminated')).toEqual({});
  });

  it("takes the backend from the environment only when it is an http(s) URL", () => {
    expect(backendUrl({})).toBe(DEFAULT_BACKEND);
    expect(backendUrl({ [BACKEND_ENV]: "http://127.0.0.1:9200/" })).toBe("http://127.0.0.1:9200");
    expect(backendUrl({ [BACKEND_ENV]: "file:///etc/passwd" })).toBe(DEFAULT_BACKEND);
    expect(backendUrl({ [BACKEND_ENV]: "not a url" })).toBe(DEFAULT_BACKEND);
  });

  it("proxies /api with WebSocket upgrades and keeps the Host", () => {
    expect(apiProxy("http://127.0.0.1:9119")).toEqual({
      "/api": { target: "http://127.0.0.1:9119", changeOrigin: false, ws: true },
    });
  });
});

describe("loading the tag", () => {
  const backend = "http://127.0.0.1:1";

  it("injects the values from a good page", async () => {
    const warn = vi.fn();
    const get = vi.fn(async () => new Response(backendIndex(fakeToken()), { status: 200 }));
    const tag = await loadBootstrapTag({ backend, warn, fetch: get as unknown as typeof fetch });
    expect(tag?.tag).toBe("script");
    expect(tag?.injectTo).toBe("head-prepend");
    expect(String(tag?.children)).toContain(`window.__INDUS_SESSION_TOKEN__="${fakeToken()}";`);
    expect(warn).not.toHaveBeenCalled();
    const [, init] = get.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.redirect).toBe("manual");
  });

  it("warns without the token when the backend is down, gated or not the dashboard", async () => {
    const replies: Array<() => Promise<Response>> = [
      async () => {
        throw new TypeError("connect ECONNREFUSED");
      },
      async () => new Response(null, { status: 302, headers: { location: "/login" } }),
      async () => new Response("<html>something else</html>", { status: 200 }),
    ];
    for (const reply of replies) {
      const warn = vi.fn();
      const tag = await loadBootstrapTag({
        backend,
        warn,
        fetch: reply as unknown as typeof fetch,
      });
      expect(tag).toBeUndefined();
      expect(warn).toHaveBeenCalledTimes(1);
      expect(String(warn.mock.calls[0]?.[0])).toContain(backend);
      expect(String(warn.mock.calls[0]?.[0])).not.toContain(fakeToken());
    }
  });

  it("only runs on the dev server", () => {
    expect(devBootstrap().apply).toBe("serve");
    expect(chunkBudget().apply).toBe("build");
  });
});

// -- a real dev server in front of a stand-in backend -------------------------------------------

const WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

function textFrame(text: string): Buffer {
  const payload = Buffer.from(text, "utf8");
  if (payload.length < 126) return Buffer.concat([Buffer.from([0x81, payload.length]), payload]);
  const head = Buffer.alloc(4);
  head[0] = 0x81;
  head[1] = 126;
  head.writeUInt16BE(payload.length, 2);
  return Buffer.concat([head, payload]);
}

interface Seen {
  path: string;
  host: string;
  protocols: string;
}

let backend: Server;
let vite: ViteDevServer;
let root: string;
let viteUrl: string;
const sockets = new Set<Socket>();
const seen: Seen[] = [];

beforeAll(async () => {
  backend = createHttpServer((req, res) => {
    if (req.url === "/") {
      res.writeHead(200, { "content-type": "text/html" });
      res.end(backendIndex(fakeToken()));
      return;
    }
    if (req.url?.startsWith("/api/ping")) {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ pong: true, host: req.headers.host }));
      return;
    }
    res.writeHead(404);
    res.end();
  });
  backend.on("connection", (socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
  });
  backend.on("upgrade", (req: IncomingMessage, socket: Socket) => {
    socket.on("error", () => {});
    const key = String(req.headers["sec-websocket-key"] ?? "");
    const accept = createHash("sha1")
      .update(key + WS_GUID)
      .digest("base64");
    const record: Seen = {
      path: req.url ?? "",
      host: String(req.headers.host ?? ""),
      protocols: String(req.headers["sec-websocket-protocol"] ?? ""),
    };
    seen.push(record);
    socket.write(
      "HTTP/1.1 101 Switching Protocols\r\n" +
        "Upgrade: websocket\r\nConnection: Upgrade\r\n" +
        `Sec-WebSocket-Accept: ${accept}\r\n` +
        "Sec-WebSocket-Protocol: indus-dashboard-v1\r\n\r\n",
    );
    socket.write(textFrame(JSON.stringify(record)));
  });
  await new Promise<void>((resolve) => backend.listen(0, "127.0.0.1", resolve));
  const target = `http://127.0.0.1:${(backend.address() as AddressInfo).port}`;

  root = mkdtempSync(join(tmpdir(), "indus-web-dev-"));
  writeFileSync(
    join(root, "index.html"),
    "<!doctype html><html><head><title>dev</title></head><body></body></html>",
  );
  vite = await createViteServer({
    configFile: false,
    root,
    cacheDir: join(root, ".vite"),
    logLevel: "silent",
    optimizeDeps: { noDiscovery: true, include: [] },
    plugins: [devBootstrap({ backend: target })],
    server: { host: "127.0.0.1", port: 0, strictPort: false, hmr: false, proxy: apiProxy(target) },
  });
  await vite.listen();
  const address = vite.httpServer?.address() as AddressInfo | null | undefined;
  if (!address) throw new Error("the dev server did not listen");
  viteUrl = `http://127.0.0.1:${address.port}`;
}, 30_000);

afterAll(async () => {
  await vite?.close();
  for (const socket of sockets) socket.destroy();
  await new Promise<void>((resolve) => backend?.close(() => resolve()));
  if (root) rmSync(root, { recursive: true, force: true });
});

describe("the dev server", () => {
  it("injects the backend's bootstrap into its own index.html", async () => {
    const page = await (await fetch(`${viteUrl}/`)).text();
    expect(page).toContain(`window.__INDUS_SESSION_TOKEN__="${fakeToken()}";`);
    expect(page).toContain('window.__INDUS_INITIAL_PROFILE__="work";');
    expect(page.indexOf("__INDUS_SESSION_TOKEN__")).toBeLessThan(page.indexOf("<title>"));
  });

  it("forwards /api requests and keeps the dev server's Host", async () => {
    const answer = (await (await fetch(`${viteUrl}/api/ping?x=1`)).json()) as {
      pong: boolean;
      host: string;
    };
    expect(answer.pong).toBe(true);
    expect(answer.host).toBe(new URL(viteUrl).host);
  });

  it("forwards a WebSocket upgrade with the subprotocol credential intact", async () => {
    const offered = ["indus-dashboard-v1", `indus-dashboard-token.${fakeToken()}`];
    const socket = new WebSocket(`${viteUrl.replace("http", "ws")}/api/pty?channel=c1`, offered);
    const message = await new Promise<string>((resolve, reject) => {
      socket.addEventListener("message", (event) => resolve(String(event.data)));
      socket.addEventListener("error", () => reject(new Error("socket error")));
      setTimeout(() => reject(new Error("no message within 10 s")), 10_000);
    });
    expect(socket.protocol).toBe("indus-dashboard-v1");
    socket.close();
    const record = JSON.parse(message) as Seen;
    expect(record.path).toBe("/api/pty?channel=c1");
    expect(record.protocols.split(",").map((p) => p.trim())).toEqual(offered);
    expect(record.path).not.toContain(fakeToken());
    expect(record.host).toBe(new URL(viteUrl).host);
  }, 15_000);
});

describe("chunks", () => {
  it("names the vendor chunks", () => {
    expect(chunkFor("/repo/node_modules/.pnpm/react-dom@19/node_modules/react-dom/index.js")).toBe(
      "react",
    );
    expect(chunkFor("/repo/node_modules/react-router/dist/index.js")).toBe("react");
    expect(chunkFor("/repo/node_modules/@xterm/xterm/lib/xterm.js")).toBe("xterm");
    expect(chunkFor("/repo/node_modules/@tanstack/react-query/build/index.js")).toBe("vendor");
    expect(chunkFor("/repo/apps/web/src/App.tsx")).toBeUndefined();
  });

  it("reports chunks over the budget, largest first", () => {
    const big = "x".repeat(CHUNK_LIMIT_KB * 1000 + 1);
    const bundle = {
      "a.js": { type: "chunk", fileName: "assets/a.js", code: big },
      "b.js": { type: "chunk", fileName: "assets/b.js", code: `${big}${big}` },
      "c.js": { type: "chunk", fileName: "assets/c.js", code: "small" },
      "d.css": { type: "asset", fileName: "assets/d.css" },
    };
    expect(oversized(bundle).map((c) => c.file)).toEqual(["assets/b.js", "assets/a.js"]);
    expect(oversized({ "c.js": bundle["c.js"] })).toEqual([]);
  });

  it("fails a CI build and only warns elsewhere", () => {
    expect(strictBuild({ CI: "true" })).toBe(true);
    expect(strictBuild({ CI: "1" })).toBe(true);
    expect(strictBuild({ CI: "false" })).toBe(false);
    expect(strictBuild({})).toBe(false);
    const bundle = { "a.js": { type: "chunk", fileName: "a.js", code: "x".repeat(2001) } };
    for (const strict of [true, false]) {
      const plugin = chunkBudget(2, strict);
      const context = { error: vi.fn(), warn: vi.fn() };
      const hook = plugin.generateBundle as unknown as (
        this: typeof context,
        options: unknown,
        bundle: unknown,
      ) => void;
      hook.call(context, {}, bundle);
      expect(context.error).toHaveBeenCalledTimes(strict ? 1 : 0);
      expect(context.warn).toHaveBeenCalledTimes(strict ? 0 : 1);
    }
  });
});
