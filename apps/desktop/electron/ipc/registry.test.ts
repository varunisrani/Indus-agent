import { describe, expect, it, vi } from "vitest";
import { z } from "zod";
import { fileAppOrigin } from "../appOrigin.ts";
import { CHANNELS } from "./channels.ts";
import { IpcError, SENDER_REJECTED_MESSAGE } from "./envelope.ts";
import {
  type IpcMainLike,
  IpcRegistrationError,
  IpcRegistry,
  type SyncEventLike,
} from "./registry.ts";
import { SCHEMAS } from "./schemas.ts";
import type { FrameLike, IpcEventLike } from "./senderCheck.ts";

const APP_URL = "file:///opt/Indus/app/dist/index.html#/";
const ORIGIN = fileAppOrigin("/opt/Indus/app/dist/index.html", "linux");
const KNOWN_ID = 11;

type Listener = (event: IpcEventLike, ...args: unknown[]) => unknown;

function fakeIpc() {
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
  return { ipc, handles, ons };
}

function frame(url: string, routingId = 1): FrameLike {
  return { url, processId: 4, routingId, frameTreeNodeId: routingId };
}

function goodEvent(): IpcEventLike {
  const main = frame(APP_URL);
  return { sender: { id: KNOWN_ID, mainFrame: main, getType: () => "window" }, senderFrame: main };
}

const foreign: Record<string, () => IpcEventLike> = {
  "iframe frame (same origin)": () => {
    const main = frame(APP_URL, 1);
    return {
      sender: { id: KNOWN_ID, mainFrame: main, getType: () => "window" },
      senderFrame: frame(APP_URL, 2),
    };
  },
  "iframe frame (foreign)": () => {
    const main = frame(APP_URL, 1);
    return {
      sender: { id: KNOWN_ID, mainFrame: main },
      senderFrame: frame("https://evil.example/", 3),
    };
  },
  "webview guest": () => {
    const main = frame("http://localhost:3000/");
    return { sender: { id: 99, mainFrame: main, getType: () => "webview" }, senderFrame: main };
  },
  "webview guest on an app URL": () => {
    const main = frame(APP_URL);
    return {
      sender: { id: KNOWN_ID, mainFrame: main, getType: () => "webview" },
      senderFrame: main,
    };
  },
  "unknown window": () => {
    const main = frame(APP_URL);
    return { sender: { id: 12345, mainFrame: main, getType: () => "window" }, senderFrame: main };
  },
  "navigated to another file": () => {
    const main = frame("file:///home/me/Downloads/evil.html");
    return { sender: { id: KNOWN_ID, mainFrame: main }, senderFrame: main };
  },
  "frame gone": () => ({ sender: { id: KNOWN_ID, mainFrame: frame(APP_URL) }, senderFrame: null }),
  "destroyed contents": () => {
    const main = frame(APP_URL);
    return {
      sender: { id: KNOWN_ID, mainFrame: main, isDestroyed: () => true },
      senderFrame: main,
    };
  },
};

function registry() {
  const fake = fakeIpc();
  const log = vi.fn();
  const reg = new IpcRegistry({
    ipc: fake.ipc,
    policy: { appOrigin: ORIGIN, isKnownWebContents: (id) => id === KNOWN_ID },
    log,
  });
  return { ...fake, log, reg };
}

/** A valid payload for each sampled channel. */
const SAMPLE: Record<string, unknown> = {
  "indus:boot:get": undefined,
  "indus:boot:retry": { action: "retry" },
  "indus:connection:get": {},
  "indus:connection:ticket": { profile: "work" },
  "indus:api": { method: "GET", path: "/api/status" },
  "indus:profile:set": { profile: "work" },
  "indus:zoom:set": { level: 1 },
  "indus:external:open": { url: "https://example.com" },
  "indus:fs:read-text": { path: "/home/me/notes.md" },
  "indus:git:status": { repo: "/home/me/repo" },
  "indus:terminal:start": { cols: 80, rows: 24 },
  "indus:secrets:policy:set": { store: "file" },
};

describe("sender checks (D-16.18)", () => {
  const channels = Object.keys(SAMPLE);
  it("samples at least 10 channels", () => {
    expect(channels.length).toBeGreaterThanOrEqual(10);
  });

  for (const [label, make] of Object.entries(foreign)) {
    it(`${label}: refused on every sampled channel, handler never runs`, async () => {
      const { reg, handles, log } = registry();
      const handler = vi.fn(() => ({}));
      for (const name of channels) {
        const schema =
          (SCHEMAS as unknown as Record<string, { request: z.ZodType }>)[name]?.request ??
          z.unknown();
        reg.registerIpc(name, schema, handler);
      }
      for (const name of channels) {
        const answer = await handles.get(name)?.(make(), SAMPLE[name]);
        expect(answer).toEqual({
          ok: false,
          code: "sender-rejected",
          message: SENDER_REJECTED_MESSAGE,
        });
        // twice: logged once per channel and reason
        await handles.get(name)?.(make(), SAMPLE[name]);
      }
      expect(handler).not.toHaveBeenCalled();
      expect(log).toHaveBeenCalledTimes(channels.length);
    });
  }

  it("the app's own main frame passes, with the parsed payload", async () => {
    const { reg, handles } = registry();
    const handler = vi.fn((payload: unknown) => ({ echo: payload }));
    for (const name of channels) {
      const schema =
        (SCHEMAS as unknown as Record<string, { request: z.ZodType }>)[name]?.request ??
        z.unknown();
      reg.registerIpc(name, schema, handler);
    }
    for (const name of channels) {
      const answer = (await handles.get(name)?.(goodEvent(), SAMPLE[name])) as { ok: boolean };
      expect(answer.ok).toBe(true);
    }
    expect(handler).toHaveBeenCalledTimes(channels.length);
    // `none` payloads default to {}
    expect(await handles.get("indus:boot:get")?.(goodEvent(), undefined)).toEqual({
      ok: true,
      value: { echo: {} },
    });
  });
});

describe("payload validation", () => {
  const malformed: Array<[string, unknown]> = [
    ["indus:zoom:set", { level: "big" }],
    ["indus:zoom:set", { level: 1, extra: true }],
    ["indus:api", { method: "GET", path: "http://other/api/x" }],
    ["indus:api", { method: "TRACE", path: "/api/x" }],
    ["indus:boot:retry", undefined],
    ["indus:profile:set", { profile: "" }],
    ["indus:external:open", "https://example.com"],
    ["indus:terminal:start", { cols: 0, rows: 24 }],
    ["indus:boot:get", { unexpected: 1 }],
  ];
  for (const [name, payload] of malformed) {
    it(`${name} ${JSON.stringify(payload)} → bad-request`, async () => {
      const { reg, handles } = registry();
      const handler = vi.fn();
      reg.registerIpc(
        name,
        (SCHEMAS as unknown as Record<string, { request: z.ZodType }>)[name]?.request ?? z.never(),
        handler,
      );
      const answer = (await handles.get(name)?.(goodEvent(), payload)) as {
        ok: boolean;
        code?: string;
      };
      expect(answer).toMatchObject({ ok: false, code: "bad-request" });
      expect(handler).not.toHaveBeenCalled();
    });
  }

  it("more than one argument is refused", async () => {
    const { reg, handles } = registry();
    reg.handle("indus:zoom:get", () => ({ level: 0, percent: 100 }));
    expect(await handles.get("indus:zoom:get")?.(goodEvent(), {}, "extra")).toMatchObject({
      code: "bad-request",
    });
  });
});

describe("answers", () => {
  it("IpcError keeps its code; other errors become internal without detail", async () => {
    const { reg, handles, log } = registry();
    reg.handle("indus:zoom:set", () => {
      throw new IpcError("out-of-range", "too big");
    });
    reg.handle("indus:zoom:get", () => {
      throw new TypeError("secret detail");
    });
    expect(await handles.get("indus:zoom:set")?.(goodEvent(), { level: 1 })).toEqual({
      ok: false,
      code: "out-of-range",
      message: "too big",
    });
    const internal = (await handles.get("indus:zoom:get")?.(goodEvent(), undefined)) as {
      message: string;
    };
    expect(internal).toMatchObject({ ok: false, code: "internal" });
    expect(internal.message).not.toContain("secret detail");
    expect(log.mock.calls.join("\n")).not.toContain("secret detail");
  });

  it("send channels run the handler only for good senders and payloads", () => {
    const { reg, ons } = registry();
    const handler = vi.fn();
    reg.on("indus:window:control", handler);
    ons.get("indus:window:control")?.(goodEvent(), { action: "minimize" });
    ons.get("indus:window:control")?.(foreign["webview guest"]?.() as IpcEventLike, {
      action: "close",
    });
    ons.get("indus:window:control")?.(goodEvent(), { action: "explode" });
    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler.mock.calls[0]?.[0]).toEqual({ action: "minimize" });
  });

  it("sync channels always set returnValue (refusal included)", () => {
    const { reg, ons } = registry();
    reg.sync("indus:facts", () => ({
      platform: "linux",
      appVersion: "1.0.0",
      packaged: true,
      dev: false,
      windowKind: "main" as const,
      watch: false,
      profile: null,
      titleBarHeight: 34,
      deepLinkScheme: "indus",
    }));
    const good = { ...goodEvent(), returnValue: undefined } as SyncEventLike;
    ons.get("indus:facts")?.(good);
    expect(good.returnValue).toMatchObject({ ok: true, value: { platform: "linux" } });
    const bad = {
      ...(foreign["unknown window"]?.() as IpcEventLike),
      returnValue: undefined,
    } as SyncEventLike;
    ons.get("indus:facts")?.(bad);
    expect(bad.returnValue).toMatchObject({ ok: false, code: "sender-rejected" });
  });

  it("emit validates the payload and skips destroyed targets", () => {
    const { reg, log } = registry();
    const send = vi.fn();
    expect(reg.emit({ send }, "indus:zoom:changed", { level: 1, percent: 120 })).toBe(true);
    expect(reg.emit({ send }, "indus:zoom:changed", { level: "x" } as never)).toBe(false);
    expect(
      reg.emit({ send, isDestroyed: () => true }, "indus:zoom:changed", { level: 1, percent: 120 }),
    ).toBe(false);
    expect(send).toHaveBeenCalledTimes(1);
    expect(log).toHaveBeenCalledTimes(1);
  });
});

describe("registration rules", () => {
  it("refuses non-indus names, unknown channels, duplicates, wrong kinds and late registrations", () => {
    const { reg } = registry();
    expect(() => reg.registerIpc("other:x", z.unknown(), () => {})).toThrow(IpcRegistrationError);
    expect(() => reg.registerIpc("indus:not-in-table", z.unknown(), () => {})).toThrow(
      IpcRegistrationError,
    );
    reg.handle("indus:zoom:get", () => ({ level: 0, percent: 100 }));
    expect(() => reg.handle("indus:zoom:get", () => ({ level: 0, percent: 100 }))).toThrow(
      /already/,
    );
    expect(() => reg.registerIpc("indus:zoom:changed", z.unknown(), () => {})).toThrow(/event/);
    reg.seal();
    expect(() => reg.handle("indus:zoom:set", ({ level }) => ({ level, percent: 100 }))).toThrow(
      /after seal/,
    );
  });

  it("seal answers not-available for declared invoke channels without a handler (still sender-checked)", async () => {
    const { reg, handles } = registry();
    reg.seal();
    const invokes = CHANNELS.filter((c) => c.kind === "invoke").map((c) => c.name);
    expect(invokes.every((name) => handles.has(name))).toBe(true);
    expect(await handles.get("indus:git:status")?.(goodEvent(), { repo: "/r" })).toMatchObject({
      ok: false,
      code: "not-available",
    });
    expect(
      await handles.get("indus:git:status")?.(foreign["unknown window"]?.() as IpcEventLike, {}),
    ).toMatchObject({
      code: "sender-rejected",
    });
  });

  it("every channel in the table starts with indus: and has schemas of its kind", () => {
    for (const def of CHANNELS) {
      expect(def.name.startsWith("indus:")).toBe(true);
      const entry = (SCHEMAS as unknown as Record<string, Record<string, unknown>>)[def.name] ?? {};
      const want = {
        invoke: ["request", "response"],
        send: ["request"],
        sync: ["response"],
        event: ["payload"],
      }[def.kind];
      expect(Object.keys(entry).sort()).toEqual([...want].sort());
    }
    expect(Object.keys(SCHEMAS).sort()).toEqual(CHANNELS.map((c) => c.name).sort());
  });
});
