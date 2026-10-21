// The wired backend through the real IPC registry and the fake `indus` (P20-08..14 together):
// a window asks for a connection, gets a ticket, dials, calls the REST proxy; the quit guard
// informs; every channel this slice owns has a handler.

import fs from "node:fs";
import path from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { cleanupFakes, posix, tempDir, writeFakeIndus } from "../../testing/fakeIndus.ts";
import { closeFakeServes, fakeServe, SERVE_TOKEN } from "../../testing/fakeServe.ts";
import { fileAppOrigin } from "../appOrigin.ts";
import type { MainContext } from "../context.ts";
import { CHANNELS } from "../ipc/channels.ts";
import type { Envelope } from "../ipc/envelope.ts";
import { type IpcMainLike, IpcRegistry } from "../ipc/registry.ts";
import type { FrameLike, IpcEventLike } from "../ipc/senderCheck.ts";
import { CspState } from "../security/csp.ts";
import { MachineryGate } from "../singleInstance.ts";
import { type ElectronHooks, installBackend } from "./install.ts";
import type { UpgradeDetails } from "./tickets.ts";

const APP_URL = "file:///opt/Indus/app/dist/index.html#/";
const KNOWN_ID = 11;
const OWNED = ["P20-08", "P20-09", "P20-10", "P20-11", "P20-12", "P20-13", "P20-14", "P24-29"];

type Listener = (event: IpcEventLike, ...args: unknown[]) => unknown;

const savedEnv = { ...process.env };
afterAll(() => {
  for (const key of Object.keys(process.env)) if (!(key in savedEnv)) delete process.env[key];
  Object.assign(process.env, savedEnv);
  cleanupFakes();
  closeFakeServes();
});

function rig() {
  const base = tempDir();
  const home = path.join(base, "home");
  const userData = path.join(base, "userData");
  fs.mkdirSync(home, { recursive: true, mode: 0o700 });
  fs.mkdirSync(userData, { recursive: true, mode: 0o700 });
  process.env.INDUS_DESKTOP_INDUS = writeFakeIndus(path.join(base, "bin", "indus"));
  delete process.env.INDUS_DESKTOP_INDUS_ROOT;
  process.env.INDUS_FAKE_SCENARIO = "healthy";
  process.env.INDUS_HOME = home;

  const handles = new Map<string, Listener>();
  const ons = new Map<string, Listener>();
  const ipc: IpcMainLike = {
    handle: (channel, listener) => {
      handles.set(channel, listener as Listener);
    },
    on: (channel, listener) => {
      ons.set(channel, listener as Listener);
      return ipc;
    },
  };
  const appOrigin = fileAppOrigin("/opt/Indus/app/dist/index.html", "linux");
  const registry = new IpcRegistry({
    ipc,
    policy: { appOrigin, isKnownWebContents: (id) => id === KNOWN_ID },
    log: () => {},
  });
  const sent: Array<{ channel: string; payload: unknown }> = [];
  const webContents = {
    id: KNOWN_ID,
    send: (channel: string, payload: unknown) => sent.push({ channel, payload }),
    isDestroyed: () => false,
  };
  const secrets = new Map<string, string>();
  const lines: string[] = [];
  const context = {
    registry,
    windows: { windows: () => [{ webContents }] },
    log: { write: (line: string) => lines.push(line) },
    paths: { home, userData },
    appOrigin,
    csp: new CspState({ dev: false }),
    dev: false,
    packaged: true,
    platform: process.platform,
    appVersion: "0.20.0",
    deepLinkScheme: "indus",
    machinery: new MachineryGate(true),
    secrets: {
      get: (name: string) => secrets.get(name) ?? null,
      put: (name: string, value: string) => secrets.set(name, value),
      delete: (name: string) => secrets.delete(name),
    },
    shellPath: Promise.resolve({ applied: false, reason: "test" }),
  } as unknown as MainContext;

  const appListeners = new Map<string, (...args: never[]) => unknown>();
  let rewrite:
    | ((
        details: UpgradeDetails,
        cb: (r: { requestHeaders?: Record<string, string> }) => void,
      ) => void)
    | null = null;
  const boxes: Array<{ message: string; buttons: string[] }> = [];
  const electron: ElectronHooks = {
    app: {
      on: (event: string, listener: (...args: never[]) => unknown) => {
        appListeners.set(event, listener);
        return undefined;
      },
      quit: () => {},
      relaunch: () => {},
      exit: () => {},
      getAppPath: () => base,
    } as unknown as ElectronHooks["app"],
    webRequest: {
      onBeforeSendHeaders: (_filter, listener) => {
        rewrite = listener;
      },
    },
    powerMonitor: { on: () => undefined },
    showMessageBox: async (options) => {
      boxes.push(options);
      return { response: 0 };
    },
    argv: [],
    execPath: process.execPath,
    homeDir: path.join(base, "nobody"),
    isWsl: false,
  };
  const handle = installBackend(context, electron);
  registry.seal();

  const main: FrameLike = { url: APP_URL, processId: 4, routingId: 1, frameTreeNodeId: 1 };
  const event: IpcEventLike = {
    sender: { id: KNOWN_ID, mainFrame: main, getType: () => "window" },
    senderFrame: main,
  };
  const invoke = async (channel: string, payload?: unknown) => {
    const listener = handles.get(channel);
    if (listener === undefined) throw new Error(`no handler for ${channel}`);
    return (await listener(
      event,
      ...(payload === undefined ? [] : [payload]),
    )) as Envelope<unknown>;
  };
  const send = (channel: string, payload: unknown) => ons.get(channel)?.(event, payload);
  return {
    handle,
    handles,
    ons,
    invoke,
    send,
    sent,
    appListeners,
    boxes,
    lines,
    rewrite: () => rewrite,
    home,
  };
}

describe.skipIf(!posix)("the wired backend (fake indus)", () => {
  it("every channel this slice owns has a handler, and nothing answers not-available", async () => {
    const r = rig();
    for (const def of CHANNELS) {
      if (!OWNED.includes(def.owner) || def.kind === "event") continue;
      expect(r.handles.has(def.name) || r.ons.has(def.name), def.name).toBe(true);
    }
    const progress = await r.invoke("indus:boot:get");
    expect(progress).toMatchObject({ ok: true, value: { phase: "resolve" } });
  });

  it("connection → ticket → dial → REST, with no credential reaching the window", async () => {
    const r = rig();
    const got = await r.invoke("indus:connection:get", {});
    expect(got.ok).toBe(true);
    const descriptor = (got as { value: Record<string, unknown> }).value;
    expect(descriptor).toMatchObject({ mode: "local", profile: null, codeVersion: "0.20.0" });
    expect(String(descriptor.wsUrl)).toMatch(/^ws:\/\/127\.0\.0\.1:\d+\/ws$/);

    // every window heard the boot and the connection
    const phases = r.sent
      .filter((s) => s.channel === "indus:boot:progress")
      .map((s) => (s.payload as { phase: string }).phase);
    expect(phases.at(-1)).toBe("ready");
    expect(r.sent.some((s) => s.channel === "indus:connection:changed")).toBe(true);

    const ticketed = (await r.invoke("indus:connection:ticket", {})) as {
      ok: true;
      value: { ticket: string; wsUrl: string; protocols: string[] };
    };
    expect(ticketed.ok).toBe(true);
    const { wsUrl, protocols, ticket } = ticketed.value;
    // main strips Origin for exactly this dial
    const headers: Array<Record<string, string> | undefined> = [];
    r.rewrite()?.(
      {
        url: wsUrl,
        webContentsId: KNOWN_ID,
        resourceType: "webSocket",
        requestHeaders: { Origin: "file://", "Sec-WebSocket-Protocol": protocols.join(", ") },
      },
      (response) => headers.push(response.requestHeaders),
    );
    expect(headers[0]).toEqual({
      "Sec-WebSocket-Protocol": `indus-gateway-v1, indus-gateway-ticket.${ticket}`,
    });
    const opened = await new Promise<boolean>((resolve) => {
      const ws = new WebSocket(wsUrl, protocols);
      ws.addEventListener("message", () => {
        resolve(true);
        ws.close();
      });
      ws.addEventListener("close", () => resolve(false));
    });
    expect(opened).toBe(true);

    const api = await r.invoke("indus:api", { method: "GET", path: "/api/sessions" });
    expect(api).toEqual({
      ok: true,
      value: { kind: "response", status: 200, body: { sessions: [] } },
    });
    const missing = await r.invoke("indus:api", { method: "GET", path: "/api/nope" });
    expect(missing).toMatchObject({ ok: true, value: { kind: "not-found", status: 404 } });
    const outside = await r.invoke("indus:api", { method: "GET", path: "/api/x?u=http://other/" });
    expect(outside.ok).toBe(true); // a query value is data, the origin stays ours

    const everything = JSON.stringify([r.sent, got, ticketed, api]);
    const token = r.handle.orchestrator.current()?.rest.token ?? "";
    expect(token).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(everything).not.toContain(token);
  });

  it("quit with an attached daemon and running turns: informational prompt, nothing stopped", async () => {
    const r = rig();
    await r.invoke("indus:connection:get", {});
    r.send("indus:work:active", { items: [{ id: "s1", label: "Refactor the parser" }] });
    let prevented = false;
    (r.appListeners.get("before-quit") as unknown as (e: { preventDefault(): void }) => void)({
      preventDefault: () => {
        prevented = true;
      },
    });
    expect(prevented).toBe(true);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(r.boxes[0]).toMatchObject({
      message: "The agent is still working on 1 chat.",
      buttons: ["Cancel", "Quit"],
    });
    // the daemon still answers
    const ticket = await r.invoke("indus:connection:ticket", {});
    expect(ticket.ok).toBe(true);
  });

  it("a registry remote dials with a server-minted ticket per dial, never the token (D-16.691)", async () => {
    const r = rig();
    const serve = await fakeServe();
    const saved = (await r.invoke("indus:connections:save", {
      kind: "remote",
      url: serve.url,
      label: "Build box",
      token: SERVE_TOKEN,
      headers: { "CF-Access-Client-Id": "cf-7", Cookie: "x=1" },
    })) as { ok: true; value: { saved: boolean; id: string; droppedHeaders: string[] } };
    expect(saved.value).toMatchObject({ saved: true, droppedHeaders: ["Cookie"] });
    const listed = (await r.invoke("indus:connections:set-primary", { id: saved.value.id })) as {
      value: { primaryId: string; connections: Array<Record<string, unknown>> };
    };
    expect(listed.value.primaryId).toBe(saved.value.id);
    const got = await r.invoke("indus:connection:get", {});
    expect(got).toMatchObject({ ok: true, value: { mode: "remote", codeVersion: "0.24.0" } });

    const dials: Array<{ ticket: string; protocols: string[]; wsUrl: string }> = [];
    for (let i = 0; i < 2; i += 1) {
      const answer = (await r.invoke("indus:connection:ticket", {})) as {
        ok: true;
        value: { ticket: string; protocols: string[]; wsUrl: string };
      };
      dials.push(answer.value);
    }
    expect(dials[0]?.ticket).not.toBe(dials[1]?.ticket);
    expect(serve.minted.slice(-2)).toEqual(dials.map((d) => d.ticket)); // the save test minted one
    expect(dials[0]?.protocols).toEqual([
      "indus-dashboard-v1",
      `indus-dashboard-ticket.${dials[0]?.ticket}`,
    ]);
    // inside Electron the hook sees no subprotocol header: matched by window + URL, Origin removed
    const headers: Array<Record<string, string> | undefined> = [];
    r.rewrite()?.(
      {
        url: dials[0]?.wsUrl as string,
        webContentsId: KNOWN_ID,
        resourceType: "webSocket",
        requestHeaders: { Origin: "file://" },
      },
      (response) => headers.push(response.requestHeaders),
    );
    expect(headers[0]).toEqual({});
    const opened = await new Promise<boolean>((resolve) => {
      const ws = new WebSocket(dials[0]?.wsUrl as string, dials[0]?.protocols);
      ws.addEventListener("message", () => {
        resolve(true);
        ws.close();
      });
      ws.addEventListener("close", () => resolve(false));
    });
    expect(opened).toBe(true);
    const api = await r.invoke("indus:api", { method: "GET", path: "/api/config" });
    expect(api).toMatchObject({ ok: true, value: { status: 200 } });
    expect(
      serve.seen.some((line) => line.startsWith("GET /api/config") && line.includes("cf=cf-7")),
    ).toBe(true);
    const everything = JSON.stringify([r.sent, got, dials, listed, saved]);
    expect(everything).not.toContain(SERVE_TOKEN);
    expect(r.lines.join("\n")).not.toContain(SERVE_TOKEN);
  });

  it("remote: a bad address is bad-request; nothing saved when the server is unreachable", async () => {
    const r = rig();
    expect(await r.invoke("indus:remote:test", { url: "ftp://x", token: "t" })).toMatchObject({
      ok: false,
      code: "bad-request",
    });
    const saved = await r.invoke("indus:remote:save", { url: "127.0.0.1:9", token: "some-token" });
    expect(saved).toMatchObject({ ok: true, value: { saved: false, http: "unreachable" } });
    expect(await r.invoke("indus:remote:get")).toEqual({
      ok: true,
      value: { url: null, hasToken: false },
    });
  });
});
