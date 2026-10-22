// P25-03 desktop security audit (D-16.18, D-16.19, D-16.20; R08.4, R08.8): one file that pins every
// desktop trust boundary against the real modules, so a change that weakens one fails here by name.
// The real-window half is `e2e/security-audit.spec.ts` (Electron lane).
//
//   ipc          every channel of the table, from every kind of foreign frame, is refused before
//                its handler runs; nothing listens on an event channel;
//   schemas      every request is a strict object (no unknown key passes), with open values only
//                where reviewed below;
//   csp          the production policy keeps scripts to the app and checked plugin modules; every
//                renderer page gets exactly one policy meta first in <head>; main's header replaces
//                any page-sent policy on app URLs;
//   navigation   app windows never leave the app; window.open is denied everywhere; a webview
//                attaches only on the preview partition, with our preload and hardened settings;
//   permissions  only the app's main frame, a short list, microphone-only media;
//   external     only http/https without credentials, and only after a real input, once;
//   media        `indus-media:` serves media files inside the roots only (links, traversal, NUL,
//                sensitive names, other methods refused);
//   deep links   foreign schemes and malformed links dropped; a link never starts a program and
//                skills / servers always wait for the person.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { ResolvedConfig } from "vite";
import { afterAll, describe, expect, it, vi } from "vitest";
import type { z } from "zod";
import { planLink } from "../../src/deepLinks/deepLinkActions.ts";
import { cspMetaPlugin, rendererMetaCsp } from "../../vite.config.ts";
import { fileAppOrigin } from "../appOrigin.ts";
import { parseDeepLink } from "../deepLinks.ts";
import { CHANNELS } from "../ipc/channels.ts";
import { SENDER_REJECTED_MESSAGE } from "../ipc/envelope.ts";
import { type IpcMainLike, IpcRegistry, type SyncEventLike } from "../ipc/registry.ts";
import { SCHEMAS } from "../ipc/schemas.ts";
import type { FrameLike, IpcEventLike } from "../ipc/senderCheck.ts";
import { createMediaHandler, MediaRoots, mediaUrl } from "../mediaProtocol.ts";
import { buildCsp, CspOriginError, CspState, withCspHeader } from "./csp.ts";
import { ExternalOpener, externalUrlProblem, UserActivationTracker } from "./externalOpen.ts";
import {
  type GuardedContents,
  hardenWebContents,
  hardenWebviewAttach,
  PREVIEW_PARTITION,
  type PreventableEvent,
} from "./navigation.ts";
import { permissionAllowed } from "./permissions.ts";

const APP_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const INDEX = "/opt/Indus/app/dist/index.html";
const APP_URL = `file://${INDEX}#/`;
const ORIGIN = fileAppOrigin(INDEX, "linux");
const KNOWN = 7;

// -- ipc --------------------------------------------------------------------------------------------

type Listener = (event: IpcEventLike, ...args: unknown[]) => unknown;

function frame(url: string, id: number): FrameLike {
  return { url, processId: 3, routingId: id, frameTreeNodeId: id };
}

/** Every way a frame that is not the app's own main frame can reach main. */
const FOREIGN: Record<string, () => IpcEventLike> = {
  "preview guest": () => {
    const main = frame("https://evil.example/", 1);
    return { sender: { id: 40, mainFrame: main, getType: () => "webview" }, senderFrame: main };
  },
  "guest showing an app URL": () => {
    const main = frame(APP_URL, 1);
    return { sender: { id: KNOWN, mainFrame: main, getType: () => "webview" }, senderFrame: main };
  },
  "iframe in the app page": () => ({
    sender: { id: KNOWN, mainFrame: frame(APP_URL, 1), getType: () => "window" },
    senderFrame: frame(APP_URL, 2),
  }),
  "foreign iframe in the app page": () => ({
    sender: { id: KNOWN, mainFrame: frame(APP_URL, 1), getType: () => "window" },
    senderFrame: frame("https://evil.example/", 3),
  }),
  "window navigated to another file": () => {
    const main = frame("file:///tmp/evil.html", 1);
    return { sender: { id: KNOWN, mainFrame: main, getType: () => "window" }, senderFrame: main };
  },
  "window main never registered": () => {
    const main = frame(APP_URL, 1);
    return { sender: { id: 9999, mainFrame: main, getType: () => "window" }, senderFrame: main };
  },
  "devtools page": () => {
    const main = frame("devtools://devtools/bundled/inspector.html", 1);
    return { sender: { id: KNOWN, mainFrame: main, getType: () => "window" }, senderFrame: main };
  },
  "destroyed sender": () => {
    const main = frame(APP_URL, 1);
    return {
      sender: { id: KNOWN, mainFrame: main, isDestroyed: () => true, getType: () => "window" },
      senderFrame: main,
    };
  },
  "no frame": () => ({
    sender: { id: KNOWN, mainFrame: frame(APP_URL, 1), getType: () => "window" },
    senderFrame: null,
  }),
};

function wiredRegistry(wire = true) {
  const handles = new Map<string, Listener>();
  const ons = new Map<string, Listener>();
  const ipc: IpcMainLike = {
    handle: (channel, listener) => void handles.set(channel, listener as Listener),
    on: (channel, listener) => {
      ons.set(channel, listener as Listener);
      return ipc;
    },
  };
  const registry = new IpcRegistry({
    ipc,
    policy: { appOrigin: ORIGIN, isKnownWebContents: (id) => id === KNOWN },
    log: () => {},
  });
  const ran = vi.fn();
  for (const def of wire ? CHANNELS : []) {
    const schemas = SCHEMAS[def.name] as { request?: z.ZodType };
    if (def.kind === "invoke" || def.kind === "send") {
      registry.registerIpc(def.name, schemas.request as z.ZodType, (payload) =>
        ran(def.name, payload),
      );
    } else if (def.kind === "sync") {
      registry.sync(def.name as Parameters<IpcRegistry["sync"]>[0], () => {
        ran(def.name);
        return {} as never;
      });
    }
  }
  registry.seal();
  return { handles, ons, ran };
}

describe("ipc: every channel refuses every foreign frame (D-16.18)", () => {
  const { handles, ons, ran } = wiredRegistry();

  it("covers the whole table", () => {
    expect(CHANNELS.length).toBeGreaterThanOrEqual(100);
    for (const def of CHANNELS) {
      if (def.kind === "event") continue;
      const listener = def.kind === "invoke" ? handles.get(def.name) : ons.get(def.name);
      expect(listener, def.name).toBeTypeOf("function");
    }
  });

  for (const [label, make] of Object.entries(FOREIGN)) {
    it(`${label}: refused on all channels, no handler runs`, async () => {
      for (const def of CHANNELS) {
        if (def.kind === "invoke") {
          const answer = await (handles.get(def.name) as Listener)(make(), {});
          expect(answer, def.name).toEqual({
            ok: false,
            code: "sender-rejected",
            message: SENDER_REJECTED_MESSAGE,
          });
        } else if (def.kind === "send") {
          (ons.get(def.name) as Listener)(make(), {});
        } else if (def.kind === "sync") {
          const event = { ...make(), returnValue: undefined } as SyncEventLike;
          (ons.get(def.name) as Listener)(event);
          expect(event.returnValue, def.name).toMatchObject({ ok: false, code: "sender-rejected" });
        }
      }
      expect(ran).not.toHaveBeenCalled();
    });
  }

  it("nothing in main listens on an event channel (main → renderer only)", () => {
    for (const def of CHANNELS.filter((c) => c.kind === "event")) {
      expect(handles.has(def.name) || ons.has(def.name), def.name).toBe(false);
    }
  });
});

describe("ipc: channels left to seal() refuse every foreign frame too (SEC-18)", () => {
  const { handles, ons } = wiredRegistry(false);

  for (const [label, make] of Object.entries(FOREIGN)) {
    it(`${label}: refused before not-available on every unwired channel`, async () => {
      for (const def of CHANNELS) {
        if (def.kind === "invoke") {
          const answer = await (handles.get(def.name) as Listener)(make(), {});
          expect(answer, def.name).toMatchObject({ ok: false, code: "sender-rejected" });
        } else if (def.kind === "sync") {
          const event = { ...make(), returnValue: undefined } as SyncEventLike;
          (ons.get(def.name) as Listener)(event);
          expect(event.returnValue, def.name).toEqual({
            ok: false,
            code: "sender-rejected",
            message: SENDER_REJECTED_MESSAGE,
          });
        }
      }
    });
  }

  it("the app's own frame still hears not-available on an unwired sync channel", () => {
    const sync = CHANNELS.filter((c) => c.kind === "sync");
    expect(sync.length).toBeGreaterThan(0);
    for (const def of sync) {
      const main = frame(APP_URL, 1);
      const event = {
        sender: { id: KNOWN, mainFrame: main, getType: () => "window" },
        senderFrame: main,
        returnValue: undefined,
      } as SyncEventLike;
      (ons.get(def.name) as Listener)(event);
      expect(event.returnValue, def.name).toMatchObject({ ok: false, code: "not-available" });
    }
  });
});

// -- schemas ----------------------------------------------------------------------------------------

/** The parts of zod 4's schema definitions the walk reads. */
interface Def {
  type: string;
  shape?: Record<string, Walkable>;
  catchall?: Walkable;
  options?: Walkable[];
  items?: Walkable[];
  innerType?: Walkable;
  element?: Walkable;
  in?: Walkable;
  out?: Walkable;
  keyType?: Walkable;
  valueType?: Walkable;
  left?: Walkable;
  right?: Walkable;
}
interface Walkable {
  _zod?: { def?: Def };
}

/**
 * Request values left open on purpose (each reviewed):
 * - `indus:api` body: the JSON body of a REST call main forwards to the local serve, which
 *   validates it itself; main adds the credential, the renderer never holds it;
 * - `indus:journal:prepare` payload: the submission the journal stores verbatim so an
 *   unknown-outcome turn can be shown again (never executed, size-capped by the journal).
 */
const OPEN_ON_PURPOSE = new Set(["indus:api.body", "indus:journal:prepare.payload"]);

function looseParts(schema: Walkable, where: string, found: string[], seen = new Set<Def>()): void {
  const def = schema._zod?.def;
  if (def === undefined) {
    found.push(`${where}: not a zod schema`);
    return;
  }
  if (seen.has(def)) return;
  seen.add(def);
  const bare = where.replace(/<[^>]*>/g, "");
  if ((def.type === "any" || def.type === "unknown") && !OPEN_ON_PURPOSE.has(bare)) {
    found.push(`${where}: ${def.type}`);
  }
  if (def.type === "object") {
    if (def.catchall?._zod?.def?.type !== "never") found.push(`${where}: object is not strict`);
    for (const [key, value] of Object.entries(def.shape ?? {}))
      looseParts(value, `${where}.${key}`, found, seen);
  }
  for (const key of [
    "innerType",
    "element",
    "in",
    "out",
    "keyType",
    "valueType",
    "left",
    "right",
  ] as const) {
    const inner = def[key];
    if (inner !== undefined) looseParts(inner, `${where}<${key}>`, found, seen);
  }
  for (const option of def.options ?? []) looseParts(option, where, found, seen);
  for (const item of def.items ?? []) looseParts(item, where, found, seen);
}

describe("schemas: every request is strict", () => {
  it("no request object takes unknown keys; open values only where reviewed", () => {
    const found: string[] = [];
    let checked = 0;
    for (const def of CHANNELS) {
      if (def.kind !== "invoke" && def.kind !== "send") continue;
      const request = (SCHEMAS[def.name] as { request: Walkable }).request;
      looseParts(request, def.name, found);
      checked += 1;
    }
    expect(checked).toBeGreaterThan(80);
    expect(found).toEqual([]);
  });

  it("an extra key on a no-payload channel is a malformed payload", () => {
    const none = (SCHEMAS["indus:boot:get"] as { request: z.ZodType }).request;
    expect(none.safeParse(undefined).success).toBe(true);
    expect(none.safeParse({ extra: 1 }).success).toBe(false);
  });
});

// -- csp --------------------------------------------------------------------------------------------

function directives(policy: string): Map<string, string[]> {
  const map = new Map<string, string[]>();
  for (const part of policy.split(";")) {
    const [name, ...sources] = part.trim().split(/\s+/);
    if (name) map.set(name, sources);
  }
  return map;
}

function assertLocked(policy: string): void {
  const d = directives(policy);
  expect(d.get("default-src")).toEqual(["'self'"]);
  const scripts = d.get("script-src") ?? [];
  expect(scripts.slice(0, 2)).toEqual(["'self'", "indus-plugin:"]);
  for (const bad of [
    "'unsafe-inline'",
    "'unsafe-eval'",
    "'wasm-unsafe-eval'",
    "data:",
    "blob:",
    "*",
  ])
    expect(scripts, bad).not.toContain(bad);
  expect(d.get("object-src")).toEqual(["'none'"]);
  expect(d.get("base-uri")).toEqual(["'none'"]);
  expect(d.get("form-action")).toEqual(["'none'"]);
  expect(d.get("frame-src")).toEqual(["'none'"]);
  const connect = d.get("connect-src") ?? [];
  expect(connect).not.toContain("*");
  expect(connect).not.toContain("http:");
  expect(connect).not.toContain("ws:");
}

describe("csp (D-16.19)", () => {
  it("the production meta and header policies keep scripts to the app", () => {
    assertLocked(rendererMetaCsp({ dev: false }));
    assertLocked(buildCsp({ dev: false }));
    const live = new CspState({ dev: false });
    live.addConnectOrigin("http://127.0.0.1:43123");
    live.addConnectOrigin("ws://127.0.0.1:43124");
    assertLocked(live.current());
    expect(directives(live.current()).get("connect-src")).toContain("ws://127.0.0.1:43124");
  });

  it("a dev build adds only its own loopback server to scripts", () => {
    const dev = directives(buildCsp({ dev: true, devServerOrigin: "http://127.0.0.1:5174" }));
    expect(dev.get("script-src")).toEqual(["'self'", "indus-plugin:", "http://127.0.0.1:5174"]);
  });

  it.each([
    "http://127.0.0.1:1/path",
    "http://*.example",
    "http://user:pw@127.0.0.1:1",
    "javascript:alert(1)",
    "file:///etc",
    "http://127.0.0.1:1/?q=1",
  ])("a connect origin with more than an origin is refused: %s", (raw) => {
    expect(() => new CspState({ dev: false }).addConnectOrigin(raw)).toThrow(CspOriginError);
  });

  it("every renderer page gets exactly one policy, first in <head>, and has no inline script", () => {
    const pages: string[] = [];
    const walk = (dir: string) => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        if (
          ["node_modules", "dist", "dist-electron", "build", "testing", "e2e", "release"].includes(
            entry.name,
          )
        )
          continue;
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) walk(full);
        else if (entry.name.endsWith(".html")) pages.push(path.relative(APP_ROOT, full));
      }
    };
    walk(APP_ROOT);
    // A new renderer page must be added to the build's CSP plugin coverage (and here) on purpose.
    expect(pages).toEqual(["index.html"]);
    const plugin = cspMetaPlugin();
    (plugin.configResolved as (config: ResolvedConfig) => void)({
      command: "build",
    } as ResolvedConfig);
    const hook = plugin.transformIndexHtml as { handler: (html: string) => string };
    for (const page of pages) {
      const source = fs.readFileSync(path.join(APP_ROOT, page), "utf8");
      for (const script of source.matchAll(/<script\b([^>]*)>/gi)) {
        expect(script[1], `${page}: inline script`).toMatch(/\bsrc=/);
      }
      expect(source, `${page}: inline handler`).not.toMatch(/\son[a-z]+\s*=/i);
      const built = hook.handler(source);
      const metas = built.match(/<meta\s+http-equiv="Content-Security-Policy"/gi) ?? [];
      expect(metas.length, page).toBe(1);
      const head = built.slice(built.search(/<head[^>]*>/i));
      expect(head.replace(/<head[^>]*>\s*/i, "")).toMatch(
        /^<meta http-equiv="Content-Security-Policy"/,
      );
      const policy = /content="([^"]*)"/.exec(head)?.[1]?.replace(/&#39;|&apos;/g, "'") ?? "";
      assertLocked(policy);
    }
  });

  it("main's header replaces any policy an app page sent; other URLs are untouched", () => {
    const policy = buildCsp({ dev: false });
    const headers = withCspHeader(
      {
        url: APP_URL,
        responseHeaders: {
          "content-security-policy": ["script-src *"],
          "Content-Security-Policy-Report-Only": ["default-src *"],
          "x-other": ["kept"],
        },
      },
      policy,
      ORIGIN,
    );
    expect(headers).toEqual({ "x-other": ["kept"], "Content-Security-Policy": [policy] });
    const foreign = { "content-security-policy": ["theirs"] };
    expect(
      withCspHeader({ url: "https://example.com/", responseHeaders: foreign }, policy, ORIGIN),
    ).toBe(foreign);
  });
});

// -- navigation and popups --------------------------------------------------------------------------

function fakeContents(type: "window" | "webview") {
  const listeners = new Map<string, (...args: unknown[]) => void>();
  let openHandler: ((details: { url: string }) => { action: "deny" }) | null = null;
  const contents: GuardedContents = {
    id: type === "window" ? KNOWN : 40,
    getType: () => type,
    on: (event, listener) => {
      listeners.set(event, listener as (...args: unknown[]) => void);
      return contents;
    },
    setWindowOpenHandler: (handler) => {
      openHandler = handler;
    },
  };
  return {
    contents,
    emit: (event: string, ...args: unknown[]) => listeners.get(event)?.(...args),
    has: (event: string) => listeners.has(event),
    open: (url: string) => openHandler?.({ url }),
  };
}

function navigationEvent(url: string, isMainFrame = true) {
  const event: PreventableEvent & { prevented: boolean } = {
    url,
    isMainFrame,
    prevented: false,
    preventDefault() {
      this.prevented = true;
    },
  };
  return event;
}

const GUARD = {
  appOrigin: ORIGIN,
  guestPreloadPath: "/opt/Indus/app/preview-guest.cjs",
  log: () => {},
};

describe("navigation and popups (D-16.20, R08.8)", () => {
  it.each([
    "https://evil.example/",
    "http://127.0.0.1:9119/",
    "file:///etc/passwd",
    "file:///opt/Indus/app/dist/other.html",
    "file:///opt/Indus/app/dist/index.html/../../../../etc/passwd",
    "javascript:alert(1)",
    "data:text/html,<script>1</script>",
    "indus-media://stream/%2Fetc%2Fpasswd",
    "about:blank",
    "devtools://devtools/bundled/inspector.html",
    "not a url",
  ])("an app window never leaves the app: %s", (url) => {
    const fake = fakeContents("window");
    hardenWebContents(fake.contents, GUARD);
    for (const name of ["will-navigate", "will-redirect"]) {
      const event = navigationEvent(url);
      fake.emit(name, event, url, false, true);
      expect(event.prevented, `${name} ${url}`).toBe(true);
    }
  });

  it("the app's own page (any route) may load", () => {
    const fake = fakeContents("window");
    hardenWebContents(fake.contents, GUARD);
    for (const url of [APP_URL, `file://${INDEX}?window=hud#/settings`]) {
      const event = navigationEvent(url);
      fake.emit("will-navigate", event, url, false, true);
      expect(event.prevented, url).toBe(false);
    }
  });

  it("window.open is denied for app windows and guests, whatever the URL, even if logging throws", () => {
    for (const type of ["window", "webview"] as const) {
      const fake = fakeContents(type);
      hardenWebContents(fake.contents, {
        ...GUARD,
        log: () => {
          throw new Error("logger down");
        },
      });
      for (const url of [
        APP_URL,
        "https://example.com/",
        "javascript:1",
        "file:///etc/hostname",
        "",
      ]) {
        expect(fake.open(url), `${type} ${url}`).toEqual({ action: "deny" });
      }
    }
  });

  it("a webview attaches only on the preview partition, hardened, with our preload and no popups", () => {
    const refusedPartitions = ["", "persist:other", "persist:indus-preview ", undefined];
    for (const partition of refusedPartitions) {
      const event = navigationEvent("");
      const ok = hardenWebviewAttach(event, {}, { partition }, GUARD);
      expect(ok, String(partition)).toBe(false);
      expect(event.prevented).toBe(true);
    }
    const noPreload = navigationEvent("");
    expect(
      hardenWebviewAttach(
        noPreload,
        {},
        { partition: PREVIEW_PARTITION },
        { ...GUARD, guestPreloadPath: null },
      ),
    ).toBe(false);
    const prefs: Record<string, unknown> = {
      preload: "/tmp/evil.js",
      preloadURL: "file:///tmp/evil.js",
      nodeIntegration: true,
      nodeIntegrationInSubFrames: true,
      contextIsolation: false,
      sandbox: false,
      webSecurity: false,
      allowRunningInsecureContent: true,
    };
    const params: Record<string, unknown> = { partition: PREVIEW_PARTITION, allowpopups: "true" };
    const event = navigationEvent("");
    expect(hardenWebviewAttach(event, prefs, params, GUARD)).toBe(true);
    expect(prefs).toEqual({
      preload: GUARD.guestPreloadPath,
      nodeIntegration: false,
      nodeIntegrationInSubFrames: false,
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
    });
    expect(params).not.toHaveProperty("allowpopups");
  });

  it("every web contents (guests included) gets the attach guard", () => {
    for (const type of ["window", "webview"] as const) {
      const fake = fakeContents(type);
      hardenWebContents(fake.contents, GUARD);
      expect(fake.has("will-attach-webview"), type).toBe(true);
    }
  });
});

// -- permissions ------------------------------------------------------------------------------------

describe("permissions", () => {
  const app = { isMainFrame: true };
  it.each([
    ["geolocation", APP_URL, app],
    ["clipboard-read", APP_URL, app],
    ["openExternal", APP_URL, app],
    ["hid", APP_URL, app],
    ["serial", APP_URL, app],
    ["usb", APP_URL, app],
    ["midi", APP_URL, app],
    ["pointerLock", APP_URL, app],
    ["display-capture", APP_URL, app],
    ["media", APP_URL, { isMainFrame: true, mediaTypes: ["video"] }],
    ["media", APP_URL, { isMainFrame: true, mediaTypes: ["audio", "video"] }],
    ["media", APP_URL, { isMainFrame: true, mediaTypes: [] }],
    ["notifications", "https://evil.example/", app],
    ["media", "https://evil.example/", { isMainFrame: true, mediaTypes: ["audio"] }],
    ["notifications", APP_URL, { isMainFrame: false }],
    ["fullscreen", "file:///tmp/evil.html", app],
  ])("%s for %s is refused", (permission, url, details) => {
    expect(permissionAllowed(permission, url, details, ORIGIN)).toBe(false);
  });

  it("the app's main frame keeps its short list (microphone only for media)", () => {
    for (const permission of ["clipboard-sanitized-write", "notifications", "fullscreen"])
      expect(permissionAllowed(permission, APP_URL, app, ORIGIN), permission).toBe(true);
    expect(
      permissionAllowed("media", APP_URL, { isMainFrame: true, mediaTypes: ["audio"] }, ORIGIN),
    ).toBe(true);
  });
});

// -- external opener --------------------------------------------------------------------------------

describe("external opener", () => {
  it.each([
    ["file:///etc/passwd", "scheme"],
    ["javascript:alert(1)", "scheme"],
    ["data:text/html,x", "scheme"],
    ["indus://open/settings", "scheme"],
    ["mailto:someone@example.com", "scheme"],
    ["smb://host/share", "scheme"],
    ["https://user:pw@example.com/", "credentials"],
    [`https://example.com/${"a".repeat(9000)}`, "too-long"],
    ["", "invalid"],
    ["https://", "invalid"],
  ])("%s → %s", (url, reason) => {
    expect(externalUrlProblem(url)).toBe(reason);
  });

  it("opens only after a real input in that contents, once", async () => {
    let now = 1_000;
    const activation = new UserActivationTracker(() => now);
    const opened: string[] = [];
    const opener = new ExternalOpener({
      openExternal: async (url) => void opened.push(url),
      activation,
      log: () => {},
    });
    const url = "https://example.com/docs";
    expect(await opener.open({ url, contentsId: 3 })).toEqual({
      ok: false,
      reason: "no-user-activation",
    });
    activation.noteInput(3, "mouseMove");
    expect((await opener.open({ url, contentsId: 3 })).ok).toBe(false);
    activation.noteInput(4, "mouseUp");
    expect((await opener.open({ url, contentsId: 3 })).ok).toBe(false);
    activation.noteInput(3, "mouseUp");
    now += 5_000;
    expect((await opener.open({ url, contentsId: 3 })).ok).toBe(false);
    activation.noteInput(3, "mouseUp");
    expect(await opener.open({ url, contentsId: 3 })).toEqual({ ok: true });
    expect((await opener.open({ url, contentsId: 3 })).ok).toBe(false);
    expect(opened).toEqual([url]);
  });
});

// -- media scheme -----------------------------------------------------------------------------------

describe("media scheme confinement", () => {
  const work = fs.mkdtempSync(path.join(os.tmpdir(), "indus-audit-media-"));
  const root = path.join(work, "root");
  fs.mkdirSync(root);
  const PNG = Buffer.from("89504e470d0a1a0a", "hex");
  fs.writeFileSync(path.join(root, "in.png"), PNG);
  fs.writeFileSync(path.join(root, "notes.txt"), "text");
  fs.writeFileSync(path.join(root, "id_ed25519"), "private");
  fs.writeFileSync(path.join(root, "server.key"), "private");
  fs.writeFileSync(path.join(work, "out.png"), PNG);
  fs.symlinkSync(path.join(work, "out.png"), path.join(root, "link.png"));
  fs.symlinkSync(work, path.join(root, "up"));
  const handler = createMediaHandler({
    roots: new MediaRoots([root], "linux"),
    target: () => ({ mode: "local" }),
  });
  const get = (url: string, method = "GET") => handler(new Request(url, { method }));
  afterAll(() => fs.rmSync(work, { recursive: true, force: true }));

  it("a media file inside the roots is served", async () => {
    expect((await get(mediaUrl(path.join(root, "in.png")))).status).toBe(200);
  });

  it.each([
    ["outside the roots", () => mediaUrl(path.join(work, "out.png")), 403],
    ["a link inside a root pointing out", () => mediaUrl(path.join(root, "link.png")), 403],
    ["a linked folder pointing out", () => mediaUrl(path.join(root, "up", "out.png")), 403],
    ["traversal", () => mediaUrl(`${root}/../out.png`), 403],
    [
      "encoded traversal",
      () => `indus-media://stream/${encodeURIComponent(root)}%2F..%2Fout.png`,
      403,
    ],
    ["a key file", () => mediaUrl(path.join(root, "id_ed25519")), 403],
    ["a .key file", () => mediaUrl(path.join(root, "server.key")), 403],
    ["not a media type", () => mediaUrl(path.join(root, "notes.txt")), 415],
    ["a relative path", () => "indus-media://stream/in.png", 404],
    [
      "another host",
      () => `indus-media://elsewhere/${encodeURIComponent(path.join(root, "in.png"))}`,
      404,
    ],
    ["a NUL byte", () => mediaUrl(`${path.join(root, "in.png")}\0.png`), 404],
    ["a system path (missing or not)", () => mediaUrl("/etc/indus-audit.png"), 403],
    ["a system file of another type", () => mediaUrl("/etc/passwd"), 415],
  ])("%s is refused", async (_label, url, status) => {
    const answer = await get(url());
    expect(answer.status).toBe(status);
    expect(answer.status).not.toBe(200);
  });

  it("only GET and HEAD", async () => {
    for (const method of ["POST", "PUT", "DELETE"]) {
      expect((await get(mediaUrl(path.join(root, "in.png")), method)).status, method).toBe(405);
    }
  });

  it("nothing is served before a backend is chosen", async () => {
    const idle = createMediaHandler({ roots: new MediaRoots([root], "linux"), target: () => null });
    expect((await idle(new Request(mediaUrl(path.join(root, "in.png"))))).status).toBe(503);
  });
});

// -- deep links -------------------------------------------------------------------------------------

describe("deep links", () => {
  const SCHEMES = ["indus"];
  it.each([
    "https://open/settings",
    "indus-evil://open/settings",
    "indus://user@open/settings",
    "indus://open:8080/settings",
    "indus://run/rm",
    "indus://open/\u0007bell",
    `indus://open/x?${Array.from({ length: 17 }, (_, i) => `k${i}=v`).join("&")}`,
    `indus://open/${"a".repeat(5000)}`,
    "",
    42,
  ])("dropped before it reaches a window: %s", (raw) => {
    expect(parseDeepLink(raw, SCHEMES)).toBeNull();
  });

  it("an MCP link never starts a program and never adds a plain-http remote", () => {
    for (const params of [
      { command: "rm", args: "-rf" },
      { command: "node", url: "https://example.com/mcp" },
      { args: "x", url: "https://example.com/mcp" },
      { url: "http://example.com/mcp" },
      { url: "file:///tmp/server" },
      { url: "https://user:pw@example.com/mcp" },
      {},
    ]) {
      const action = planLink({ kind: "mcp", name: "docs", params });
      expect(action.kind, JSON.stringify(params)).toBe("refused");
    }
    expect(
      planLink({ kind: "mcp", name: "../x", params: { url: "https://example.com" } }).kind,
    ).toBe("refused");
  });

  it("skills and servers always wait for the person; app routes only", () => {
    expect(
      planLink({ kind: "mcp", name: "docs", params: { url: "https://example.com/mcp" } }).kind,
    ).toBe("confirm-mcp");
    expect(planLink({ kind: "skill", name: "demo-skill", params: {} }).kind).toBe("confirm-skill");
    expect(planLink({ kind: "skill", name: "../../etc", params: {} }).kind).toBe("refused");
    expect(planLink({ kind: "open", name: "no/such/route/at/all", params: {} }).kind).toBe(
      "refused",
    );
    expect(planLink({ kind: "open", name: "settings?x=1", params: {} }).kind).toBe("refused");
  });
});
